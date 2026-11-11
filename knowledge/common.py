import os, re, unicodedata
from pathlib import Path
from urllib.parse import unquote, urlparse

HERE = Path(__file__).resolve().parent
DATA = os.path.abspath(os.environ.get('G_RESEARCH_DATA', '/home/gabriel/g-research-data'))
LIB = os.path.join(DATA, 'library')


def norm(text):
    text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode().lower()
    text = re.sub(r'\\[a-zA-Z]+', '', text)
    return re.sub(r'[^a-z0-9]+', ' ', text).strip()


def urlkey(url):
    url = unquote(url.strip()).rstrip('.,')
    parsed = urlparse(url)
    if 'arxiv.org' in parsed.netloc:
        return 'arxiv:' + re.sub(r'v\d+$', '', re.sub(r'^/(abs|pdf|html)/', '', parsed.path).rstrip('/').removesuffix('.pdf'))
    if parsed.netloc in ('doi.org', 'dx.doi.org'):
        return 'doi:' + parsed.path.lstrip('/').lower()
    return url.rstrip('/')


def bib_fields(entry):
    fields = {}
    for m in re.finditer(r'(?im)^\s*(title|url|doi|eprint)\s*=\s*', entry):
        start = m.end()
        if start == len(entry):
            continue
        opening = entry[start]
        if opening not in '{"':
            value = entry[start:].split(',', 1)[0].strip()
        else:
            depth, end = 1, start + 1
            while end < len(entry):
                c = entry[end]
                if c == '\\':
                    end += 2
                    continue
                if opening == '{':
                    depth += (c == '{') - (c == '}')
                elif c == '"':
                    depth = 0
                if not depth:
                    break
                end += 1
            value = entry[start + 1:end]
        fields[m.group(1).lower()] = re.sub(r'\s+', ' ', value).strip()
    return fields


def known():
    titles, urls = [], set()
    bib = Path('/home/gabriel/ans/lit/refs.bib')
    for entry in re.split(r'(?m)^@', bib.read_text())[1:]:
        fields = bib_fields(entry)
        if fields.get('title'):
            titles.append(fields['title'])
        if fields.get('url'):
            urls.add(urlkey(fields['url']))
        if fields.get('doi'):
            urls.add(urlkey('https://doi.org/' + fields['doi']))
        if fields.get('eprint'):
            urls.add(urlkey('https://arxiv.org/abs/' + fields['eprint']))
    piece = (HERE.parent / 'pieces' / 'making-ladder.md').read_text()
    sources = piece.split('## Sources', 1)[1].split('\n## ', 1)[0]
    for line in sources.splitlines():
        if not re.match(r'^\d+\. ', line):
            continue
        titles += re.findall(r'"([^"]+)"|\*([^*]+)\*', line)
        urls.update(urlkey(u.rstrip(').,')) for u in re.findall(r'https?://(?:[^\s()]|\([^\s()]*\))+', line))
    titles = [next(t for t in title if t) if isinstance(title, tuple) else title for title in titles]
    return titles, urls
