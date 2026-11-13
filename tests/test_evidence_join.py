"""Exercise the join on the audited real Coldfix ingest sample, when retained locally."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import coldfix as c
import failure_ingest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(os.environ.get('COLDFIX_RECORDED_STORE', ROOT / '.coldfix'))
IDS = ('0042f636e4234a55898c31fbc24a55b2', '00534f5d95ca4d74b1c7f10d0ed0b173',
       '006e1d3844f34ac1aca8de62ac86200f')


class RecordedEvidenceTest(unittest.TestCase):
    def test_real_ingest_reimport_is_noop(self):
        if not all((SOURCE / 'failures' / (i + '.json')).is_file() for i in IDS):
            self.skipTest('audited private ingest records are not present')
        with tempfile.TemporaryDirectory() as temp:
            store = Path(temp) / 'store'
            (store / 'failures').mkdir(parents=True, mode=0o700)
            for identity in IDS:
                shutil.copyfile(SOURCE / 'failures' / (identity + '.json'), store / 'failures' / (identity + '.json'))
            path = store / 'evidence-join.json'
            self.assertFalse(path.exists())  # zero joined incidents before import
            def run():
                result = subprocess.run([sys.executable, str(ROOT / 'evidence_join.py'), '--store', str(store)],
                                        capture_output=True, text=True, check=True)
                return json.loads(result.stdout)
            self.assertEqual(run(), {'changed': True, 'joined': 0, 'missing_proof': 3})
            original = path.read_bytes()
            snapshot = json.loads(original)
            self.assertEqual(snapshot['incidents'], [])
            self.assertEqual({entry['failure_ids'][0] for entry in snapshot['missing_proof']}, set(IDS))
            for entry in snapshot['missing_proof']:
                self.assertEqual(entry['missing'], ['PR-to-commit edge', 'observed failing revision', 'failing check', 'replay'])
                self.assertIsNotNone(entry['recorded_fix'])
                self.assertIsNone(entry['failing_revision'])
            self.assertEqual(run(), {'changed': False, 'joined': 0, 'missing_proof': 3})
            self.assertEqual(path.read_bytes(), original)


class MembershipTest(unittest.TestCase):
    def test_failing_revision_resolves_unique_pr_but_not_ambiguous_prs(self):
        fixtures = ROOT / 'tests' / 'fixtures'
        head = 'a' * 40
        for source in ('trace', 'ci'):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as temp:
                work = Path(temp)
                store = work / '.coldfix'
                if source == 'trace':
                    args = ['trace', '--input', str(fixtures / 'verify-trace.jsonl'),
                            '--repo', 'example/project', '--occurred', '2026-10-30T12:00:00Z']
                else:
                    event = json.loads((fixtures / 'failed-ci-run.json').read_text())
                    event['pr'] = None
                    input_path = work / 'event.json'
                    input_path.write_text(json.dumps(event))
                    args = ['json', '--input', str(input_path)]
                subprocess.run([sys.executable, str(ROOT / 'failure_ingest.py'), '--store', str(store), *args],
                               capture_output=True, text=True, check=True)
                membership = work / 'prs.json'
                def reconcile(prs):
                    membership.write_text(json.dumps(prs))
                    subprocess.run([sys.executable, str(ROOT / 'evidence_join.py'), '--store', str(store),
                                    '--pr-commits', str(membership)], capture_output=True, text=True, check=True)
                    return json.loads((store / 'evidence-join.json').read_text())['missing_proof'][0]
                unique = [{'repo': 'example/project', 'pr': 17, 'commits': [head]},
                          {'repo': 'other/project', 'pr': 99, 'commits': [head]}]
                result = reconcile(unique)
                self.assertEqual(result['pr'], 17)
                self.assertEqual(result['failing_revision'], head)
                self.assertNotIn('PR-to-commit edge', result['missing'])
                self.assertIn('replay', result['missing'])
                ambiguous = reconcile(unique + [{'repo': 'example/project', 'pr': 18, 'commits': [head]}])
                self.assertIsNone(ambiguous['pr'])
                self.assertIn('PR-to-commit edge', ambiguous['missing'])


class DiagnosticTest(unittest.TestCase):
    def test_non_object_json_diagnostic_stays_in_queue(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Path(temp) / '.coldfix'
            event = failure_ingest.ci_event(json.loads((ROOT / 'tests/fixtures/failed-ci-run.json').read_text()))
            event['error'] = '["AssertionError: expected 2"]'
            row = c.event_record(event, 'failure-ingest')
            c.save(store, 'failures', row)
            result = subprocess.run([sys.executable, str(ROOT / 'evidence_join.py'), '--store', str(store)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            queue = json.loads((store / 'evidence-join.json').read_text())['missing_proof']
            self.assertEqual(len(queue), 1)
            self.assertEqual(queue[0]['failure_ids'], [row['id']])
            self.assertIn('observed failing revision', queue[0]['missing'])

    def test_failed_trace_without_revision_has_a_failing_check(self):
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            store = work / '.coldfix'
            rows = [json.loads(line) for line in (ROOT / 'tests/fixtures/verify-trace.jsonl').read_text().splitlines()]
            for row in rows:
                row['head'] = None
            trace = work / 'trace.jsonl'
            trace.write_text(''.join(json.dumps(row) + '\n' for row in rows))
            subprocess.run([sys.executable, str(ROOT / 'failure_ingest.py'), '--store', str(store), 'trace',
                            '--input', str(trace), '--repo', 'example/project', '--occurred', '2026-10-30T12:00:00Z'],
                           capture_output=True, text=True, check=True)
            subprocess.run([sys.executable, str(ROOT / 'evidence_join.py'), '--store', str(store)],
                           capture_output=True, text=True, check=True)
            queue = json.loads((store / 'evidence-join.json').read_text())['missing_proof']
            self.assertEqual(len(queue), 1)
            self.assertIsNone(queue[0]['failing_revision'])
            self.assertIn('observed failing revision', queue[0]['missing'])
            self.assertNotIn('failing check', queue[0]['missing'])


if __name__ == '__main__':
    unittest.main()
