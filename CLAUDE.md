# g-research

**Owner:** Gabriel Penman **Host:** h **No code, no service**

Theory behind the g-system: one piece per idea, each with its sources. Research
holds the why. Brain holds what is true, skills hold how, g-levels holds what must
hold and is checked.

## Pieces

| Piece | About | Drives |
|---|---|---|
| `pieces/making-ladder.md` | Levels of making (L-1 to L4), gates, charters, design-gate saturation, critiques | Volcanex/g-levels, the g-levels skill, gabrielpenman.com/charter |

## Elsewhere

- **ANS** (`/home/gabriel/ans`, private): an experimental research instrument with its
  own verified literature in `lit/`. It stays in its repo because code, runs and
  literature belong together. List new threads here, one row each.

## Writing a piece

- Cite only sources someone actually checked, and mark second-hand ones.
- Mark claims as established, argued or conjecture.
- No private incident detail: the repo is public. Link to the system it drives.
- No em dashes, semicolons or middle dots.

## Knowledge

`knowledge/` holds the thesis literature tooling. Run scripts with `python3 -I`.
The data root is `G_RESEARCH_DATA`, default `/home/gabriel/g-research-data`,
set once in `knowledge/common.py`. Original files, extracted text and retrieval
indexes stay outside this public repo. A library folder is `library/<id>/`, with
the full record id, `source.pdf` or `page.html`, `text.txt` and `meta.json`.

Sweep reports belong in `sweeps/<letter>-<name>-<date>.txt` under the data root.
Rows have nine tab separated fields: rank from 0 to 3, NEW or KNOWN, kind, year,
authors, title, URL, reading depth and note. Every row stays in the report.
`sweeps.py` takes ranks 2 and 3, plus lower rows named with a reason in
`knowledge/raised.txt`. Reading goes to picks and code or data goes to tools.
`knowledge/terms.txt` maps titles, then notes, to thesis parts. It has no engine
experiment mapping.

```sh
python3 -I knowledge/sweeps.py picks > /tmp/research-picks.txt
python3 -I knowledge/shelve.py /tmp/research-picks.txt > knowledge/shelf.toml
python3 -I knowledge/sweeps.py tools > knowledge/tools.toml
python3 -I knowledge/fetch_library.py knowledge/shelf.toml
python3 -I knowledge/fetch_library.py knowledge/shelf.toml --deepen
python3 -I knowledge/fetch_library.py knowledge/shelf.toml --settle
python3 -I knowledge/fetch_library.py knowledge/shelf.toml --toml > knowledge/library.toml
python3 -I knowledge/check.py --stats
```

Pass `knowledge/terms.txt` and explicit sweep files to process a subset. Keep file
order stable so shelf ids stay stable. `shelve.py` reads the existing bibliography
and this repo's `pieces/making-ladder.md` Sources at runtime to exclude known
titles and addresses. It does not copy the bibliography or treat known as read.
`--papers FILE` adds a known paper table. `--tables FILE[,FILE...]` resolves picks
without addresses against tables from `neighbours.py`.

The fetcher keeps the engine's open-copy, linked PDF and archive fallbacks. It
needs `curl`, `pdftotext` and `pdfinfo`, plus the existing BeautifulSoup and lxml
dependencies for HTML. Every downloaded PDF is checked against the record title.
`--file ID PATH ADDRESS` takes a browser-saved copy with the same title check.
`--alt ID ADDRESS`, `--recheck`, `--suspect` and `--drop ID WHY` inspect or replace
copies. `--settle` records why a find could not be held. A title match is a filter,
so readers must still report a wrong work when they find one.

```sh
python3 -I knowledge/read.py briefs harness /tmp/research-reading
python3 -I knowledge/read.py check /tmp/research-reading --mark
python3 -I knowledge/index.py build
python3 -I knowledge/index.py ask "What evidence supports verification loops?" --tokens 8000
python3 -I knowledge/search.py build
python3 -I knowledge/search.py ask "What evidence supports verification loops?" --scope library --k 10 --json
```

`read.py briefs` uses shelf records and an optional `knowledge/papers.toml`.
`--records FILE[,FILE...]` selects other records. It groups held texts by thesis
part, up to three papers and about 26,000 words per job. Sources over 50,000 words
are reported and left for a separate reading. Abstract pages are excluded. Run each brief as a reading job and
save its final report as `out-NN.txt` in the output directory. The checker writes
`readings.json`, tests every quote against the original extracted text and exits
1 on a missing quote or no paper reports. `--mark` sets reading depth only when a
paper reports a full reading and every quote is found. This does not promote a
shelf find or establish its claims.

`index.py` calls the installed `jselect`, first on PATH, then the host's existing
installation. Build indexes every library `text.txt` into `library.jselect` in the
data root. Ask uses local lexical retrieval with source links and no key. Its
token budget counts bytes, conservatively, to avoid a tokenizer download. It
selects passages rather than answering the question or reading a paper in full.
Rebuild after library changes.

`search.py` is the passage search agents should use first. It cuts every library
`text.txt` and the working notes in the data root into 280-word passages, and
ranks them with SQLite FTS5 and static embeddings (POTION-base-8M through
sqlite-vec), fused two to one in favour of the lexical rank. `--scope library`
is for primary evidence, `--scope notes` for working notes, `--source s.<id>`
for one paper, and `--json` returns source id, title, year, path and character
offsets for each hit. It needs the venv and cached model under `search/` in the
data root and downloads nothing. The index stays in the data root. Builds are
incremental, so rebuild after any change. On the development questions the
lexical rank alone did as well as the fused one, and paraphrased questions found
their source in the top ten seven times in ten, so a miss is not evidence of
absence. A passage is a place to start reading and proves no claim.

`neighbours.py` retains the engine's search, shared-reference count and graphics
venue walker. The venue walker applies the thesis terms. This is not a thesis
venue survey. Responses are cached in the output directory. Query files have one
`part | search phrase` per line. All scripts expose `--help`.

```sh
python3 -I knowledge/neighbours.py queries /tmp/queries.txt /tmp/research-wider
python3 -I knowledge/neighbours.py refs knowledge/papers.toml /tmp/research-refs
python3 -I knowledge/neighbours.py venues knowledge/terms.txt /tmp/research-venues
```

`check.py` checks whichever TOML records are present. It validates shelf metadata,
holdings, tool licences and any links, with no requirement for ideas or decisions
files. Every shelf record needs a holding or a closed record with a reason in
`library.toml`. `--stats` prints counts. The engine's optional checks still apply
if graph records are later supplied. Engine design excerpts, kill-list mapping
and the `readings.toml` selection workflow are omitted. Quote checking lives in
`read.py`.
