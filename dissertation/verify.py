#!/usr/bin/env python3
"""Check the real corpus, public aggregates and publication boundary."""
import collections
import csv
import hashlib
import json
import math
from pathlib import Path
import random
import subprocess
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / 'data/vendor'))
from analyse import CLASSES, MAP, SEED, intervals
from collect import stamp
from label import read_rows


def main():
    private = HERE / 'data'
    corpus = read_rows(private / 'labelled.jsonl')
    secondary = read_rows(private / 'relabel_labels.jsonl')
    summary = json.loads((HERE / 'tables/summary.json').read_text())
    assert len(corpus) == summary['corpus_prompts']
    assert len({r['id'] for r in corpus}) == len(corpus)
    assert sum(r['is_correction'] == 'y' for r in corpus) == summary['corrections']
    assert all(r['class'] in CLASSES and r['is_correction'] in ['y', 'n'] for r in corpus)
    assert all(stamp(r['timestamp']) < stamp(summary['cutoff']) for r in corpus)
    primary = {r['id']: r for r in corpus}
    expected = random.Random(SEED).sample(sorted(primary), math.ceil(len(corpus)*.15))
    assert set(expected) == {r['id'] for r in secondary}, 'independent sample differs from seeded draw'
    assert len(secondary) == len(expected)
    assert all(r['label_model'] == 'gpt-6-luna' for r in corpus)
    assert all(r['model'] == 'gpt-6.1-sol' for r in secondary)
    table_rows = {}
    for path in (HERE / 'tables').glob('*.csv'):
        with path.open() as stream:
            reader = csv.DictReader(stream)
            forbidden = {'text', 'prompt', 'session_id', 'record_id', 'project', 'source_file',
                         'line', 'note', 'by', 'repo', 'path', 'id'}
            assert not forbidden.intersection(reader.fieldnames), path.name
            table_rows[path.name] = list(reader)
    registry = {r['gate']: r for r in table_rows['gate_registry.csv']}
    for r in corpus:
        gate_ids = [g for g in MAP[r['class']] if stamp(registry[g]['git_birth']) <= stamp(r['timestamp'])]
        assert r['covering_gate_ids'] == gate_ids
        assert r['gate_existed'] == bool(gate_ids)
        assert not r['text'].lstrip().startswith(('Another Claude session sent a message:',
                                                 '<teammate-message', '<task-notification>'))
    source_counts = collections.Counter(r['source'] for r in corpus)
    assert all(int(r['prompts']) == source_counts[r['source']] for r in table_rows['source_counts.csv'])
    assert sum(int(r['prompts']) for r in table_rows['source_month_counts.csv']) == len(corpus)
    assert sum(int(r['corrections']) for r in table_rows['month_counts.csv']) == summary['corrections']
    assert math.isclose(sum(float(r['hours']) for r in table_rows['daily_activity.csv']), summary['active_hours'])
    direct_hours = sum((b-a).total_seconds()/3600 for a, b in intervals(corpus))
    assert math.isclose(direct_hours, summary['active_hours'])
    assert math.isclose(summary['global_all']['before_hours'] + summary['global_all']['after_hours'], direct_hours)
    assert sum(int(r['before_corrections'])+int(r['after_corrections'])
               for r in table_rows['class_global_rates.csv']) == summary['corrections']
    assert sum(int(r['count']) for r in table_rows['gate_results_daily.csv']) == summary['gate_result_events']
    assert len(table_rows['gate_birth_rates.csv']) == summary['gate_count']
    assert len(table_rows['class_birth_rates.csv']) == len([c for c in CLASSES if MAP[c]])
    # Public prompt-sized strings and identifiers must not leak into text outputs.
    public = [p for p in HERE.rglob('*') if p.is_file() and 'data' not in p.relative_to(HERE).parts
              and '__pycache__' not in p.parts and p.suffix in ['.md', '.csv', '.json', '.toml', '.txt', '.py']]
    all_rows = read_rows(private / 'extracted.jsonl')
    inventory = json.loads((private / 'inventory.json').read_text())
    candidates = {r['id'] for r in all_rows if not r['exclusion']}
    primary_labels = read_rows(private / 'primary_labels.jsonl')
    assert len(candidates) == inventory['retained']
    assert {r['id'] for r in primary_labels} == candidates
    assert {r['id'] for r in primary_labels if r['include'] == 'y'} == set(primary)
    identifiers = ({r['id'] for r in all_rows} | {r['session_id'] for r in all_rows if r['session_id']}
                   | {r['record_id'] for r in all_rows if r.get('record_id')})
    raw_texts = {r['text'] for r in all_rows if len(r['text']) >= 80}
    for path in public:
        text = path.read_text()
        for identifier in identifiers:
            assert identifier not in text, f'private identifier leaked: {path.name}'
        for prompt in raw_texts:
            assert prompt not in text, f'prompt leaked: {path.name}'
    ignored = subprocess.run(['git', '-C', str(HERE.parent), 'check-ignore',
                              str(private / 'labelled.jsonl')], capture_output=True)
    assert ignored.returncode == 0
    tracked = subprocess.check_output(['git', '-C', str(HERE.parent), 'ls-files', '--cached', '--',
                                      'dissertation/data'], text=True)
    assert not tracked.strip(), 'private directory tracked or staged'
    for path in (private / 'batches').glob('*.meta.json'):
        meta = json.loads(path.read_text())
        assert meta['exit'] == 0, path.name
        assert meta['model'] in ['gpt-6-luna', 'gpt-6.1-sol']
    from PIL import Image
    figures = []
    for path in sorted((HERE / 'figures').glob('*.png')):
        with Image.open(path) as image:
            assert image.width >= 1400 and image.height >= 700
            image.verify()
        figures.append(dict(file=path.name, sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
    assert len(figures) == 4
    print(f'PASS: {len(corpus)} corpus rows, {len(secondary)} independent labels, '
          f'{len(table_rows)} aggregate CSV files, {len(figures)} valid PNG charts')
    print('PASS: sample, counts, exposure, gate dates and ignored-data boundary reconciled')
    print('PASS: no full raw text of 80+ characters or raw prompt/session identifiers in public text outputs')


if __name__ == '__main__':
    main()
