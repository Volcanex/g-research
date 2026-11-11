#!/usr/bin/env python3
"""Check the thesis literature records: links resolve, every idea has a source, every paper was
opened by a research job, and nothing floats: each paper bears on an idea or gate, each idea
links to something, each decision is answered. A claim is a source an earlier round said
existed that no search found: it must say where it was looked for, what was read in its
place and which idea it was about. dropped means the content went with it. found means the
content was real and was read somewhere else. library.toml says what is held in house for
each paper: every paper needs one record, a closed one needs a note saying why, and a thin
one (a stub nobody explained) is a fault. shelf.toml holds what the wider sweeps turned up
and nobody has read yet: it says who found each one and why it was kept, it is held in house
like a paper, and nothing may draw on it until a reading job moves it into a report.
tools.toml lists code, tools and data that could help, each with its licence as far as it was
read and the gates it would serve: none may be adopted with its licence unread.
Exit 0 clean, 1 faults. --stats prints counts."""
import argparse, pathlib, sys, tomllib

HERE = pathlib.Path(__file__).parent
KINDS = ('paper', 'idea', 'decision', 'gate', 'claim', 'shelf', 'tool')
LINKS = ('supports', 'replaces', 'conflicts', 'protects', 'from', 'instead', 'about')
HOW = {'backs', 'warns', 'background', 'ruled-out'}
STATUS = {'paper': {'verified'},
          'idea': {'adopted', 'candidate', 'rejected', 'parked'},
          'decision': {'standing', 'changed'},
          'gate': {'theoretical', 'sensed'},
          'claim': {'dropped', 'found'},
          'shelf': {'unread'},
          'tool': {'candidate', 'adopted', 'rejected'}}


def load():
    nodes = {}
    faults = []
    links = []
    held = []
    for path in sorted(HERE.glob('*.toml')):
        data = tomllib.loads(path.read_text())
        links += data.get('link', [])
        held += data.get('held', [])
        for kind in KINDS:
            for n in data.get(kind, []):
                n['kind'] = kind
                n['file'] = path.name
                if n.get('id') in nodes:
                    faults.append(f"{path.name}: duplicate id {n.get('id')}")
                nodes[n.get('id')] = n
    return nodes, faults, links, held


def check(nodes, faults, links, held):
    have = {}
    for h in held:
        where = f"library.toml: {h.get('paper')}"
        if nodes.get(h.get('paper'), {}).get('kind') not in ('paper', 'shelf'):
            faults.append(f"{where}: no such paper or shelf entry")
        if h.get('paper') in have:
            faults.append(f"{where}: held twice")
        have[h.get('paper')] = h
        if h.get('status') not in ('ok', 'held', 'closed'):
            faults.append(f"{where}: status {h.get('status')!r}: only a stub is held and nobody has said why")
        if h.get('status') == 'closed' and not h.get('note'):
            faults.append(f"{where}: a closed source needs a note saying why it could not be had")
        if h.get('depth') not in ('none', 'skimmed', 'read'):
            faults.append(f"{where}: depth {h.get('depth')!r} not in none, skimmed, read")
    for i, n in nodes.items():
        if n['kind'] in ('paper', 'shelf') and i not in have:
            faults.append(f"{n['file']}: {i}: nothing in library.toml, run fetch_library.py")
    touched = set()
    for l in links:
        where = f"links.toml: {l.get('paper')} -> {l.get('idea')}"
        if nodes.get(l.get('paper'), {}).get('kind') != 'paper':
            faults.append(f"{where}: no such paper")
        if nodes.get(l.get('idea'), {}).get('kind') != 'idea':
            faults.append(f"{where}: no such idea")
        if l.get('how') not in HOW:
            faults.append(f"{where}: how {l.get('how')!r} not in {sorted(HOW)}")
        if not l.get('why'):
            faults.append(f"{where}: link needs why")
        touched |= {l.get('paper'), l.get('idea')}
    for i, n in nodes.items():
        for link in LINKS:
            for t in n.get(link, []):
                touched |= {i, t}
    for i, n in nodes.items():
        if n['kind'] not in ('gate', 'shelf', 'tool') and i not in touched:
            faults.append(f"{n['file']}: {i}: linked to nothing")
    for i, n in nodes.items():
        k = n['kind']
        where = f"{n['file']}: {i}"
        if not i or not i.startswith(k[0] + '.'):
            faults.append(f"{where}: id must start with '{k[0]}.'")
        if n.get('status') not in STATUS[k]:
            faults.append(f"{where}: status {n.get('status')!r} not in {sorted(STATUS[k])}")
        for link in LINKS:
            for t in n.get(link, []):
                if t not in nodes:
                    faults.append(f"{where}: {link} -> {t} does not exist")
        if k == 'paper':
            for f in ('authors', 'title'):
                if not n.get(f):
                    faults.append(f"{where}: paper needs {f}")
            if not (n.get('year') or n.get('version')):
                faults.append(f"{where}: paper needs a year, or a version for an undated source")
            if n['status'] == 'verified' and not (n.get('url') and n.get('checked_by')):
                faults.append(f"{where}: a verified paper needs url and checked_by")
        if k == 'shelf':
            for f in ('title', 'url', 'part', 'found_by', 'why'):
                if not n.get(f):
                    faults.append(f"{where}: a shelf entry needs {f}")
            if n.get('form') not in ('paper', 'talk', 'page'):
                faults.append(f"{where}: form must be paper, talk or page")
        if k == 'tool':
            for f in ('name', 'url', 'form', 'licence', 'part', 'found_by', 'use'):
                if not n.get(f):
                    faults.append(f"{where}: a tool needs {f}")
            if n['status'] == 'adopted' and n['licence'] == 'not read':
                faults.append(f"{where}: adopted with its licence not read")
        if k == 'idea':
            for t in n.get('from', []):
                if nodes.get(t, {}).get('kind') == 'shelf':
                    faults.append(f"{where}: draws on {t}, which nobody has read yet")
            if not n.get('text'):
                faults.append(f"{where}: idea needs text")
            src = n.get('from', [])
            if not src and not n.get('own'):
                faults.append(f"{where}: idea needs a from = [papers] or own = true")
            if n['status'] == 'rejected' and not n.get('why'):
                faults.append(f"{where}: a rejected idea needs why")
            if n['status'] == 'adopted':
                papers = [nodes[t] for t in src if t in nodes and nodes[t]['kind'] == 'paper']
                if papers and not any(p['status'] == 'verified' for p in papers):
                    faults.append(f"{where}: adopted but none of its papers is verified")
        if k == 'decision':
            if not (n.get('text') and n.get('date')):
                faults.append(f"{where}: decision needs text and date")
            if n['status'] == 'changed' and not n.get('why'):
                faults.append(f"{where}: a changed decision needs why")
        if k == 'claim':
            for f in ('row', 'text', 'searched', 'found'):
                if not n.get(f):
                    faults.append(f"{where}: claim needs {f}")
            if not [t for t in n.get('instead', []) if nodes.get(t, {}).get('kind') == 'paper']:
                faults.append(f"{where}: claim needs instead = [papers that were read in its place]")
            if not [t for t in n.get('about', []) if nodes.get(t, {}).get('kind') == 'idea']:
                faults.append(f"{where}: claim needs about = [ideas]")
        if k == 'gate':
            for f in ('principle', 'sensor', 'number', 'reason'):
                if n.get(f) in (None, ''):
                    faults.append(f"{where}: gate needs {f}")
    return faults


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stats', action='store_true')
    args = parser.parse_args()
    nodes, faults, links, held = load()
    faults = check(nodes, faults, links, held)
    if args.stats:
        for k in KINDS:
            ns = [n for n in nodes.values() if n['kind'] == k]
            by = {}
            for n in ns:
                by[n.get('status')] = by.get(n.get('status'), 0) + 1
            print(f"{k}: {len(ns)} {by}")
        print(f"link: {len(links)}")
        for f in ('status', 'depth'):
            by = {}
            for h in held:
                by[h.get(f)] = by.get(h.get(f), 0) + 1
            print(f"held by {f}: {by}")
        print(f"held words: {sum(h.get('words', 0) for h in held if h.get('status') != 'closed')}")
    for f in faults:
        print('FAULT', f)
    print('ok' if not faults else f'{len(faults)} faults')
    return 1 if faults else 0


if __name__ == '__main__':
    sys.exit(main())
