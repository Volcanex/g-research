# Dissertation analysis

Scripts, aggregate tables, figures and the write-up for the correction-rate study.
All raw prompts, labels, session identifiers, source paths, worker inputs and private
incident detail belong in `data/`. This directory is ignored by `.gitignore`.
Only publish approved aggregate columns. Never add `data/` with force.

`results.md` records the method, results and limitations. `rubric.md` fixes the labels.
`collect.py` extracts local and SSH sources. `label.py` runs independent model labels.
`analyse.py` rebuilds the aggregate tables, figures and results from private data.
`test_pipeline.py` checks extraction, exposure arithmetic, dates and inference.
