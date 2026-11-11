#!/usr/bin/env python3
"""Look further than the agents did, mechanically, and keep the denominator.

  neighbours.py queries queries.txt OUT_DIR    the top results for each line of queries.txt
  neighbours.py refs papers.toml OUT_DIR       what the papers we hold cite, counted across them
  neighbours.py venues terms.txt OUT_DIR       every paper title at the main graphics venues, by year

Both write a tab separated file with every candidate seen, so what was looked at and
turned down is on record. Two open indexes are used, Crossref and arXiv, and neither
needs a key. Only public paper titles and search phrases are sent. Answers are cached
in OUT_DIR/cache so a rerun costs nothing.

venues walks Ke-Sen Huang's paper lists (SIGGRAPH, SIGGRAPH Asia, SCA, I3D, HPG,
Eurographics, EGSR, MIG) and writes every title with the parts of the design its words
touch, taken from terms.txt. A title that touches nothing is written too.

Who cites the papers we hold is not covered: no keyless index gives it in bulk.
"""
import argparse, difflib, hashlib, json, os, re, sys, time, tomllib, urllib.parse, urllib.request
from collections import defaultdict

last = {}


def get(url, cache, gap):
    f = os.path.join(cache, hashlib.sha1(url.encode()).hexdigest())
    if os.path.exists(f):
        return open(f, errors='replace').read()
    host = urllib.parse.urlparse(url).netloc
    for wait in (0, 10, 40):
        time.sleep(max(0, gap - (time.time() - last.get(host, 0))) + wait)
        last[host] = time.time()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'research-fetch'}), timeout=60) as r:
                body = r.read().decode('utf-8', 'replace')
            open(f, 'w').write(body)
            return body
        except Exception as e:
            err = e
    print('gave up:', url[:110], err, file=sys.stderr)
    return ''


def norm(t):
    return re.sub(r'[^a-z0-9]+', ' ', (t or '').lower()).strip()


def clean(t):
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', t or '')).strip()


def crossref(q, cache, rows=25, extra=''):
    u = 'https://api.crossref.org/works?' + urllib.parse.urlencode({
        'query.bibliographic': q, 'rows': rows,
        'select': 'DOI,title,issued,container-title,is-referenced-by-count,abstract,type,reference'}) + extra
    body = get(u, cache, 0.3)
    return json.loads(body)['message']['items'] if body else []


def cr_row(w):
    year = ((w.get('issued') or {}).get('date-parts') or [[None]])[0][0]
    return {'year': year or '', 'cited': w.get('is-referenced-by-count') or 0, 'title': clean((w.get('title') or [''])[0]),
            'venue': clean((w.get('container-title') or [''])[0]), 'where': 'https://doi.org/' + w['DOI'],
            'abstract': clean(w.get('abstract'))[:700]}


def arxiv(q, cache, rows=25):
    terms = ' AND '.join('all:' + t for t in re.findall(r'[A-Za-z0-9-]+', q) if len(t) > 2)
    body = get('https://export.arxiv.org/api/query?' + urllib.parse.urlencode(
        {'search_query': terms, 'max_results': rows, 'sortBy': 'relevance'}), cache, 3.5)
    out = []
    for e in re.findall(r'<entry>(.*?)</entry>', body, re.S):
        g = lambda tag: clean((re.search(r'<%s>(.*?)</%s>' % (tag, tag), e, re.S) or [None, ''])[1])
        out.append({'year': g('published')[:4], 'cited': '', 'title': g('title'), 'venue': 'arXiv',
                    'where': g('id').replace('http://', 'https://'), 'abstract': g('summary')[:700]})
    return out


def write(path, head, rows):
    with open(path, 'w') as f:
        f.write('\t'.join(head) + '\n')
        for r in rows:
            f.write('\t'.join(str(x).replace('\t', ' ').replace('\n', ' ') for x in r) + '\n')


def queries(qfile, out):
    cache = os.path.join(out, 'cache')
    os.makedirs(cache, exist_ok=True)
    seen, n = {}, 0
    for line in open(qfile):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        tag, q = [x.strip() for x in line.split('|', 1)]
        n += 1
        found = [cr_row(w) for w in crossref(q, cache)]
        found += [cr_row(w) for w in crossref(q, cache, extra='&filter=from-pub-date:2021')]
        found += arxiv(q, cache)
        for r in found:
            k = norm(r['title'])
            if len(k) > 12:
                seen.setdefault(k, (r, set()))[1].add(tag + ': ' + q)
        print(n, tag, q, len(found), file=sys.stderr, flush=True)
    rows = [[len(qs), r['year'], r['cited'], r['title'], r['venue'], r['where'], r['abstract'], ' \x3b '.join(sorted(qs))]
            for r, qs in sorted(seen.values(), key=lambda x: (sorted(x[1])[0], -(x[0]['cited'] or 0) if x[0]['cited'] != '' else 0))]
    write(os.path.join(out, 'neighbours-queries.tsv'), ['hits', 'year', 'cited', 'title', 'venue', 'where', 'abstract', 'queries'], rows)
    print(n, 'queries,', len(seen), 'distinct results written', file=sys.stderr)


def refs(papers_file, out):
    cache = os.path.join(out, 'cache')
    os.makedirs(cache, exist_ok=True)
    papers = tomllib.load(open(papers_file, 'rb'))['paper']
    ours, cand, resolved = set(), defaultdict(lambda: [None, set()]), []
    for p in papers:
        ours.add(norm(p.get('title')))
    for p in papers:
        t = norm(p.get('title'))
        if len(t) < 16:
            continue
        for w in crossref(p['title'][:200], cache, rows=3):
            n = norm((w.get('title') or [''])[0])
            if difflib.SequenceMatcher(None, t, n).ratio() > 0.9:
                resolved.append([p['id'], w['DOI'], len(w.get('reference') or [])])
                for r in w.get('reference') or []:
                    title = r.get('article-title') or r.get('volume-title') or ''
                    key = (r.get('DOI') or '').lower() or norm(title) or norm(r.get('unstructured'))[:90]
                    if not key or norm(title) in ours and title:
                        continue
                    c = cand[key]
                    c[0] = c[0] or {'doi': r.get('DOI') or '', 'title': clean(title or r.get('unstructured') or ''),
                                    'year': r.get('year') or '', 'venue': clean(r.get('journal-title') or '')}
                    c[1].add(p['id'])
                break
    write(os.path.join(out, 'neighbours-resolved.tsv'), ['paper', 'doi', 'references listed'], sorted(resolved))
    rows = []
    for key, (c, by) in sorted(cand.items(), key=lambda x: -len(x[1][1])):
        if len(by) < 2:
            continue
        if c['doi'] and not c['title']:
            body = get('https://api.crossref.org/works/' + urllib.parse.quote(c['doi']), cache, 0.3)
            if body:
                r = cr_row(json.loads(body)['message'])
                c.update(title=r['title'], year=r['year'], venue=r['venue'])
        rows.append([len(by), c['year'], c['title'], c['venue'], ('https://doi.org/' + c['doi']) if c['doi'] else '', ' '.join(sorted(by))])
    write(os.path.join(out, 'neighbours-refs.tsv'), ['cited by how many of ours', 'year', 'title', 'venue', 'where', 'cited by'], rows)
    print(len(resolved), 'of', len(papers), 'sources found with a reference list\x3b', len(cand), 'works cited,', len(rows), 'by two or more of ours', file=sys.stderr)


KESEN = 'https://kesen.realtimerendering.com/'
VENUES = {'sig': 'SIGGRAPH', 'siga': 'SIGGRAPH Asia', 'sca': 'SCA', 'i3d': 'I3D', 'hpg': 'HPG', 'eg': 'Eurographics',
          'egsr': 'EGSR', 'mig': 'MIG'}


def venues(terms_file, out):
    cache = os.path.join(out, 'cache')
    os.makedirs(cache, exist_ok=True)
    terms = [(a.strip(), re.compile(b.strip(), re.I)) for a, b in
             (l.split('|', 1) for l in open(terms_file) if '|' in l and not l.startswith('#'))]
    index = get(KESEN, cache, 2)
    pages = sorted(set(re.findall(r'(?:kesen\.realtimerendering\.com/|kesen/)((?:%s)(20\d\d)(?:Papers)?\.html?)' % '|'.join(VENUES), index)))
    rows, log = [], []
    for page, year in pages:
        body = get(KESEN + page, cache, 2)
        n = 0
        for m in re.finditer(r'<dt>(.*?)(?:</dt>|<dd>|<br)(.*?)(?=<dt>|$)', body, re.S | re.I):
            title = clean(m.group(1))
            if len(title) < 8:
                continue
            link = re.search(r'href\s*=\s*"([^"]+)"', m.group(1), re.I) or re.search(r'href\s*=\s*"([^"]+\.pdf|[^"]*(?:doi|arxiv)[^"]*)"', m.group(2), re.I)
            parts = [a for a, rx in terms if rx.search(title)]
            # every link the list gives for the paper, the author's own copy first
            cell = m.group(1) + m.group(2).split('<dd', 1)[0]
            also = sorted(dict.fromkeys(h for h, alt in re.findall(r'<a\s[^>]*href\s*=\s*"(http[^"]+)"[^>]*>\s*(?:<img[^>]*alt\s*=\s*"([^"]*)")?', cell, re.I)
                                        if not re.search(r'youtu|vimeo|\.mp4|\.mov|\.zip', h, re.I)),
                          key=lambda h: 0 if h.lower().split('?')[0].endswith('.pdf') else 1)
            rows.append([len(parts), ' '.join(parts), year, VENUES[re.match(r'[a-z0-9]+?(?=20)', page).group()], title,
                         link.group(1) if link else '', ' '.join(h.replace(' ', '%20') for h in also[:5])])
            n += 1
        log.append([VENUES[re.match(r'[a-z0-9]+?(?=20)', page).group()], year, KESEN + page, n])
        print(page, n, file=sys.stderr, flush=True)
    rows.sort(key=lambda r: (-r[0], r[1], r[2]))
    write(os.path.join(out, 'venues-titles.tsv'), ['parts touched', 'parts', 'year', 'venue', 'title', 'where', 'links'], rows)
    write(os.path.join(out, 'venues-walked.tsv'), ['venue', 'year', 'list', 'titles'], log)
    print(len(pages), 'lists,', len(rows), 'titles,', sum(1 for r in rows if r[0]), 'touch a part', file=sys.stderr)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('mode', choices=['queries', 'refs', 'venues'])
    parser.add_argument('input')
    parser.add_argument('out_dir')
    args = parser.parse_args()
    {'queries': queries, 'refs': refs, 'venues': venues}[args.mode](args.input, args.out_dir)
