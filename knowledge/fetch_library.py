#!/usr/bin/env python3
"""Bring every source in papers.toml in house: the original file plus plain text.

  python3 -I fetch_library.py papers.toml LIBRARY_DIR            fetch what is missing
  python3 -I fetch_library.py papers.toml LIBRARY_DIR --deepen   go past landing pages to the paper
  python3 -I fetch_library.py papers.toml LIBRARY_DIR --alt ID ADDRESS   try another home for one source
  python3 -I fetch_library.py shelf.toml LIBRARY_DIR --file ID PATH ADDRESS   take a copy a browser saved from ADDRESS
  python3 -I fetch_library.py papers.toml,shelf.toml LIBRARY_DIR --suspect   list held PDFs that may be the wrong work
  python3 -I fetch_library.py shelf.toml LIBRARY_DIR --drop ID WHY            throw out a copy a reader found to be wrong
  python3 -I fetch_library.py papers.toml LIBRARY_DIR --recheck  throw out shelf copies that are another paper
  python3 -I fetch_library.py papers.toml LIBRARY_DIR --settle   close shelf finds that could not be had
  python3 -I fetch_library.py papers.toml LIBRARY_DIR --toml     print library.toml

The first argument may name several files with commas, papers.toml,shelf.toml.

LIBRARY_DIR defaults to G_RESEARCH_DATA/library.
One folder per source, named after its full id. Inside: the original (source.pdf or page.html),
text.txt and meta.json. The folder lives outside the repo because the files are other
people's work. Only library.toml (ids, sizes, hashes) is committed.

Downloaded files are untrusted. Run with -I, from outside the library folder.
A bot wall is left alone. The fallbacks are other homes for the same work: the PDF a
landing page links to, an open copy, or the Internet Archive's copy of the same address.
"""
import argparse, hashlib, json, os, re, shutil, subprocess, sys, threading, time, tomllib, unicodedata
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import LIB

UA = 'Mozilla/5.0 (X11\x3b Linux x86_64\x3b rv:128.0) Gecko/20100101 Firefox/128.0'
WALL = re.compile(rb'captcha|cf-chl|Just a moment\.\.\.|Access Denied|unusual traffic|Enable JavaScript and cookies', re.I)
VIDEO = re.compile(r'youtube\.com|youtu\.be|wayback-fakeurl\.archive\.org/yt|vimeo\.com')
SLOW = {'arxiv.org': 4, 'export.arxiv.org': 4, 'web.archive.org': 4, 'raw.githubusercontent.com': 0.3, 'api.semanticscholar.org': 4}
locks, last = {}, {}
guard = threading.Lock()


def candidates(url):
    """Addresses to try for one source, best first."""
    u = urlparse(url)
    out = []
    m = re.match(r'/abs/(.+)', u.path)
    if 'arxiv.org' in u.netloc and m:
        out.append('https://arxiv.org/pdf/' + m.group(1))
    if u.netloc == 'github.com':
        p = [x for x in u.path.split('/') if x]
        if len(p) >= 5 and p[2] == 'blob':
            out.append('https://raw.githubusercontent.com/%s/%s/%s' % (p[0], p[1], '/'.join(p[3:])))
        elif len(p) == 2:
            for n in ('README.md', 'readme.md', 'README.rst', 'README', 'README.txt'):
                out.append('https://raw.githubusercontent.com/%s/%s/HEAD/%s' % (p[0], p[1], n))
    m = re.match(r'/documentation/(.+)', u.path)
    if u.netloc == 'developer.apple.com' and m:
        # the page is drawn by script from this data file, which holds the whole text
        out.append('https://developer.apple.com/tutorials/data/documentation/%s.json' % m.group(1).rstrip('/'))
    out.append(url)
    return out


def curl(url, dest):
    host = urlparse(url).netloc
    with guard:
        lock = locks.setdefault(host, threading.Lock())
    with lock:
        wait = SLOW.get(host, 1.0) - (time.time() - last.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        r = subprocess.run(['curl', '-sS', '-L', '--compressed', '--max-time', '120', '--max-filesize', '90000000',
                            '--proto', '=http,https', '-A', UA, '-o', dest, '-w', '%{http_code}', url],
                           capture_output=True, text=True)
        last[host] = time.time()
    return r.stdout.strip()[-3:]


def to_text(d, kind):
    out = os.path.join(d, 'text.txt')
    if kind == 'pdf':
        subprocess.run(['pdftotext', '-enc', 'UTF-8', os.path.join(d, 'source.pdf'), out], capture_output=True, timeout=300)
    elif kind == 'html':
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(open(os.path.join(d, 'page.html'), 'rb').read(), 'lxml')
        for t in soup(['script', 'style', 'noscript', 'svg', 'nav', 'footer', 'form', 'iframe']):
            t.decompose()
        whole = (soup.body or soup).get_text('\n')
        part = soup.find('article') or soup.find('main')
        part = part.get_text('\n') if part else ''
        # the marked-up article only when it really is most of the page
        text = re.sub(r'\n\s*\n+', '\n\n', part if len(part) > 0.6 * len(whole) else whole)
        store = soup.find(attrs={'data-partnereventstore': True})
        if store:
            # a Steam announcement is drawn by script from this attribute, which holds the post
            try:
                for ev in json.loads(store['data-partnereventstore']):
                    text += '\n\n' + ev.get('event_name', '') + '\n\n' + re.sub(r'\[/?[a-z*0-9]+(?:=[^\]]*)?\]', ' ', (ev.get('announcement_body') or {}).get('body', ''))
            except (ValueError, TypeError, AttributeError):
                pass
        open(out, 'w').write(re.sub(r'[ \t]+', ' ', text).strip() + '\n')
    else:
        os.replace(os.path.join(d, 'body'), out)
    try:
        return len(open(out, errors='replace').read().split())
    except OSError:
        return 0


def fetch(paper, root):
    d = os.path.join(root, paper['id'])
    mp = os.path.join(d, 'meta.json')
    if os.path.exists(mp) and json.load(open(mp)).get('status') in ('ok', 'video', 'held'):
        return
    os.makedirs(d, exist_ok=True)
    url = paper.get('url', '')
    meta = {'id': paper['id'], 'url': url, 'fetched': time.strftime('%Y-%m-%d'), 'status': 'error'}
    if not url.startswith('http'):
        meta['status'] = 'no-url'
    elif VIDEO.search(url):
        meta['status'] = 'video'
    else:
        tries = [(c, 'direct') for c in candidates(url)] + [('https://web.archive.org/web/2id_/' + url, 'archive')]
        body = os.path.join(d, 'body')
        for c, via in tries:
            code = curl(c, body)
            if code != '200' or not os.path.exists(body) or os.path.getsize(body) < 200:
                meta.setdefault('codes', []).append(code)
                continue
            head = open(body, 'rb').read(200000)
            if head[:5] == b'%PDF-' or b'%PDF-' in head[:1024]:
                kind, name = 'pdf', 'source.pdf'
            elif re.search(rb'<html|<!doctype html|<body', head[:4000], re.I):
                if WALL.search(head) and len(head) < 60000:
                    meta['status'] = 'blocked'
                    continue
                kind, name = 'html', 'page.html'
            else:
                kind, name = 'text', None
            raw = open(body, 'rb').read()
            meta.update(kind=kind, via=via, got=c, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
            if name:
                os.replace(body, os.path.join(d, name))
            try:
                meta['words'] = to_text(d, kind)
                meta['status'] = 'ok'
                if kind == 'pdf':
                    meta['title_ok'] = right(os.path.join(d, 'text.txt'), paper['title']) or named(os.path.join(d, 'source.pdf'), paper['title'])
                    if not meta['title_ok']:
                        meta['status'] = 'wrong-title'
                        for name in ('source.pdf', 'text.txt'):
                            if os.path.exists(os.path.join(d, name)):
                                os.remove(os.path.join(d, name))
                        continue
            except Exception as e:
                meta['status'], meta['why'] = 'error', repr(e)[:200]
            break
        if os.path.exists(body):
            os.remove(body)
    json.dump(meta, open(mp, 'w'), indent=1)
    print(meta['status'], meta.get('kind', '-'), meta.get('words', 0), paper['id'], file=sys.stderr, flush=True)


def right(path, title, reach=8000):
    """Is this text the work the title names? The title must stand near the top, word for word
    and in order. Sharing most of its words is not enough: two papers by one group on one
    subject do that. A word may differ by an ending of two letters, so a plural or a footnote
    mark stuck to a word does not turn the right paper away."""
    flat = lambda t: re.sub(r'[^a-z0-9]+', ' ', unicodedata.normalize('NFKC', t).lower()).split()
    raw = open(path, errors='replace').read(reach)
    if re.search(r'curriculum\s+vit', unicodedata.normalize('NFKC', raw[:1500]), re.I):
        return False  # an author's CV lists the title and is not the paper
    # arXiv prints its stamp up the margin, and text extraction can drop it between a title and its subtitle
    raw = re.sub(r'arXiv:\d{4}\.\d{4,5}(v\d+)?\s*\[[^\]]+\]\s*\d{1,2}\s+\w{3}\s+\d{4}', ' ', raw)
    want, head = flat(title), flat(raw)
    if not want or ''.join(want) in ''.join(head):
        return True
    near = lambda a, b: a == b or (len(a) > 3 and abs(len(a) - len(b)) <= 2 and (a.startswith(b) or b.startswith(a)))
    return any(all(near(w, h) for w, h in zip(want, head[i:i + len(want)])) for i in range(len(head) - len(want) + 1))


def inside(raw, title):
    """Is the title only part of a longer one? True when its words sit in a line with two or more
    other words before them, or two or more after them with no colon or dash between."""
    word = re.compile(r'[a-z0-9]+')
    raw, want = unicodedata.normalize('NFKC', raw).lower(), word.findall(unicodedata.normalize('NFKC', title).lower())
    toks = [(m.group(), m.start(), m.end()) for m in word.finditer(raw)]
    for i in range(len(toks) - len(want) + 1):
        if want and [t[0] for t in toks[i:i + len(want)]] == want:
            a, b = toks[i][1], toks[i + len(want) - 1][2]
            before, e = raw[raw.rfind('\n', 0, a) + 1:a], raw.find('\n', b)
            after = raw[b:e if e >= 0 else len(raw)]
            return (len(word.findall(before)) >= 2 and not re.search(r'[.:)\]]\s*$', before)) or \
                   (len(word.findall(after)) >= 2 and not re.match(r'\s*[:\u2013\u2014\-(*\u2217\u2020\d]', after))
    return False


def suspect(papers, root):
    """List held PDFs that may be the wrong work, for someone to open. It flags too much to act alone."""
    for p in papers:
        d = os.path.join(root, p['id'])
        try:
            meta = json.load(open(os.path.join(d, 'meta.json')))
            raw = open(os.path.join(d, 'text.txt'), errors='replace').read(8000)
        except OSError:
            continue
        if meta.get('status') != 'ok' or meta.get('kind') != 'pdf':
            continue
        cv = re.search(r'(^|[/_.-])(cv|vita|vitae|resume)([/_.-]|$)', (meta.get('got') or '').lower().split('?')[0])
        if cv or inside(raw, p.get('title') or ''):
            print('%s\t%s\t%s\t%s' % (p['id'], 'cv address' if cv else 'inside a longer title', p.get('title'), re.sub(r'\s+', ' ', raw[:140])))


def drop(papers, root, pid, why):
    """Throw out a held copy that a reader found to be the wrong work, and say why."""
    p = next(x for x in papers if x['id'] == pid)
    d = os.path.join(root, p['id'])
    meta = json.load(open(os.path.join(d, 'meta.json')))
    for n in ('source.pdf', 'page.html', 'text.txt'):
        if os.path.exists(os.path.join(d, n)):
            os.remove(os.path.join(d, n))
    meta.update(status='closed', words=0, note='wrong copy thrown out: ' + why)
    for k in ('kind', 'via', 'got', 'bytes', 'sha256', 'depth'):
        meta.pop(k, None)
    json.dump(meta, open(os.path.join(d, 'meta.json'), 'w'), indent=1)
    print('dropped', pid)


def named(pdf, title):
    """Does the PDF's own title field name the work? For a scan whose text comes out in the wrong order."""
    flat = lambda t: re.sub(r'[^a-z0-9]+', ' ', unicodedata.normalize('NFKC', t).lower()).split()
    r = subprocess.run(['pdfinfo', pdf], capture_output=True, text=True, errors='replace')
    m = re.search(r'^Title:\s*(.+)$', r.stdout, re.M)
    return bool(m) and len(flat(title)) > 2 and flat(m.group(1)) == flat(title)


def whole(paper, meta, d):
    """Is a shelf find itself on disk, and not just a page about it? A page is the find when the
    find is a page (a post, a set of notes). A paper or a talk needs its file, or a long page that
    carries its title and reads like a paper. A landing page or a programme listing is neither."""
    if meta['status'] != 'ok':
        return False
    if meta.get('kind') == 'pdf':
        return True
    if paper.get('form') == 'page':
        return meta.get('words', 0) >= 300
    text = os.path.join(d, 'text.txt')
    if paper.get('form') != 'paper' or meta.get('words', 0) < 5000 or not os.path.exists(text):
        return False
    body = open(text, errors='replace').read().lower()
    return right(text, paper['title'], reach=20000) and 'introduction' in body and ('references' in body or 'bibliography' in body)


def take(d, meta, url, via, floor=300, title=None, links=None):
    """Try one address for a source that came back thin or not at all. True if it now holds real text.

    The download is judged in a side folder and only moved in when it is better than what is
    held. With a title, a PDF must carry that title near its top, and a web page is only searched
    for PDF links (appended to links), because a page that mentions a paper is not the paper.
    """
    from urllib.parse import urljoin
    t = os.path.join(d, 'try')
    shutil.rmtree(t, ignore_errors=True)
    os.makedirs(t)
    body = os.path.join(t, 'body')
    if os.path.isabs(url):
        # a file saved by hand or through a browser, for a source that turns scripts away
        shutil.copy(url, body)
        code = '200'
    else:
        code = curl(url, body)
    good = False
    if code == '200' and os.path.exists(body) and os.path.getsize(body) > 2000:
        raw = open(body, 'rb').read()
        kind = None
        if b'%PDF-' in raw[:1024]:
            os.replace(body, os.path.join(t, 'source.pdf'))
            kind = 'pdf'
        elif re.search(rb'<html|<!doctype html|<body', raw[:4000], re.I):
            if not WALL.search(raw[:60000]):
                os.replace(body, os.path.join(t, 'page.html'))
                kind = 'html'
                if links is not None:
                    from bs4 import BeautifulSoup
                    hrefs = [urljoin(url, a['href']) for a in BeautifulSoup(raw, 'lxml').find_all('a', href=True)
                             if '.pdf' in a['href'].lower() or '/bitstream/' in a['href']]
                    links += sorted(dict.fromkeys(h for h in hrefs if h.startswith('http')), key=lambda h: 'supp' in h.lower())[:6]
        else:
            try:
                raw.decode('utf-8')
                kind = 'text'
            except UnicodeDecodeError:
                pass
        if kind:
            words = to_text(t, kind)
            text = os.path.join(t, 'text.txt')
            if title and kind == 'html':
                good = False
            elif words > max(floor, 0 if title and kind == 'pdf' and meta.get('kind') != 'pdf' else meta.get('words', 0) * 2):
                good = not title or right(text, title) or (kind == 'pdf' and named(os.path.join(t, 'source.pdf'), title))
            if good:
                for n in os.listdir(t):
                    os.replace(os.path.join(t, n), os.path.join(d, n))
                meta.update(kind=kind, via=via, got=url, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(), words=words, status='ok')
                if title:
                    meta['title_ok'] = True
    shutil.rmtree(t, ignore_errors=True)
    return good


def arxiv_copy(title):
    """The arXiv PDF whose title matches, if there is one."""
    from urllib.parse import urlencode
    terms = ' AND '.join('ti:' + w for w in re.findall(r'[A-Za-z0-9]+', title) if len(w) > 3)
    r = subprocess.run(['curl', '-sS', '--max-time', '40', '-A', 'research-fetch',
                        'https://export.arxiv.org/api/query?' + urlencode({'search_query': terms, 'max_results': 3})],
                       capture_output=True, text=True)
    want = re.sub(r'[^a-z0-9]+', ' ', title.lower()).strip()
    for e in re.findall(r'<entry>(.*?)</entry>', r.stdout, re.S):
        have = re.sub(r'[^a-z0-9]+', ' ', (re.search(r'<title>(.*?)</title>', e, re.S) or [0, ''])[1].lower()).strip()
        m = re.search(r'<id>https?://arxiv\.org/abs/([^<]+)</id>', e)
        if m and have and (have in want or want in have):
            return 'https://arxiv.org/pdf/' + m.group(1)


def scholar_copy(title, d):
    """The open copy a second paper index knows for this title, if it knows one. Only the title is sent."""
    from urllib.parse import urlencode
    out = os.path.join(d, 'scholar.json')
    try:
        code = curl('https://api.semanticscholar.org/graph/v1/paper/search/match?' + urlencode(
            {'query': title[:200], 'fields': 'title,openAccessPdf,externalIds'}), out)
        if code == '429':
            time.sleep(20)
            code = curl('https://api.semanticscholar.org/graph/v1/paper/search/match?' + urlencode(
                {'query': title[:200], 'fields': 'title,openAccessPdf,externalIds'}), out)
        w = (json.load(open(out)).get('data') or [{}])[0] if code == '200' else {}
    except (OSError, ValueError):
        w = {}
    finally:
        if os.path.exists(out):
            os.remove(out)
    flat = lambda t: re.sub(r'[^a-z0-9]+', ' ', (t or '').lower()).strip()
    if not w or flat(w.get('title')) != flat(title):
        return []
    found = [(w.get('openAccessPdf') or {}).get('url') or '']
    if (w.get('externalIds') or {}).get('ArXiv'):
        found.append('https://arxiv.org/pdf/' + w['externalIds']['ArXiv'])
    return [u for u in found if u.startswith('http')]


def deepen(paper, root):
    """A landing page is not the paper. Try the other homes the find came with, the PDF the page
    links to, arXiv by title, the index's open copy, then the archive."""
    from urllib.parse import urljoin
    d = os.path.join(root, paper['id'])
    mp = os.path.join(d, 'meta.json')
    if not os.path.exists(mp):
        return
    meta = json.load(open(mp))
    shelf = paper['id'].startswith('s.')
    # a shelf find is only held when the paper itself is, so its floor is a PDF or a long page
    enough = whole(paper, meta, d) if shelf else meta.get('words', 0) >= 300
    if meta['status'] in ('video', 'held', 'closed') or (meta['status'] == 'ok' and enough):
        return
    if paper.get('form') == 'page':
        # the page is the find, so its other homes are the data file behind it and the archive's copy
        if os.path.exists(os.path.join(d, 'page.html')):
            meta['words'] = to_text(d, 'html')
        for u in [c for c in candidates(meta['url']) if c != meta['url']] + ['https://web.archive.org/web/2id_/' + meta['url']]:
            if whole(paper, meta, d) or take(d, meta, u, 'archive' if 'web.archive.org' in u else 'data'):
                break
        json.dump(meta, open(mp, 'w'), indent=1)
        print(meta['status'], meta.get('kind', '-'), meta.get('words', 0), meta.get('via', '-'), paper['id'], file=sys.stderr, flush=True)
        return
    tried = list(paper.get('also', []))
    page = os.path.join(d, 'page.html')
    if os.path.exists(page):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(open(page, 'rb').read(), 'lxml')
        base = meta.get('got') or meta['url']
        for a in soup.find_all('a', href=True):
            if '.pdf' in a['href'].lower() or '/bitstream/' in a['href']:
                u = urljoin(base, a['href'])
                if u.startswith('http') and u not in tried:
                    tried.append(u)
    tried = tried[:8]
    dead = [u for u in tried if '.pdf' in u.lower()][:3]
    try:
        u = arxiv_copy(paper['title'])
        if u:
            tried.append(u)
    except Exception:
        pass
    tried += [u for u in scholar_copy(paper['title'], d) if u not in tried]
    try:
        q = 'https://api.openalex.org/works?' + __import__('urllib.parse').parse.urlencode(
            {'search': paper['title'][:200], 'per-page': 2, 'select': 'title,locations,open_access'})
        r = subprocess.run(['curl', '-sS', '--max-time', '40', '-A', 'research-fetch', q], capture_output=True, text=True)
        want = re.sub(r'[^a-z0-9]+', ' ', paper['title'].lower()).strip()
        for w in json.loads(r.stdout).get('results', []):
            have = re.sub(r'[^a-z0-9]+', ' ', (w.get('title') or '').lower()).strip()
            if have and (have in want or want in have):
                for loc in w.get('locations') or []:
                    for k in ('pdf_url', 'landing_page_url'):
                        if loc.get('is_oa') and loc.get(k) and loc[k] not in tried:
                            tried.append(loc[k])
                break
    except Exception:
        pass
    # an author's link that has died often lives on in the archive
    tried += ['https://web.archive.org/web/2id_/' + u for u in dead]
    tried.append('https://web.archive.org/web/2id_/' + meta['url'])
    n = 0
    while tried and n < 20:
        u = tried.pop(0)
        n += 1
        more = []
        via = 'archive' if 'web.archive.org' in u else ('page-link' if '.pdf' in u.lower() else 'open-copy')
        if take(d, meta, u, via, title=paper['title'], links=more):
            break
        tried = [m for m in more if m not in tried][:4] + tried
    else:
        if meta['status'] == 'ok' and not shelf and meta.get('words', 0) < 300:
            meta['status'] = 'thin'
    json.dump(meta, open(mp, 'w'), indent=1)
    print(meta['status'], meta.get('kind', '-'), meta.get('words', 0), meta.get('via', '-'), paper['id'], file=sys.stderr, flush=True)


def recheck(papers, root):
    """Throw out a shelf copy that is not the work its title names. Direct copies and copies found by following links
    are tested against the title. The find goes back to being fetched from scratch."""
    n = 0
    for p in papers:
        d = os.path.join(root, p['id'])
        mp = os.path.join(d, 'meta.json')
        if not os.path.exists(mp):
            continue
        m = json.load(open(mp))
        if m.get('kind') != 'pdf' or right(os.path.join(d, 'text.txt'), p['title']):
            continue
        for f in os.listdir(d):
            if f != 'meta.json' and os.path.isfile(os.path.join(d, f)):
                os.remove(os.path.join(d, f))
        m = {'id': m['id'], 'url': m['url'], 'status': 'error', 'words': 0, 'wrong': m.get('got', '')}
        json.dump(m, open(mp, 'w'), indent=1)
        print('thrown out', p['id'], m['wrong'][:90], file=sys.stderr)
        n += 1
    print(n, 'wrong copies thrown out', file=sys.stderr)


def settle(papers, root):
    """Shelf finds whose paper could not be had are closed, with the reason, so nothing sits as a stub."""
    n = 0
    for p in papers:
        mp = os.path.join(root, p['id'], 'meta.json')
        if not p['id'].startswith('s.') or not os.path.exists(mp):
            continue
        m = json.load(open(mp))
        if m['status'] in ('held', 'closed') or whole(p, m, os.path.join(root, p['id'])):
            continue
        if p.get('form') == 'page':
            note = 'The page could not be read by a plain fetch. Tried the address and the Internet Archive.'
        else:
            what = {'video': 'It is a talk on video and no transcript has been taken'}.get(
                m['status'], 'Only the listing or abstract page could be had' if m.get('words') else 'The address gave nothing')
            note = what + '. Tried the address, the other links the find came with, arXiv by title and the Internet Archive.'
        m.update(status='closed', note=note)
        json.dump(m, open(mp, 'w'), indent=1)
        n += 1
    print(n, 'shelf finds closed', file=sys.stderr)


def toml(papers, root):
    print('# Generated by fetch_library.py. What is held in house for each source, outside the repo,')
    print('# in G_RESEARCH_DATA/library/<id>/ as the original file plus text.txt.')
    print('# status: ok or held (the full source is on disk), closed (it could not be had, note says why),')
    print('# thin (only a stub came back and nobody has said why).')
    print('# depth: how far anyone has read it. skimmed (a research job opened it for about half a minute),')
    print('# read (read through, by the main session or a later reading job), none.')
    for p in papers:
        mp = os.path.join(root, p['id'], 'meta.json')
        m = json.load(open(mp)) if os.path.exists(mp) else {'status': 'thin'}
        status = m['status']
        if status not in ('ok', 'held', 'closed') or (status == 'ok' and m.get('words', 0) < 300 and not m.get('short')):
            status = 'thin'
        print('\n[[held]]\npaper = "%s"\nstatus = "%s"' % (p['id'], status))
        for k in ('kind', 'via', 'sha256'):
            if m.get(k):
                print('%s = "%s"' % (k, m[k]))
        for k in ('words', 'bytes'):
            if k in m:
                print('%s = %d' % (k, m[k]))
        note = m.get('note') or m.get('short')
        if note:
            print('note = "%s"' % note.replace('"', "'"))
        print('depth = "%s"' % m.get('depth', 'none' if status in ('closed', 'thin') or p['id'].startswith('s.') else 'read' if p.get('checked_by', '').startswith('Main session') else 'skimmed'))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('records', help='comma separated papers.toml,shelf.toml')
    parser.add_argument('library', nargs='?', default=LIB)
    actions = parser.add_mutually_exclusive_group()
    for flag in ('toml', 'suspect', 'recheck', 'settle', 'deepen'):
        actions.add_argument('--' + flag, action='store_true')
    actions.add_argument('--alt', nargs=2, metavar=('ID', 'ADDRESS'))
    actions.add_argument('--file', nargs=3, metavar=('ID', 'PATH', 'ADDRESS'))
    actions.add_argument('--drop', nargs=2, metavar=('ID', 'WHY'))
    args = parser.parse_args()
    papers = []
    for path in args.records.split(','):
        data = tomllib.load(open(path, 'rb'))
        papers += data.get('paper', []) + data.get('shelf', [])
    root = args.library
    if '--toml' in sys.argv:
        toml(papers, root)
    elif '--alt' in sys.argv:
        # --alt ID ADDRESS: another home for a source whose first address gave nothing
        i = sys.argv.index('--alt')
        d = os.path.join(root, sys.argv[i + 1])
        meta = json.load(open(os.path.join(d, 'meta.json')))
        p = next(x for x in papers if x['id'] == sys.argv[i + 1])
        okay = take(d, meta, sys.argv[i + 2], 'alternate', floor=60, title=p['title'])
        json.dump(meta, open(os.path.join(d, 'meta.json'), 'w'), indent=1)
        print('held' if okay else 'no', meta.get('kind', '-'), meta.get('words', 0), sys.argv[i + 1])
    elif '--file' in sys.argv:
        # --file ID PATH ADDRESS: a copy saved through a browser from ADDRESS. The title must stand near its top.
        i = sys.argv.index('--file')
        p = next(x for x in papers if x['id'] == sys.argv[i + 1])
        d = os.path.join(root, p['id'])
        os.makedirs(d, exist_ok=True)
        mp = os.path.join(d, 'meta.json')
        meta = json.load(open(mp)) if os.path.exists(mp) else {'id': p['id'], 'url': p.get('url', '')}
        meta['words'] = 0 if meta.get('status') != 'ok' else meta.get('words', 0)
        okay = take(d, meta, sys.argv[i + 2], 'browser', floor=300, title=p['title'])
        if okay:
            meta['got'] = sys.argv[i + 3]
            meta.pop('note', None)
        json.dump(meta, open(mp, 'w'), indent=1)
        print('held' if okay else 'no', meta.get('kind', '-'), meta.get('words', 0), p['id'])
    elif '--suspect' in sys.argv:
        suspect(papers, root)
    elif '--drop' in sys.argv:
        i = sys.argv.index('--drop')
        drop(papers, root, sys.argv[i + 1], sys.argv[i + 2])
    elif '--recheck' in sys.argv:
        recheck(papers, root)
    elif '--settle' in sys.argv:
        settle(papers, root)
    elif '--deepen' in sys.argv:
        with ThreadPoolExecutor(8) as ex:
            list(ex.map(lambda p: deepen(p, root), papers))
    else:
        with ThreadPoolExecutor(12) as ex:
            list(ex.map(lambda p: fetch(p, root), papers))
