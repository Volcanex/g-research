import datetime as dt
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import numpy as np
from analyse import comparison, daily_units, did, intervals, kappa, portions
from collect import exclusion, source_rows, stamp, text_content
from label import validate


def row(time, correction='n', cls='other', host='h'):
    return dict(timestamp='2026-10-17T' + time + '+00:00',
                is_correction=correction, **{'class': cls}, host=host)


class ExtractionTests(unittest.TestCase):
    def extract_fixture(self, records, kind):
        private = Path(__file__).resolve().parent / 'data'
        private.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=private) as folder:
            (Path(folder) / 'test.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in records))
            return [r['row'] for r in source_rows(folder, 'fixture', 'h', kind,
                                                 stamp('2026-11-01T00:00:00Z')) if r['type']=='prompt']

    def test_worker_after_injected_wrapper_excludes_whole_session(self):
        records = [dict(type='user', timestamp='2026-10-17T10:00:00Z', sessionId='synthetic',
                        message=dict(content=text)) for text in [
                            '<command-name>run</command-name>',
                            'You are an implementation worker. Task: build.', 'Please continue']]
        rows = self.extract_fixture(records, 'claude')
        self.assertEqual([r['exclusion'] for r in rows],
                         ['injected_or_command', 'worker_brief', 'worker_brief_session'])

    def test_inherited_metadata_cannot_turn_a_subagent_into_a_human(self):
        records = [dict(type='session_meta', payload=dict(id='child', source={'subagent': {}})),
                   dict(type='session_meta', payload=dict(id='parent', source='vscode')),
                   dict(type='response_item', timestamp='2026-10-17T10:00:00Z',
                        payload=dict(role='user', content=[dict(type='input_text', text='Read the code')]))]
        rows = self.extract_fixture(records, 'codex')
        self.assertEqual(rows[0]['exclusion'], 'headless_or_subagent_session')
        self.assertEqual(rows[0]['session_id'], 'child')

    def test_tool_result_is_not_a_prompt(self):
        self.assertEqual(text_content([{'type': 'tool_result', 'content': 'private'}]), '')
        self.assertEqual(text_content([{'type': 'text', 'text': 'human'},
                                       {'type': 'tool_result', 'content': 'private'}]), 'human')

    def test_origin_and_generated_records_are_excluded(self):
        cases = [({'isSidechain': True}, 'sidechain'), ({'isMeta': True}, 'meta'),
                 ({'isCompactSummary': True}, 'summary_or_transcript_only'),
                 ({'origin': {'kind': 'task-notification'}}, 'nonhuman_origin'),
                 ({'promptSource': 'suggestion_accepted'}, 'system_or_accepted_suggestion')]
        for metadata, reason in cases:
            self.assertEqual(exclusion('Do this task', metadata), reason)
        self.assertEqual(exclusion('<task-notification>finished</task-notification>', {}), 'injected_or_command')
        self.assertEqual(exclusion('You are an implementation worker. Task: build.', {}), 'worker_brief')
        self.assertIsNone(exclusion('The heading still overlaps the button', {'origin': {'kind': 'human'}}))

    def test_long_structured_brief(self):
        text = '# Objective\n' + 'x' * 2100 + '\n# Scope\n# Verification\n'
        self.assertEqual(exclusion(text, {}), 'long_structured_brief')

    def test_timezone_policy_and_invalid_dates(self):
        self.assertEqual(stamp('2026-10-18T12:00:00'), stamp('2026-10-18T12:00:00Z'))
        self.assertEqual(stamp('2026-10-18T13:00:00+01:00'), stamp('2026-10-18T12:00:00Z'))
        self.assertIsNone(stamp('not-a-date'))


class ExposureTests(unittest.TestCase):
    def test_exact_thirty_minutes_is_idle(self):
        self.assertEqual(intervals([row('10:00:00'), row('10:30:00')]), [])
        spans = intervals([row('10:00:00'), row('10:29:59')])
        self.assertEqual((spans[0][1]-spans[0][0]).total_seconds(), 1799)

    def test_cross_host_overlap_is_not_double_counted(self):
        rows = [row('10:00:00'), row('10:10:00'), row('10:05:00', host='b'),
                row('10:10:00', host='b')]
        spans = intervals(rows)
        self.assertEqual(len(spans), 1)
        self.assertAlmostEqual((spans[0][1]-spans[0][0]).total_seconds()/60, 10)

    def test_midnight_and_exact_gate_birth_split(self):
        a, b = stamp('2026-10-17T23:50:00Z'), stamp('2026-10-18T00:10:00Z')
        cut = stamp('2026-10-18T00:05:00Z')
        parts = list(portions(a, b, cut))
        self.assertEqual([(x[0], x[1]) for x in parts], [
            ('2026-10-17', 'before'), ('2026-10-18', 'before'), ('2026-10-18', 'after')])
        self.assertAlmostEqual(sum(x[2] for x in parts), 1/3)
        self.assertAlmostEqual(parts[-1][2], 1/12)

    def test_singleton_has_no_added_exposure(self):
        rows = [row('10:00:00', 'y')]
        units = daily_units(rows, intervals(rows), stamp('2026-10-18T00:00:00Z'))
        result = comparison(units, ['other'], 100)
        self.assertEqual(result['before_corrections'], 1)
        self.assertIsNone(result['before_rate'])
        self.assertEqual(result['valid_bootstrap_draws'], 0)


class InferenceTests(unittest.TestCase):
    def test_known_kappa(self):
        result = kappa(['y', 'y', 'y', 'n', 'n'], ['y', 'y', 'n', 'n', 'n'])
        self.assertAlmostEqual(result['agreement'], .8)
        self.assertAlmostEqual(result['kappa'], 8/13)
        self.assertIsNone(kappa(['y', 'y'], ['y', 'y'])['kappa'])

    def test_known_difference_and_paired_did(self):
        units = {'before': [dict(hours=2, counts={'covered': 8, 'other': 2})],
                 'after': [dict(hours=4, counts={'covered': 4, 'other': 4})]}
        result = comparison(units, ['covered'], 100)
        self.assertEqual(result['before_rate'], 4)
        self.assertEqual(result['after_rate'], 1)
        self.assertEqual(result['change'], -3)
        self.assertEqual((result['change_lo'], result['change_hi']), (-3, -3))
        self.assertEqual(did(units, ['covered'], 100)['did'], -3)

    def test_bootstrap_reproducibility_and_joint_resampling(self):
        units = {'before': [dict(hours=1, counts={'covered': 1, 'other': 1}),
                            dict(hours=3, counts={'covered': 9, 'other': 9})],
                 'after': [dict(hours=2, counts={'covered': 5, 'other': 5}),
                           dict(hours=1, counts={'covered': 0, 'other': 0})]}
        first = did(units, ['covered'], 500)
        self.assertEqual(first, did(units, ['covered'], 500))
        self.assertEqual((first['did'], first['lo'], first['hi']), (0, 0, 0))

    def test_model_batch_requires_every_row(self):
        with self.assertRaises(AssertionError):
            validate([dict(i=0, include='y', is_correction='n', **{'class': 'other'})], 2)


class PublicationTests(unittest.TestCase):
    def test_private_directory_is_ignored(self):
        here = Path(__file__).resolve().parent
        p = subprocess.run(['git', 'check-ignore', str(here / 'data' / 'corpus.jsonl')], capture_output=True)
        self.assertEqual(p.returncode, 0)

    def test_public_markdown_has_no_forbidden_punctuation(self):
        here = Path(__file__).resolve().parent
        for path in here.glob('*.md'):
            text = path.read_text()
            for char in ['\u2014', ';', '\u00b7']:
                self.assertNotIn(char, text, f'{path.name}: prohibited punctuation')


if __name__ == '__main__':
    unittest.main()
