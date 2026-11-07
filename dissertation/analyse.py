#!/usr/bin/env python3
"""Rebuild public aggregates and figures from ignored private evidence."""
import argparse
import collections
import csv
import datetime as dt
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tomllib

HERE = Path(__file__).resolve().parent
PRIVATE = HERE / 'data'
sys.path.insert(0, str(PRIVATE / 'vendor'))
os.environ.setdefault('MPLCONFIGDIR', str(PRIVATE / 'mpl'))
import numpy as np
from collect import stamp, write_json
from label import read_rows, save_rows

UTC = dt.timezone.utc
GLOBAL = stamp('2026-10-18T00:00:00Z')
SEED = 20261018
MAP = json.loads((HERE / 'gate_map.json').read_text())
CLASSES = list(MAP)


def command(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()


def gate_registry(repo):
    mapped = {g: c for c, gates in MAP.items() for g in gates}
    rows = []
    for path in sorted((repo / 'gates').glob('*.toml')):
        current = tomllib.loads(path.read_text())
        name = path.relative_to(repo).as_posix()
        history = command(repo, 'log', '--follow', '--diff-filter=A',
                          '--format=%H|%cI|%aI', '--name-only', '--', name).splitlines()
        starts = [(i, line) for i, line in enumerate(history) if line.count('|') == 2]
        if not starts:
            raise ValueError(f'No committed birth for {name}')
        index, line = starts[-1]
        commit, committed, authored = line.split('|')
        original = next(x for x in history[index + 1:] if x.endswith('.toml'))
        initial = tomllib.loads(command(repo, 'show', f'{commit}:{original}'))
        def field(data, section, key):
            value = data.get(section, {})
            return str(value.get(key, '')) if isinstance(value, dict) else ''
        rows.append(dict(gate=current['id'], mapped_class=mapped.get(current['id'], ''),
                         git_birth=stamp(committed).isoformat(), first_commit=commit,
                         first_path=original, author_timestamp=stamp(authored).isoformat(),
                         initial_origin_date=field(initial, 'origin', 'date'),
                         initial_written_date=field(initial, 'written_against', 'date'),
                         current_origin_date=field(current, 'origin', 'date'),
                         current_written_date=field(current, 'written_against', 'date')))
    missing = set(mapped) - {r['gate'] for r in rows}
    assert not missing, f'Mapped gates missing: {missing}'
    return rows


def intervals(rows, gap=30):
    times = sorted(set(stamp(r['timestamp']) for r in rows))
    spans = []
    for a, b in zip(times, times[1:]):
        if dt.timedelta(0) < b-a < dt.timedelta(minutes=gap):
            if spans and spans[-1][1] == a:
                spans[-1] = (spans[-1][0], b)
            else:
                spans.append((a, b))
    return spans


def portions(a, b, cut=None):
    while a < b:
        midnight = dt.datetime.combine(a.date() + dt.timedelta(days=1), dt.time(), UTC)
        stop = min(b, midnight)
        if cut and a < cut < stop:
            stop = cut
        yield a.date().isoformat(), ('before' if cut and a < cut else 'after'), (stop-a).total_seconds()/3600
        a = stop


def daily_units(rows, spans, cut):
    units = {}
    for row in rows:
        t = stamp(row['timestamp'])
        key = (t.date().isoformat(), 'before' if t < cut else 'after')
        unit = units.setdefault(key, dict(hours=0.0, prompts=0, counts=collections.Counter()))
        unit['prompts'] += 1
        if row['is_correction'] == 'y':
            unit['counts'][row['class']] += 1
    for a, b in spans:
        for date, phase, hours in portions(a, b, cut):
            unit = units.setdefault((date, phase), dict(hours=0.0, prompts=0, counts=collections.Counter()))
            unit['hours'] += hours
    return {phase: [dict(date=day, **unit) for (day, p), unit in sorted(units.items()) if p == phase]
            for phase in ['before', 'after']}


def divide(a, b):
    return a/b if b else None


def quantiles(values):
    finite = np.asarray(values, dtype=float)
    finite = finite[np.isfinite(finite)]
    return tuple(float(x) for x in np.quantile(finite, [.025, .975])) if len(finite) else (None, None)


def bootstrap(units, selected, draws, rng):
    if not units:
        return np.full(draws, np.nan)
    hours = np.array([r['hours'] for r in units])
    counts = np.array([sum(r['counts'][c] for c in selected) for r in units])
    indices = rng.integers(0, len(units), size=(draws, len(units)))
    denominator = hours[indices].sum(axis=1)
    return np.divide(counts[indices].sum(axis=1), denominator,
                     out=np.full(draws, np.nan), where=denominator > 0)


def comparison(units, selected, draws, seed=SEED):
    rng = np.random.default_rng(seed)
    result = {}
    boots = []
    for phase in ['before', 'after']:
        group = units[phase]
        n = sum(sum(r['counts'][c] for c in selected) for r in group)
        h = sum(r['hours'] for r in group)
        rate = divide(n, h)
        boot = bootstrap(group, selected, draws, rng)
        lo, hi = quantiles(boot)
        boots.append(boot)
        result.update({phase + '_corrections': n, phase + '_hours': h,
                       phase + '_days': len(group), phase + '_rate': rate,
                       phase + '_lo': lo, phase + '_hi': hi})
    a, b = result['before_rate'], result['after_rate']
    change = b-a if a is not None and b is not None else None
    lo, hi = quantiles(boots[1] - boots[0])
    result.update(change=change, change_lo=lo, change_hi=hi,
                  rate_ratio=divide(b, a) if b is not None and a is not None else None,
                  valid_bootstrap_draws=int(np.isfinite(boots[1]-boots[0]).sum()))
    return result


def did(units, treated, draws):
    # Keep treated/control counts paired within each sampled day.
    rng = np.random.default_rng(SEED)
    sampled = []
    point = []
    for phase in ['before', 'after']:
        group = units[phase]
        hours = np.array([r['hours'] for r in group])
        contrasts = np.array([sum(r['counts'][c] for c in treated)-r['counts']['other'] for r in group])
        point.append(divide(float(contrasts.sum()), float(hours.sum())))
        if not group:
            sampled.append(np.full(draws, np.nan))
            continue
        indices = rng.integers(0, len(group), (draws, len(group)))
        denom = hours[indices].sum(axis=1)
        sampled.append(np.divide(contrasts[indices].sum(axis=1), denom,
                                 out=np.full(draws, np.nan), where=denom > 0))
    estimate = point[1]-point[0] if None not in point else None
    lo, hi = quantiles(sampled[1]-sampled[0])
    return dict(did=estimate, lo=lo, hi=hi,
                valid_bootstrap_draws=int(np.isfinite(sampled[1]-sampled[0]).sum()))


def kappa(a, b):
    if not a:
        return dict(n=0, agreement=None, kappa=None)
    assert len(a) == len(b)
    ca, cb = collections.Counter(a), collections.Counter(b)
    po = sum(x == y for x, y in zip(a, b))/len(a)
    pe = sum(ca[x]*cb[x] for x in ca)/(len(a)**2)
    return dict(n=len(a), agreement=po, kappa=(po-pe)/(1-pe) if pe < 1 else None)


def table(name, rows, columns=None):
    assert rows or columns
    path = HERE / 'tables' / name
    path.parent.mkdir(exist_ok=True)
    with path.open('w') as stream:
        writer = csv.DictWriter(stream, fieldnames=columns or list(rows[0]), lineterminator='\n')
        writer.writeheader()
        writer.writerows(rows)


def fmt(value, places=3):
    return 'not estimable' if value is None else f'{value:.{places}f}'


def figures(daily, comparisons, births, gate_counts, dids):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 10, 'axes.spines.top': False,
                         'axes.spines.right': False, 'axes.grid': True, 'grid.alpha': .18})
    dest = HERE / 'figures'
    dest.mkdir(exist_ok=True)
    def save(fig, name):
        fig.savefig(dest / name, dpi=180, bbox_inches='tight', metadata={
            'Title': name.removesuffix('.png').replace('_', ' '),
            'Description': 'Aggregate study data. Crafted with analyse.py. No prompt text or identifiers.'})
        plt.close(fig)
    fig, axes = plt.subplots(2, 1, figsize=(11, 6), sharex=True)
    dates = [dt.date.fromisoformat(r['date']) for r in daily]
    axes[0].bar(dates, [r['hours'] for r in daily], color='#486e8c')
    axes[0].set_ylabel('Active hours')
    axes[0].set_title('Surviving human-prompt coverage and correction rate')
    axes[1].plot(dates, [r['rate'] if r['rate'] is not None else np.nan for r in daily],
                 'o', markersize=3, color='#486e8c')
    axes[1].set_ylabel('Corrections / active hour')
    for ax in axes:
        ax.axvline(GLOBAL.date(), color='#a05242', linestyle='--', label='18 October')
    axes[0].legend()
    fig.autofmt_xdate()
    save(fig, 'daily_rates.png')
    fig, ax = plt.subplots(figsize=(10, 6))
    y = np.arange(len(comparisons))
    for phase, offset, colour in [('before', -.16, '#486e8c'), ('after', .16, '#a05242')]:
        vals = [r[phase + '_rate'] for r in comparisons]
        errors = [[max(0, v-r[phase+'_lo']) if v is not None and r[phase+'_lo'] is not None else 0
                   for v, r in zip(vals, comparisons)],
                  [max(0, r[phase+'_hi']-v) if v is not None and r[phase+'_hi'] is not None else 0
                   for v, r in zip(vals, comparisons)]]
        ax.errorbar([v if v is not None else np.nan for v in vals], y+offset,
                    xerr=errors, fmt='o', color=colour, capsize=3, label=phase.title())
    ax.set_yticks(y, [r['class'] for r in comparisons])
    ax.invert_yaxis()
    ax.set_xlabel('Corrections / active hour, 95% day-bootstrap intervals')
    ax.set_title('Before and after 18 October, pooled surviving corpus')
    ax.legend()
    save(fig, 'class_rates.png')
    fig, ax = plt.subplots(figsize=(10, 5))
    dates = sorted(set(r['date'] for r in gate_counts))
    bottom = np.zeros(len(dates))
    for result, colour in [('met', '#486e8c'), ('caught', '#a05242'), ('attempted', '#b69857'),
                           ('impossible', '#666666'), ('waived', '#a9a9a9')]:
        counts = [sum(r['count'] for r in gate_counts if r['date'] == day and r['result'] == result) for day in dates]
        ax.bar([dt.date.fromisoformat(x) for x in dates], counts, bottom=bottom, label=result, color=colour)
        bottom += counts
    ax.set_ylabel('Logged gate-result events')
    ax.set_title('Gate results are repeated events, not independent defects')
    ax.legend(ncol=5, fontsize=9)
    fig.autofmt_xdate()
    save(fig, 'gate_results.png')
    fig, ax = plt.subplots(figsize=(10, 5))
    names = list(births)
    x = [dt.datetime.fromisoformat(births[c]) for c in names]
    ax.scatter(x, range(len(names)), color='#486e8c')
    ax.set_yticks(range(len(names)), names)
    ax.invert_yaxis()
    ax.set_xlim(stamp('2026-10-08T00:00:00Z'), stamp('2026-10-23T00:00:00Z'))
    ax.axvline(GLOBAL, color='#a05242', linestyle='--')
    ax.set_title('First committed gate in each mapped class')
    fig.autofmt_xdate()
    save(fig, 'gate_births.png')


def report(summary, comparisons, class_births, dids, reliability, sources, months, registry, result_counts):
    total = summary['corpus_prompts']
    headline = summary['global_all']
    mapped = summary['global_mapped']
    direction = ('The observed direction is opposite to the predicted relative fall.'
                 if dids[0]['did'] is not None and dids[0]['did'] > 0
                 else 'The point estimate has the predicted direction, but the design cannot attribute it to gates.')
    lines = ['# Correction rates before and after gates', '',
             'This study asks whether manual corrections fall when gates cover their class. '
             'I reconstructed the surviving prompt corpus and measured corrections per active hour. '
             'The estimates describe this corpus. They do not identify a causal effect.', '',
             '## Findings', '',
             f"**Established, within the extracted corpus.** I retained {total:,} inferred human prompts, "
             f"including {summary['corrections']:,} corrections, from {summary['first_prompt']} to {summary['last_prompt']}. "
             f"The 30 minute activity rule gives {summary['active_hours']:.2f} hours on {summary['active_days']} days with positive exposure. "
             f"There are {summary['zero_exposure_days']} prompt days with no measurable active interval.", '',
             f"**Established, descriptive estimate.** All-class corrections per active hour change from "
             f"{fmt(headline['before_rate'])} before 18 October to {fmt(headline['after_rate'])} after it. "
             f"The change is {fmt(headline['change'])}, with a 95% day-bootstrap interval "
             f"[{fmt(headline['change_lo'])}, {fmt(headline['change_hi'])}].", '',
             f"**Established, descriptive estimate.** The eight mapped classes change from "
             f"{fmt(mapped['before_rate'])} to {fmt(mapped['after_rate'])} corrections per hour. "
             f"Their change is {fmt(mapped['change'])} [{fmt(mapped['change_lo'])}, {fmt(mapped['change_hi'])}].", '',
             f"**Argued, with an imperfect comparison group.** The difference-in-differences for the eight mapped classes "
             f"against the residual other class is {fmt(dids[0]['did'])} corrections per hour "
             f"[{fmt(dids[0]['lo'])}, {fmt(dids[0]['hi'])}]. A negative estimate favours a relative fall. "
             'The groups have different widths and other contains defects touched by domain gates. '
             'Colour and motion gates arrive later than 18 October. This contrast is exploratory. ' + direction, '',
             '**Conjecture.** Gates replace some manual correction. Registry existence, repeated gate results and '
             'a before/after association cannot establish that mechanism without comparable observation and '
             'evidence that the relevant gate ran before each prompt.', '',
             '## Corpus and observation gaps', '',
             f"The snapshot excludes records at or after {summary['cutoff']}, before this study task started. "
             'I scanned both named Claude roots on h and the SSH account on b, plus both local Codex roots '
             'and Codex on b. Missing roots are counted as zero files. No model-worker session from this analysis '
             'enters the snapshot. Text, project, host, timestamp and session identifier are retained privately.', '',
             '| Source | Files | Eligible prompts | First retained | Last retained |',
             '|---|---:|---:|---|---|']
    for r in sources:
        lines.append(f"| {r['source']} | {r['files']} | {r['prompts']} | {r['first'] or 'none'} | {r['last'] or 'none'} |")
    lines += ['', '| Month | Prompts | Corrections | Active hours |', '|---|---:|---:|---:|']
    for r in months:
        lines.append(f"| {r['month']} | {r['prompts']} | {r['corrections']} | {r['hours']:.2f} |")
    lines += ['', 'Retention was incomplete. The operator reports automatic deletion after roughly 30 days. '
              'Some older records survive on b, but their presence does not prove complete historical coverage. '
              'The retained series is sparse before October and h contributes no pre-intervention prompts. '
              'Deleted logs cannot be reconstructed and a missing day is not a day with no corrections. '
              'The pooled contrast changes host and project composition. The b-only contrast is a sensitivity, '
              'not a repair for selective retention.', '',
              'I drop sidechains, meta records, tool results, compacted summaries, task notifications, local '
              'command wrappers, system-origin prompts and accepted model suggestions. Codex metadata excludes '
              'headless exec and subagent sessions. I deduplicate the same session, timestamp and text across '
              'copied logs. Claude worker-prefix and long-brief rules are in [rubric.md](rubric.md). '
              'The primary annotator removes remaining clearly generated briefs. Provenance fields support '
              'authorship for newer Claude records, but cannot authenticate older text. Exclusion counts are '
              'in [corpus_exclusions.csv](tables/corpus_exclusions.csv).', '',
              '## Labels and agreement', '',
              'The fixed [rubric](rubric.md) defines a correction as an explicit mistake, unmet instruction, '
              'unwanted action or defect in an existing result. New requests and neutral questions do not '
              'count. Each prompt receives one primary class. Luna labels the corpus and Sol independently '
              'labels a simple random 15% sample, rounded up, seed 20261018. Annotators see only prompt text '
              'and a local index, without dates, hosts, project names, gate status, the claim or each other\'s labels. '
              'The prompt itself can mention dates or projects. No disagreements are adjudicated into the primary labels.', '',
              '| Measure | Sample | Raw agreement | Cohen\'s kappa |', '|---|---:|---:|---:|']
    for r in reliability:
        lines.append(f"| {r['measure']} | {r['n']} | {fmt(r['agreement'])} | {fmt(r['kappa'])} |")
    lines += ['', 'Kappa uses observed agreement adjusted for agreement expected from the two label marginals. '
              'The calculation follows the [scikit-learn definition](https://scikit-learn.org/stable/modules/generated/sklearn.metrics.cohen_kappa_score.html), '
              'implemented directly to avoid a runtime dependency. This is agreement between two models, '
              'not validation against a human gold standard. Model-specific eligibility disagreement is '
              'reported separately. Self-labelled data is a general threat in an n=1 study. Here, models '
              'assign the row labels under a researcher-defined rubric. The labels have not been checked '
              'against independent human judgements.', '',
              '## Gate timing and coverage', '',
              'The [g-levels registry](https://github.com/Volcanex/g-levels) begins with '
              '[commit 1193d87](https://github.com/Volcanex/g-levels/commit/1193d87) on 18 October 2026 at 16:54 UTC. '
              'For each current gate I inspect its first adding commit, following renames. I retain the first '
              'commit timestamp, author timestamp, original file name, initial origin date and initial '
              'written-against date in [gate_registry.csv](tables/gate_registry.csv). Incident narratives are omitted. '
              'Some written-against and origin dates predate the registry. They do not establish when a gate '
              'became available. The written-date sensitivity is therefore a different assumption, not a '
              'preferred deployment history.', '',
              'The [explicit mapping](gate_map.json) covers eight classes. Coverage is partial. A requirements '
              'gate is only a proxy for wrong-target errors and the house-system gate is a proxy for colour. '
              'I attach gate availability to each private labelled row after annotation. Availability '
              'means that at least one mapped gate existed at that timestamp. It does not mean its charter '
              'covered that project or that the sensor ran.', '',
              '| Class | First committed covering gate, UTC |', '|---|---|']
    for c, birth in class_births.items():
        lines.append(f'| {c} | {birth} |')
    lines += ['', '![Gate births](figures/gate_births.png)', '',
              '## Exposure and inference', '',
              'I sort retained human-prompt timestamps across all sources. Consecutive timestamps less than '
              '30 minutes apart define an active interval. A gap of exactly 30 minutes is excluded. I merge '
              'overlap across hosts and split intervals at UTC midnight and at each intervention timestamp. '
              'There is no added time for isolated prompts or long agent runs. Every class uses the same '
              'person-level exposure denominator. Counts on zero-exposure days remain in period numerators. '
              'They have no daily rate. Sensitivities use 15 and 60 minute thresholds.', '',
              'Before/after rates are total class corrections divided by total observed active hours, not '
              'averages of daily rates. The global cut is 18 October at 00:00 UTC. Per-class and per-gate '
              'comparisons use exact Git birth timestamps and split the exposure at that instant. Gates '
              'without a defensible mapping are listed without an estimate. Multiple gates in one class '
              'reuse that class\'s outcome, so later gate contrasts are not incremental effects.', '',
              f"Intervals use {summary['bootstrap_draws']:,} percentile bootstrap draws with seed 20261018. "
              'I resample observed UTC days with replacement separately within each period. Treatment and '
              'control counts stay paired within a day in the difference-in-differences. Days split by an '
              'exact birth contribute separate portions to the two periods. Bootstrap draws with zero '
              'exposure are undefined and excluded from percentiles. Draw counts are published. This is '
              'a day-cluster bootstrap, not a causal confidence interval. It does not address serial '
              'dependence across days or the many class and gate comparisons. '
              'Sampling uses [NumPy\'s random generator](https://numpy.org/doc/1.26/reference/random/generated/numpy.random.Generator.integers.html).', '',
              '## Before and after 18 October', '',
              '| Class | Before count | After count | Before / h | After / h | Change [95% interval] |',
              '|---|---:|---:|---:|---:|---|']
    for r in comparisons:
        lines.append(f"| {r['class']} | {r['before_corrections']} | {r['after_corrections']} | "
                     f"{fmt(r['before_rate'])} | {fmt(r['after_rate'])} | "
                     f"{fmt(r['change'])} [{fmt(r['change_lo'])}, {fmt(r['change_hi'])}] |")
    lines += ['', f"Every row has {headline['before_hours']:.2f} observed hours before the cut "
              f"and {headline['after_hours']:.2f} after it. There are {headline['before_days']} observed prompt days "
              f"before and {headline['after_days']} after. These are observations of one person, not independent participants.", '',
              '![Class rates](figures/class_rates.png)', '', '![Daily coverage and rate](figures/daily_rates.png)', '',
              '## Difference-in-differences and checks', '',
              'The contrast is (covered after minus covered before) minus (other after minus other before), '
              'in corrections per active hour. Covered pools several classes and other is a broad residual. '
              'Parallel trends are neither established nor credible from the short and incomplete pre-period. '
              'I also report only classes already represented on the registry creation day and restrict to b '
              'to show sensitivity to the later arrival of h logs.', '',
              '| Contrast | Estimate | 95% interval |', '|---|---:|---|']
    for r in dids:
        lines.append(f"| {r['analysis']} | {fmt(r['did'])} | [{fmt(r['lo'])}, {fmt(r['hi'])}] |")
    lines += ['', 'Exact class-birth comparisons are in [class_birth_rates.csv](tables/class_birth_rates.csv). '
              'Each current gate appears in [gate_birth_rates.csv](tables/gate_birth_rates.csv). '
              f"Only {summary['mapped_gate_count']} of {summary['gate_count']} gates have a specific mapping in the fixed taxonomy. "
              'The other gate rows have no estimate because their domain outcomes cannot be isolated inside other. '
              'The written-date and activity-threshold sensitivities are in '
              '[sensitivity.csv](tables/sensitivity.csv). All these estimates are descriptive.', '',
              '## Gate results over time', '',
              f"**Established, within the surviving results log.** There are {sum(result_counts.values()):,} recorded result events: "
              + ', '.join(f'{n:,} {key}' for key, n in sorted(result_counts.items())) + '. '
              'Only date, gate identifier, result category and count are exported. Notes, paths, actors and '
              'incident details remain private. Dates without entries are no recorded events, not proof '
              'that no gate ran. Repeated checks and retries are not unique prevented errors. A caught '
              'event is evidence that a sensor reported a failure, not that a human correction was prevented.', '',
              '![Gate-result events](figures/gate_results.png)', '',
              '## Limitations and interpretation', '',
              '**Established limitations.** This is n=1 with a short after window and uneven retention. '
              'Self-labelled records and researcher-chosen classes can bias interpretation. Model labels '
              'can miss implicit corrections, confuse new requests with defects and misread tone. '
              'The independent sample estimates reproducibility, not truth. Whole-brief exclusions and '
              'metadata gaps can leave or remove agent-written text. Prompt-only active hours omit '
              'isolated interactions, offline work and long assistant runs. The rate denominator is an '
              'estimate of engagement, not measured labour time. No project-level exposure is inferred.', '',
              '**Argued threats to inference.** Regression to the mean is plausible because gates were '
              'created after salient errors. Changing models, tasks, project mix, logging practices, '
              'skills and other process changes can affect rates at the same time. A residual control '
              'does not remove those changes, and domain gates contaminate it. Class existence is a '
              'weak proxy for actual treatment. Bootstrap intervals reflect observed day variation only. '
              'A fall in corrections can also mean abandonment, lower scrutiny or deleted evidence. '
              'An increase can mean harder tasks or closer scrutiny.', '',
              '**Conjecture requiring further evidence.** A prospective study with stable retention, '
              'human validation, actual gate-execution exposure and comparable task types could test '
              'whether correction work moves from Gabriel to sensors. The present series cannot '
              'separate that mechanism from the alternatives above.', '',
              '## Reproduction and publication boundary', '',
              'Read [CLAUDE.md](CLAUDE.md) for the file layout. Commands and pinned dependencies are in '
              '[run.md](run.md). The private snapshot, row labels, independent sample and model batch '
              'evidence are under ignored `data/`. Public outputs contain no prompt text, session '
              'identifiers, project-level breakdowns or incident narratives. Public tables and figures '
              'can be regenerated only by someone with the private evidence or equivalent source access. '
              'The public verification suite uses synthetic examples. No unavailable records are imputed.', '']
    (HERE / 'results.md').write_text('\n'.join(lines))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gates', type=Path, default=Path('/home/gabriel/g-levels'))
    parser.add_argument('--draws', type=int, default=10000)
    args = parser.parse_args()
    os.umask(0o077)
    inventory = json.loads((PRIVATE / 'inventory.json').read_text())
    cutoff = stamp(inventory['cutoff'])
    corpus = read_rows(PRIVATE / 'corpus.jsonl')
    relabel = read_rows(PRIVATE / 'relabel_labels.jsonl')
    assert corpus and len(relabel) == math.ceil(len(corpus)*.15)
    assert all(stamp(r['timestamp']) < cutoff for r in corpus)
    assert len({r['id'] for r in corpus}) == len(corpus)
    registry = gate_registry(args.gates)
    registry = [r for r in registry if stamp(r['git_birth']) < cutoff]
    lookup = {r['gate']: r for r in registry}
    births = {c: min(lookup[g]['git_birth'] for g in gates) for c, gates in MAP.items() if gates}
    written = {c: min(lookup[g]['initial_written_date'] or lookup[g]['git_birth'][:10]
                      for g in gates) for c, gates in MAP.items() if gates}
    for row in corpus:
        row['gate_existed'] = row['class'] in births and stamp(row['timestamp']) >= stamp(births[row['class']])
        row['covering_gate_ids'] = [g for g in MAP[row['class']] if stamp(lookup[g]['git_birth']) <= stamp(row['timestamp'])]
    save_rows(PRIVATE / 'labelled.jsonl', corpus)
    spans = intervals(corpus)
    units = daily_units(corpus, spans, GLOBAL)
    all_comp = comparison(units, CLASSES, args.draws)
    comparisons = [dict(**{'class': c}, **comparison(units, [c], args.draws)) for c in CLASSES]
    covered = [c for c in CLASSES if MAP[c]]
    mapped_comp = comparison(units, covered, args.draws)
    control_comp = comparison(units, ['other'], args.draws)
    initial = [c for c in covered if stamp(births[c]).date() <= GLOBAL.date()]
    b_rows = [r for r in corpus if r['host'] == 'b']
    short_rows = [r for r in corpus if GLOBAL-dt.timedelta(days=14) <= stamp(r['timestamp']) < GLOBAL+dt.timedelta(days=14)]
    short_units = daily_units(short_rows, intervals(short_rows), GLOBAL)
    dids = [dict(analysis='all mapped classes vs other', **did(units, covered, args.draws)),
            dict(analysis='creation-day mapped classes vs other', **did(units, initial, args.draws)),
            dict(analysis='b only, all mapped classes vs other',
                 **did(daily_units(b_rows, intervals(b_rows), GLOBAL), covered, args.draws)),
            dict(analysis='14 days either side, all mapped classes vs other',
                 **did(short_units, covered, args.draws))]
    daily = []
    by_day = collections.defaultdict(list)
    hours = collections.Counter()
    for row in corpus:
        by_day[stamp(row['timestamp']).date().isoformat()].append(row)
    for a, b in spans:
        for date, _, value in portions(a, b):
            hours[date] += value
    for date, rows in sorted(by_day.items()):
        n = sum(r['is_correction'] == 'y' for r in rows)
        daily.append(dict(date=date, prompts=len(rows), corrections=n, hours=hours[date], rate=divide(n, hours[date])))
    sources = []
    extraction = read_rows(PRIVATE / 'extracted.jsonl')
    for inv in inventory['sources']:
        rows = [r for r in corpus if r['source'] == inv['source']]
        candidates = sum(r['source'] == inv['source'] and not r['exclusion'] for r in extraction)
        ts = sorted(r['timestamp'] for r in rows)
        sources.append(dict(source=inv['source'], files=inv['counts']['files'],
                            candidate_records=inv['counts'].get('candidate_records', 0),
                            prelabel_candidates=candidates, model_excluded=candidates-len(rows), prompts=len(rows),
                            corrections=sum(r['is_correction'] == 'y' for r in rows),
                            first=ts[0] if ts else '', last=ts[-1] if ts else ''))
    months = []
    source_month = []
    first_month = min(r['timestamp'][:7] for r in corpus)
    last_month = max(r['timestamp'][:7] for r in corpus)
    year, month_number = map(int, first_month.split('-'))
    calendar_months = []
    while f'{year:04d}-{month_number:02d}' <= last_month:
        calendar_months.append(f'{year:04d}-{month_number:02d}')
        month_number += 1
        if month_number == 13:
            year, month_number = year+1, 1
    for month in calendar_months:
        rows = [r for r in corpus if r['timestamp'].startswith(month)]
        months.append(dict(month=month, prompts=len(rows), corrections=sum(r['is_correction'] == 'y' for r in rows),
                           hours=sum(r['hours'] for r in daily if r['date'].startswith(month))))
        for source in sources:
            selected = [r for r in rows if r['source'] == source['source']]
            source_month.append(dict(source=source['source'], month=month, prompts=len(selected),
                                     corrections=sum(r['is_correction'] == 'y' for r in selected)))
    original = {r['id']: r for r in corpus}
    assert len({r['id'] for r in relabel}) == len(relabel)
    reliability = []
    for measure, function in [('correction', lambda r: r['is_correction']),
                               ('class', lambda r: r['class']),
                               ('joint correction/class', lambda r: (r['is_correction'], r['class']))]:
        reliability.append(dict(measure=measure, **kappa([function(original[r['id']]) for r in relabel],
                                                       [function(r) for r in relabel])))
    both = [r for r in relabel if r['is_correction'] == original[r['id']]['is_correction'] == 'y']
    reliability.append(dict(measure='class among joint corrections',
                            **kappa([original[r['id']]['class'] for r in both], [r['class'] for r in both])))
    reliability.append(dict(measure='eligibility on retained sample',
                            **kappa(['y']*len(relabel), [r['include'] for r in relabel])))
    confusions = []
    pairs = collections.Counter((original[r['id']]['is_correction'], r['is_correction']) for r in relabel)
    for (a, b), n in sorted(pairs.items()):
        confusions.append(dict(primary=a, independent=b, count=n))
    class_rates = []
    for c, birth in births.items():
        class_rates.append(dict(**{'class': c}, birth=birth,
                                **comparison(daily_units(corpus, spans, stamp(birth)), [c], args.draws)))
    gate_rates = []
    for gate in registry:
        row = dict(gate=gate['gate'], mapped_class=gate['mapped_class'], birth=gate['git_birth'])
        row.update(comparison(daily_units(corpus, spans, stamp(gate['git_birth'])), [gate['mapped_class']], args.draws)
                   if gate['mapped_class'] else {k: None for k in all_comp})
        gate_rates.append(row)
    sensitivity = []
    sensitivity.append(dict(analysis='14_days_either_side_all', **comparison(short_units, CLASSES, args.draws)))
    for gap in [15, 30, 60]:
        u = daily_units(corpus, intervals(corpus, gap), GLOBAL)
        sensitivity.append(dict(analysis=f'gap_{gap}_minutes_all', **comparison(u, CLASSES, args.draws)))
    for c, birth in written.items():
        sensitivity.append(dict(analysis='initial_written_date_' + c,
                                **comparison(daily_units(corpus, spans, stamp(birth)), [c], args.draws)))
    private_results = []
    result_counts = collections.Counter()
    gate_daily = collections.Counter()
    malformed_results = 0
    for line in (args.gates / 'var/results.jsonl').open():
        try:
            row = json.loads(line)
        except ValueError:
            malformed_results += 1
            continue
        t = stamp(row.get('ts'))
        if t is None or t >= cutoff:
            continue
        if row.get('result') not in ['met', 'caught', 'attempted', 'impossible', 'waived']:
            continue
        # Keep only non-narrative fields even in the private results snapshot.
        item = dict(ts=t.isoformat(), gate=row['gate'], result=row['result'])
        private_results.append(item)
        result_counts[row['result']] += 1
        gate_daily[(t.date().isoformat(), row['gate'], row['result'])] += 1
    save_rows(PRIVATE / 'gate_results.jsonl', private_results)
    gate_counts = [dict(date=d, gate=g, result=r, count=n) for (d, g, r), n in sorted(gate_daily.items())]
    exclusions = []
    counts = collections.Counter()
    for r in extraction:
        if r['exclusion']:
            counts[(r['source'], r['exclusion'])] += 1
    original_sources = {r['id']: r['source'] for r in extraction if not r['exclusion']}
    for r in read_rows(PRIVATE / 'primary_labels.jsonl'):
        if r['include'] == 'n':
            counts[(original_sources[r['id']], 'model_ineligible')] += 1
    exclusions = [dict(source=s, reason=r, count=n) for (s, r), n in sorted(counts.items())]
    exclusions += [dict(source=r['source'], reason='malformed_json', count=r['counts']['malformed_json'])
                   for r in inventory['sources'] if r['counts'].get('malformed_json')]
    summary = dict(cutoff=inventory['cutoff'], corpus_prompts=len(corpus),
                   corrections=sum(r['is_correction'] == 'y' for r in corpus),
                   first_prompt=min(r['timestamp'] for r in corpus), last_prompt=max(r['timestamp'] for r in corpus),
                   active_hours=sum(hours.values()), active_days=sum(r['hours'] > 0 for r in daily),
                   zero_exposure_days=sum(r['hours'] == 0 for r in daily), bootstrap_draws=args.draws,
                   primary_model='gpt-6-luna', independent_model='gpt-6.1-sol', seed=SEED,
                   gate_revision=command(args.gates, 'rev-parse', 'HEAD'), gate_count=len(registry),
                   mapped_gate_count=sum(bool(r['mapped_class']) for r in registry),
                   gate_result_events=sum(result_counts.values()), malformed_result_rows=malformed_results,
                   corpus_sha256=hashlib.sha256((PRIVATE / 'corpus.jsonl').read_bytes()).hexdigest(),
                   label_sample_sha256=hashlib.sha256((PRIVATE / 'relabel_labels.jsonl').read_bytes()).hexdigest(),
                   global_all=all_comp, global_mapped=mapped_comp, global_control=control_comp,
                   reliability=reliability, did=dids)
    table('source_counts.csv', sources)
    table('source_month_counts.csv', source_month)
    table('month_counts.csv', months)
    table('corpus_exclusions.csv', exclusions)
    table('daily_activity.csv', daily)
    table('class_global_rates.csv', comparisons)
    table('group_global_rates.csv', [dict(group='all', **all_comp),
                                    dict(group='mapped', **mapped_comp),
                                    dict(group='residual_other', **control_comp)])
    table('class_birth_rates.csv', class_rates)
    table('gate_registry.csv', registry)
    table('gate_birth_rates.csv', gate_rates)
    table('difference_in_differences.csv', dids)
    table('sensitivity.csv', sensitivity)
    table('agreement.csv', reliability)
    table('correction_confusion.csv', confusions)
    table('gate_results_daily.csv', gate_counts)
    write_json(HERE / 'tables/summary.json', summary)
    versions = {p: importlib.metadata.version(p) for p in ['numpy', 'matplotlib']}
    write_json(PRIVATE / 'analysis_environment.json', dict(python=sys.version, packages=versions))
    figures(daily, comparisons, births, gate_counts, dids)
    report(summary, comparisons, births, dids, reliability, sources, months, registry, result_counts)
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
