#!/usr/bin/env python3
"""Offline passage search. G_RESEARCH_DATA selects the data root, as in common.py.

  python3 -I knowledge/search.py build
  python3 -I knowledge/search.py ask "question" --k 10 --json --scope library

Requires model2vec, numpy and sqlite-vec in DATA/search/venv. Model weights must
already be cached in DATA/search/models. Nothing is downloaded. The index and
all build state live in DATA/search, never beside this public script.
Offsets are zero-based, end-exclusive Unicode character positions in the UTF-8
decoded source, with line endings retained. Scores are ranks, not confidence.
"""
import argparse
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
import tomllib

DATA = Path(os.path.abspath(os.environ.get('G_RESEARCH_DATA', '/home/gabriel/g-research-data')))
SEARCH = DATA / 'search'
MODEL = 'minishlab/potion-base-8M'
DIMENSIONS = 256
WORDS = 280
OVERLAP = 60
VERSION = '1'
FUSION_WEIGHTS = {'lexical': 2.0, 'dense': 1.0}
PATTERNS = ('notes/**/*.md', 'reports/*.md', 'drafts/*.md', 'view/**/*.md',
            'charters/**/*.toml', 'journal/*.md', 'sweeps/*.txt', 'runs/*/*.md')
# Source bodies saved beside reading notes count as held literature.
HELD = ('notes/sources/*.txt', 'charters/*/evidence/*.txt')
STOPWORDS = set('a an and are as at be been but by can did do does for from had has '
                'have how i if in into is it its may more not of on or our should '
                'so than that the their then there these they this those to up '
                'was we were what when where which while who why will with would'.split())


def configure():
    sys.dont_write_bytecode = True
    for key in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS',
                'NUMEXPR_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS', 'RAYON_NUM_THREADS'):
        os.environ[key] = '1'
    os.environ['TOKENIZERS_PARALLELISM'] = 'false'
    os.environ['JOBLIB_MULTIPROCESSING'] = '0'
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
    os.environ['DO_NOT_TRACK'] = '1'


def bootstrap():
    configure()
    venv = SEARCH / 'venv'
    python = venv / 'bin/python'
    if python.is_file() and Path(sys.prefix).resolve() != venv.resolve():
        os.execv(str(python), [str(python), '-I', '-B', str(Path(__file__).absolute()), *sys.argv[1:]])


class Embedder:
    def __init__(self):
        configure()
        from model2vec import StaticModel
        hub = SEARCH / 'models' / ('models--' + MODEL.replace('/', '--'))
        ref = hub / 'refs/main'
        snapshots = hub / 'snapshots'
        paths = [snapshots / ref.read_text().strip()] if ref.is_file() else []
        paths += sorted(snapshots.glob('*'))
        model = next((p for p in paths if all((p / n).is_file() for n in
                     ('model.safetensors', 'tokenizer.json', 'config.json'))), None)
        if model is None:
            raise ValueError(f'cached {MODEL} weights missing or broken under {hub}; no download attempted')
        self.revision = model.name
        self.model = StaticModel.from_pretrained(model, force_download=False, max_length=None)

    def encode(self, texts):
        import numpy as np
        vectors = self.model.encode(texts, use_multiprocessing=False,
                                    batch_size=128, max_length=None, normalize=True)
        vectors = np.asarray(vectors, dtype='<f4')
        if vectors.shape != (len(texts), DIMENSIONS) or not np.isfinite(vectors).all():
            raise ValueError('embedding model returned invalid vectors')
        return vectors


def connect(readonly=False):
    import sqlite_vec
    path = SEARCH / 'passages.sqlite'
    if readonly and not path.is_file():
        raise ValueError('no passage index found; run search.py build first')
    db = sqlite3.connect(path.as_uri() + '?mode=ro' if readonly else str(path),
                         uri=readonly, timeout=30)
    db.row_factory = sqlite3.Row
    db.enable_load_extension(True)
    try:
        sqlite_vec.load(db)
    finally:
        db.enable_load_extension(False)
    db.execute('PRAGMA cache_size=-16384')
    db.execute('PRAGMA temp_store=MEMORY')
    if readonly:
        db.execute('PRAGMA query_only=ON')
    return db


def schema(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, sha256 TEXT NOT NULL,
            source TEXT NOT NULL, scope TEXT NOT NULL, title TEXT NOT NULL, year TEXT
        );
        CREATE TABLE IF NOT EXISTS passages (
            id INTEGER PRIMARY KEY, path TEXT NOT NULL REFERENCES files(path),
            source TEXT NOT NULL, scope TEXT NOT NULL, title TEXT NOT NULL, year TEXT,
            start INTEGER NOT NULL, end INTEGER NOT NULL, text TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS passages_path ON passages(path);
        CREATE INDEX IF NOT EXISTS passages_source ON passages(source);
        CREATE VIRTUAL TABLE IF NOT EXISTS lexical USING fts5(
            title, text, source, content=passages, content_rowid=id,
            tokenize='porter unicode61 remove_diacritics 2'
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS vectors USING vec0(
            embedding float[256] distance_metric=cosine, scope text, source text
        );
    ''')


def catalogues():
    records = {}
    # The sibling path also supports staging this script in DATA/search.
    for directory in (DATA, DATA.parent / 'g-research/knowledge', Path(__file__).resolve().parent):
        for name in ('shelf.toml', 'papers.toml'):
            path = directory / name
            if not path.is_file():
                continue
            for entries in tomllib.loads(path.read_text(encoding='utf-8')).values():
                if isinstance(entries, list):
                    for entry in entries:
                        if isinstance(entry, dict) and entry.get('id'):
                            records[entry['id']] = entry
    return records


def inputs():
    paths = set((DATA / 'library').glob('*/text.txt'))
    for pattern in PATTERNS + HELD:
        paths.update(DATA.glob(pattern))
    root = DATA.resolve()
    for path in sorted(paths):
        if not path.resolve().is_relative_to(root):
            raise ValueError(f'source symlink leaves data root: {path}')
        yield path


def document(path, records):
    raw = path.read_bytes()
    text = raw.decode('utf-8', errors='replace')
    relative = path.relative_to(DATA).as_posix()
    # context-* files are our own framing notes handed to a reader, not literature.
    held = any(path.match(pattern) for pattern in HELD) and not path.name.startswith('context-')
    scope = 'library' if held or relative.startswith('library/') else 'notes'
    source = 'n.' + path.stem if held else path.parent.name if scope == 'library' else relative
    meta = {}
    if scope == 'library':
        metadata = path.with_suffix('.json') if held else path.with_name('meta.json')
        if metadata.exists():
            meta = json.loads(metadata.read_text(encoding='utf-8'))
            meta = {key: meta[key] for key in ('title', 'year') if isinstance(meta.get(key), (str, int))}
    record = records.get(source, {})
    heading = re.search(r'(?m)^#{1,6}\s+(.+)', text)
    first = next((line.strip() for line in text.splitlines() if line.strip()), path.stem)
    title = meta.get('title') or record.get('title') or (heading[1] if heading else first)
    year = meta.get('year') or record.get('year')
    if scope == 'notes' and path.suffix == '.toml':
        note = tomllib.loads(text)
        title = note.get('title') or title
    if not year and scope == 'library':
        match = re.search(r'(?:19|20)\d{2}', source)
        year = match[0] if match else None
    if not year and scope == 'notes':
        match = re.search(r'(?:19|20)\d{2}', path.name)
        year = match[0] if match else None
    title = str(title).strip()[:500]
    year = str(year) if year else None
    sha256 = hashlib.sha256(raw).hexdigest()
    fingerprint = hashlib.sha256(json.dumps([sha256, meta, title, year, source, scope],
                                           sort_keys=True).encode()).hexdigest()
    return relative, source, scope, title, year, sha256, fingerprint, text


def passages(text):
    words = list(re.finditer(r'\S+', text))
    offset = 0
    while offset < len(words):
        end = min(offset + WORDS, len(words))
        start_char, end_char = words[offset].start(), words[end - 1].end()
        yield start_char, end_char, text[start_char:end_char]
        if end == len(words):
            break
        offset = end - OVERLAP


def remove(db, path):
    rows = db.execute('SELECT id, title, text, source FROM passages WHERE path=?', (path,)).fetchall()
    for row in rows:
        db.execute("INSERT INTO lexical(lexical,rowid,title,text,source) VALUES('delete',?,?,?,?)", tuple(row))
    db.execute('DELETE FROM vectors WHERE rowid IN (SELECT id FROM passages WHERE path=?)', (path,))
    db.execute('DELETE FROM passages WHERE path=?', (path,))
    db.execute('DELETE FROM files WHERE path=?', (path,))


def build():
    started = time.perf_counter()
    SEARCH.mkdir(parents=True, exist_ok=True)
    # Re-exec through nice even when the operator omits it.
    if os.getpriority(os.PRIO_PROCESS, 0) < 10:
        os.execvp('nice', ['nice', '-n', '10', sys.executable, '-I', '-B',
                          str(Path(__file__).absolute()), *sys.argv[1:]])
    with (SEARCH / 'build.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('another search build is already running') from None
        with contextlib.closing(connect()) as db:
            schema(db)
            settings = dict(db.execute('SELECT key,value FROM settings'))
            config = json.dumps([VERSION, MODEL, DIMENSIONS, WORDS, OVERLAP])
            if settings and settings.get('config') != config:
                raise ValueError('index format/model changed; move passages.sqlite aside and rebuild')
            previous = dict(db.execute('SELECT path,fingerprint FROM files'))
            records = catalogues()
            seen = set()
            changed = embedded = unchanged = 0
            embedder = None
            with db:
                db.execute('BEGIN IMMEDIATE')
                for path in inputs():
                    relative, source, scope, title, year, sha, fingerprint, text = document(path, records)
                    seen.add(relative)
                    if previous.get(relative) == fingerprint:
                        unchanged += 1
                        continue
                    chunks = list(passages(text))
                    if chunks and embedder is None:
                        embedder = Embedder()
                        if settings.get('revision') and settings['revision'] != embedder.revision:
                            raise ValueError('cached model revision changed; move passages.sqlite aside and rebuild')
                    remove(db, relative)
                    db.execute('INSERT INTO files VALUES(?,?,?,?,?,?,?)',
                               (relative, fingerprint, sha, source, scope, title, year))
                    for offset in range(0, len(chunks), 128):
                        batch = chunks[offset:offset + 128]
                        vectors = embedder.encode([title + '\n' + chunk[2] for chunk in batch])
                        for (start, end, passage), vector in zip(batch, vectors):
                            rowid = db.execute('''INSERT INTO passages(path,source,scope,title,year,start,end,text)
                                VALUES(?,?,?,?,?,?,?,?)''',
                                (relative, source, scope, title, year, start, end, passage)).lastrowid
                            db.execute('INSERT INTO lexical(rowid,title,text,source) VALUES(?,?,?,?)',
                                       (rowid, title, passage, source))
                            db.execute('INSERT INTO vectors(rowid,embedding,scope,source) VALUES(?,?,?,?)',
                                       (rowid, vector.tobytes(), scope, source))
                        embedded += len(batch)
                    changed += 1
                    if changed % 100 == 0:
                        print(f'{changed} changed files, {embedded} passages embedded', file=sys.stderr, flush=True)
                removed = set(previous) - seen
                for relative in removed:
                    remove(db, relative)
                built_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
                for key, value in (('config', config), ('built_at', built_at), ('model', MODEL)):
                    db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (key, value))
                if embedder is not None:
                    db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', ('revision', embedder.revision))
                if changed or removed:
                    db.execute("INSERT INTO lexical(lexical) VALUES('optimize')")
            summary = dict(files=len(seen), changed=changed, unchanged=unchanged, removed=len(removed),
                           embedded=embedded, passages=db.execute('SELECT count(*) FROM passages').fetchone()[0],
                           seconds=round(time.perf_counter() - started, 3),
                           index_bytes=(SEARCH / 'passages.sqlite').stat().st_size, nice=os.getpriority(os.PRIO_PROCESS, 0))
            print(json.dumps(summary))


def filters(scope, source):
    clauses, params = [], []
    if scope != 'all':
        clauses.append('scope=?')
        params.append(scope)
    if source:
        clauses.append('source=?')
        params.append(source)
    return ''.join(' AND ' + clause for clause in clauses), params


def lexical(db, question, limit, scope='all', source=None):
    tokens = list(dict.fromkeys(re.findall(r'[^\W_]+', question.lower())))
    useful = [t for t in tokens if t not in STOPWORDS]
    tokens = (useful or tokens)[:96]
    if not tokens:
        return []
    expression = ' OR '.join('"' + token + '"' for token in tokens)
    where, params = filters(scope, source)
    where = where.replace('scope=?', 'p.scope=?').replace('source=?', 'p.source=?')
    sql = '''SELECT p.id, bm25(lexical, 3.0, 1.0, 2.0) AS score FROM lexical
             JOIN passages p ON p.id=lexical.rowid WHERE lexical MATCH ?'''
    return [row['id'] for row in db.execute(sql + where + ' ORDER BY score,p.id LIMIT ?',
                                           [expression, *params, limit])]


def dense(db, vector, limit, scope='all', source=None):
    where, params = filters(scope, source)
    return [row['rowid'] for row in db.execute('''SELECT rowid,distance FROM vectors
        WHERE embedding MATCH ? AND k=?''' + where + ' ORDER BY distance',
        [vector.tobytes(), limit, *params])]


def rank(db, question, k=10, scope='all', source=None, method='hybrid', embedder=None):
    limit = min(4096, max(100, k * 10))
    ranks = {}
    lists = {}
    if method in ('lexical', 'hybrid'):
        lists['lexical'] = lexical(db, question, limit, scope, source)
    if method in ('dense', 'hybrid'):
        embedder = embedder or Embedder()
        lists['dense'] = dense(db, embedder.encode([question])[0], limit, scope, source)
    for name, ids in lists.items():
        for position, rowid in enumerate(ids, 1):
            ranks.setdefault(rowid, {})[name] = position
    def score(rowid):
        return sum(FUSION_WEIGHTS[name] / (60 + position) for name, position in ranks[rowid].items())

    ordered = sorted(ranks, key=lambda rowid: (-score(rowid), rowid))[:k]
    hits = []
    for rowid in ordered:
        row = db.execute('''SELECT p.*, f.sha256 FROM passages p JOIN files f ON p.path=f.path
                            WHERE p.id=?''', (rowid,)).fetchone()
        hit = dict(row)
        hit['path'] = str(DATA / hit['path'])
        hit['score'] = score(rowid)
        hit['ranks'] = ranks[rowid]
        hits.append(hit)
    return hits


def ask(args):
    with contextlib.closing(connect(readonly=True)) as db:
        with db:
            # One read transaction keeps postings, vectors and metadata in the same generation.
            db.execute('BEGIN')
            settings = dict(db.execute('SELECT key,value FROM settings'))
            expected = json.dumps([VERSION, MODEL, DIMENSIONS, WORDS, OVERLAP])
            if settings.get('config') != expected:
                raise ValueError('index format/model mismatch; rebuild the passage index')
            embedder = Embedder()
            if settings.get('revision') and settings['revision'] != embedder.revision:
                raise ValueError('cached model revision differs from index; rebuild the passage index')
            hits = rank(db, args.question, args.k, args.scope, args.source, embedder=embedder)
        result = dict(question=args.question, scope=args.scope, source=args.source,
                      model=MODEL, revision=embedder.revision, fusion_weights=FUSION_WEIGHTS,
                      built_at=settings['built_at'], hits=hits)
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        elif not hits:
            print('No matching passages.')
        else:
            for n, hit in enumerate(hits, 1):
                print(f'[{n}] {hit["source"]} | {hit["title"]} | {hit["year"] or "year unknown"}')
                print(f'{hit["path"]} chars [{hit["start"]}:{hit["end"]}]')
                print(hit['text'] + '\n')


def main():
    bootstrap()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('build')
    query = commands.add_parser('ask')
    query.add_argument('question')
    query.add_argument('--k', type=int, default=10)
    query.add_argument('--json', action='store_true')
    query.add_argument('--scope', choices=('library', 'notes', 'all'), default='all')
    query.add_argument('--source')
    args = parser.parse_args()
    if args.command == 'ask' and (not args.question.strip() or not 1 <= args.k <= 100):
        parser.error('question must be nonempty and --k must be between 1 and 100')
    try:
        build() if args.command == 'build' else ask(args)
    except (ValueError, OSError, sqlite3.Error, ImportError) as exc:
        print(f'search: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
