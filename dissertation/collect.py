#!/usr/bin/env python3
"""Extract private prompt evidence without copying assistant/tool payloads."""
import argparse
import collections
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tomllib

HERE = Path(__file__).resolve().parent
UTC = dt.timezone.utc
CUTOFF = '2026-11-07T13:58:00+00:00'


def stamp(value):
    try:
        d = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return d.replace(tzinfo=UTC) if d.tzinfo is None else d.astimezone(UTC)
    except (ValueError, TypeError):
        return None


def text_content(value, kind='text'):
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ''
    return '\n'.join(b.get('text', '') for b in value
                     if isinstance(b, dict) and b.get('type') == kind)


def exclusion(text, record):
    if record.get('isSidechain'):
        return 'sidechain'
    if record.get('isMeta'):
        return 'meta'
    if record.get('isCompactSummary') or record.get('isVisibleInTranscriptOnly'):
        return 'summary_or_transcript_only'
    origin = record.get('origin', {})
    if isinstance(origin, dict) and origin.get('kind') in ['task-notification', 'system', 'agent']:
        return 'nonhuman_origin'
    if record.get('promptSource') in ['system', 'suggestion_accepted']:
        return 'system_or_accepted_suggestion'
    if not text.strip():
        return 'tool_result_or_empty'
    t = text.lstrip()
    markers = ('<task-notification>', '<local-command', '<command-name>',
               '<command-message>', '<system-reminder>', '[Request interrupted',
               'This session is being continued', 'You have been resumed',
               '# AGENTS.md instructions', '<environment_context>',
               '<permissions instructions>', '<turn_aborted>', '<user_instructions>',
               '<subagent_notification>', '<agent_message>', '<INSTRUCTIONS>')
    if t.startswith(markers):
        return 'injected_or_command'
    if re.match(r'You are (?:a |an |the )?(?:read-only |implementation |research |precise |independent |bounded |coding |exploration )*(?:worker|agent|reviewer|judge|assistant)\b', t, re.I):
        return 'worker_brief'
    headings = re.findall(r'(?im)^\s*(?:#{1,6}\s+|(?:objective|task|constraints|scope|verification|deliverables|done condition|context):)', t)
    if len(t) > 2000 and len(headings) >= 3:
        return 'long_structured_brief'
    if len(t) > 20000:
        return 'long_brief_or_pasted_context'
    return None


def source_rows(root, source, host, kind, cutoff):
    stats = collections.Counter()
    files = sorted(Path(root).expanduser().rglob('*.jsonl'))
    stats['files'] = len(files)
    oldest = None
    newest = None
    for path in files:
        # Each file is streamed twice for Codex metadata, never loaded in memory.
        meta = {}
        has_events = False
        if kind == 'codex':
            with path.open(errors='replace') as stream:
                for line in stream:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    p = r.get('payload', {})
                    if r.get('type') == 'session_meta' and not meta:
                        meta = p
                    if r.get('type') == 'event_msg' and p.get('type') == 'user_message':
                        has_events = True
        session_source = meta.get('source', meta.get('thread_source'))
        nonhuman_session = kind == 'codex' and (
            isinstance(session_source, dict) or session_source in ['exec', 'subagent'])
        session_id = meta.get('id', meta.get('session_id', path.stem))
        session_brief = False
        first_text = True
        seen = set()
        with path.open(errors='replace') as stream:
            for lineno, line in enumerate(stream, 1):
                # Claude's large tool payloads are discarded before JSON parsing.
                if kind == 'claude' and not re.search(r'"type"\s*:\s*"user"', line):
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    stats['malformed_json'] += 1
                    continue
                p = r.get('payload', {})
                if kind == 'claude':
                    if r.get('type') != 'user':
                        continue
                    text = text_content(r.get('message', {}).get('content'))
                    session_id = r.get('sessionId', path.stem)
                    project = r.get('cwd', '')
                    ident = r.get('uuid')
                    reason = exclusion(text, r)
                else:
                    event = r.get('type') == 'event_msg' and p.get('type') == 'user_message'
                    fallback = (not has_events and r.get('type') == 'response_item'
                                and p.get('role') == 'user')
                    if not event and not fallback:
                        continue
                    text = p.get('message', '') if event else text_content(p.get('content'), 'input_text')
                    project = meta.get('cwd', '')
                    ident = p.get('id')
                    reason = 'headless_or_subagent_session' if nonhuman_session else exclusion(text, {})
                stats['candidate_records'] += 1
                date = stamp(r.get('timestamp'))
                if date:
                    oldest = min(oldest, date) if oldest else date
                    newest = max(newest, date) if newest else date
                if date is None:
                    reason = 'missing_timestamp'
                elif date >= cutoff:
                    reason = 'after_snapshot'
                if first_text and text.strip() and reason in [None, 'worker_brief', 'long_structured_brief', 'long_brief_or_pasted_context']:
                    first_text = False
                    session_brief = reason in ['worker_brief', 'long_structured_brief', 'long_brief_or_pasted_context']
                if session_brief and reason is None:
                    reason = 'worker_brief_session'
                key = ident or hashlib.sha256((str(date) + text).encode()).hexdigest()
                if key in seen:
                    reason = 'duplicate_in_file'
                seen.add(key)
                row = dict(source=source, host=host, session_id=session_id,
                           project=project, timestamp=date.isoformat() if date else None,
                           text=text, record_id=ident, source_file=str(path), line=lineno,
                           exclusion=reason)
                if reason:
                    stats['drop_' + reason] += 1
                else:
                    stats['retained_before_dedup'] += 1
                yield dict(type='prompt', row=row)
    yield dict(type='inventory', row=dict(source=source, host=host, root=root, kind=kind,
              counts=dict(stats), oldest=oldest.isoformat() if oldest else None,
              newest=newest.isoformat() if newest else None))


def emit(host, cutoff):
    roots = ([('/root/.claude/projects', 'h_claude_root', 'claude'),
              ('/home/gabriel/.claude/projects', 'h_claude_gabriel', 'claude'),
              ('/root/.codex/sessions', 'h_codex_root', 'codex'),
              ('/home/gabriel/.codex/sessions', 'h_codex_gabriel', 'codex')]
             if host == 'h' else [
                 ('~/.claude/projects', 'b_claude', 'claude'),
                 ('~/.codex/sessions', 'b_codex', 'codex')])
    for root, name, kind in roots:
        yield from source_rows(root, name, host, kind, cutoff)


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')
    path.chmod(0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--emit', choices=['h', 'b'])
    parser.add_argument('--cutoff', default=CUTOFF)
    parser.add_argument('--remote', default='b')
    args = parser.parse_args()
    cutoff = stamp(args.cutoff)
    if args.emit:
        for value in emit(args.emit, cutoff):
            print(json.dumps(value, ensure_ascii=False))
        return
    os.umask(0o077)
    private = HERE / 'data'
    private.mkdir(exist_ok=True, mode=0o700)
    out = private / 'extracted.jsonl'
    inventories = []
    seen = set()
    n = 0
    with out.open('w') as target:
        def accept(item):
            nonlocal n
            if item['type'] == 'inventory':
                inventories.append(item['row'])
                return
            row = item['row']
            # Same UUID or same session/timestamp/text across copied logs is one event.
            key = (row['session_id'], row['timestamp'], row['text'])
            if row['exclusion'] is None:
                if key in seen:
                    row['exclusion'] = 'duplicate_across_files'
                else:
                    seen.add(key)
                    n += 1
            row['id'] = hashlib.sha256(json.dumps(key, ensure_ascii=False).encode()).hexdigest()[:24]
            target.write(json.dumps(row, ensure_ascii=False) + '\n')
        for value in emit('h', cutoff):
            accept(value)
        proc = subprocess.Popen(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15',
                                 args.remote, 'python3', '-', '--emit', 'b', '--cutoff', args.cutoff],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=(private / 'ssh.stderr').open('w'), text=True)
        proc.stdin.write(Path(__file__).read_text())
        proc.stdin.close()
        for line in proc.stdout:
            accept(json.loads(line))
        code = proc.wait()
        if code:
            raise SystemExit(f'SSH collection failed: exit {code}, see data/ssh.stderr')
    write_json(private / 'inventory.json', dict(cutoff=cutoff.isoformat(), sources=inventories,
               retained=n, note='No assistant messages or tool result bodies copied.'))
    print(json.dumps(dict(retained=n, sources=[dict(source=x['source'], **x['counts'])
                                              for x in inventories]), indent=2))


if __name__ == '__main__':
    main()
