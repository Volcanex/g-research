# Reproduce the analysis

Python 3.11 or newer, Git, SSH access to b, Codex CLI and its ChatGPT sign-in are
required for the full private run. The executed model versions are gpt-6-luna
and gpt-6.1-sol. CLI authentication uses subscription access. No API fallback is
configured. The extraction snapshot is fixed at 7 November 2026, 13:58 UTC.

Run from the repository root. Install packages inside ignored data if the system
does not already have them. Matplotlib's cache also stays in data.

```sh
mkdir -p dissertation/data
chmod 700 dissertation/data
python3 -m pip install --target dissertation/data/vendor -r dissertation/requirements.txt
python3 dissertation/collect.py
python3 dissertation/label.py --phase primary --workers 4
python3 dissertation/label.py --phase relabel --workers 4
python3 dissertation/analyse.py --draws 10000
python3 -m unittest discover -s dissertation -p 'test_pipeline.py' -v
python3 dissertation/verify.py
```

`collect.py` streams the source script to Python over SSH and does not create
files on b. Excluded text is retained privately for auditing, but assistant and
tool payloads are never copied. `data/inventory.json` records scan counts and
coverage. The first Codex session metadata record owns identity and source,
so inherited parent metadata cannot turn a subagent into a human session.
Each model batch stores its exact input, schema, output, exit status
and CLI events under `data/batches/`. A completed batch is reused only when its
model, effort and input hash match. Individual annotations can also be reused
from earlier successful batches when model, effort, rubric and exact prompt text
match and those batches agree. Batch inputs and outputs remain available.
Failed or incomplete labelling aborts rather
than filling labels with keyword rules. No fallback model is substituted.

The independent sample is drawn from the final eligible primary corpus, sorted
by private identifier, with seed 20261018. Primary labels remain unchanged.
`analyse.py` derives gate availability and writes `data/labelled.jsonl`. This
private dataset preserves timestamp, host, project, session, text, labels and
the mapped gates available at that timestamp.

The analysis writes aggregate CSV and JSON under `tables/`, charts under
`figures/`, and `results.md`. `assets.toml` records chart provenance. No raw text,
row labels, identifiers, source filenames or incident notes belong in public
outputs. `verify.py` checks the private/public boundary, count reconciliation,
coverage dates and aggregate output columns. It requires the private data.
The automated text scan checks full raw text of at least 80 characters and raw
identifiers. Short phrases, excerpts and raster content need manual review.

The public synthetic unit tests run without source logs or model access.
The full run is not independently reproducible from the public repo alone.
Source logs can change or disappear. Recollecting later can change the corpus
even with the same cutoff. Preserve the private snapshot for exact reproduction.
