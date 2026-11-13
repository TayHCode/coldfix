import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'tests/fixtures/run-result'


class RunResultIngestTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        self.record = self.work / 'verify-evidence.json'
        self.store = self.work / 'store'

    def cli(self, document):
        self.record.write_text(document if isinstance(document, str) else json.dumps(document))
        return subprocess.run([sys.executable, str(ROOT / 'coldfix.py'), '--store', str(self.store),
                               'ingest', '--format', 'run-result', '--input', str(self.record)],
                              text=True, capture_output=True, cwd=self.work)

    def fixture(self, name):
        return json.loads((FIXTURES / f'coldverify-{name}.json').read_text())

    def stored(self):
        return [json.loads(path.read_text()) for path in (self.store / 'failures').glob('*.json')]

    def test_a_failed_coldverify_record_becomes_one_failure_per_failed_step(self):
        failed = self.fixture('failed')
        result = self.cli(failed)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['imported_records'], 1)
        [row] = self.stored()
        self.assertEqual((row['repo'], row['kind'], row['source'], row['command']),
                         ('example/project', 'test', 'failure-ingest', failed['steps'][2]['command']))
        evidence = json.loads(row['error'])
        self.assertEqual((evidence['head'], evidence['step'], evidence['excerpt'], evidence['run_id']),
                         (failed['repo']['commit'], 'check tests', 'exit 1', failed['run_id']))
        result = self.cli(failed)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((json.loads(result.stdout)['imported_records'], json.loads(result.stdout)['skipped_duplicates']), (0, 1))

    def test_verified_and_incomplete_records_are_accepted_and_store_no_failure(self):
        for name in ('verified', 'incomplete'):
            with self.subTest(name=name):
                result = self.cli(self.fixture(name))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)['imported_records'], 0)
                self.assertEqual(self.stored(), [])

    def test_a_step_without_inputs_hash_is_accepted(self):
        failed = self.fixture('failed')
        failed['steps'] = [{k: v for k, v in step.items() if k != 'inputs_hash'} for step in failed['steps']]
        result = self.cli(failed)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['imported_records'], 1)

    def test_changed_evidence_for_a_stored_step_is_rejected(self):
        failed = self.fixture('failed')
        self.assertEqual(self.cli(failed).returncode, 0)
        failed['steps'][2]['failing_excerpt'] = 'exit 2'
        result = self.cli(failed)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('conflicts with a stored failure', result.stderr)

    def test_invalid_or_inconsistent_records_exit_two_and_write_nothing(self):
        failed, verified, incomplete = self.fixture('failed'), self.fixture('verified'), self.fixture('incomplete')
        tests_step = failed['steps'][2]
        bad = {
            'not json': '{not json',
            'not an object': [],
            'version 2': dict(failed, schema_version=2),
            'version as string': dict(failed, schema_version='1'),
            'version as bool': dict(failed, schema_version=True),
            'short commit': dict(failed, repo=dict(failed['repo'], commit='abc')),
            'bare repo name': dict(failed, repo=dict(failed['repo'], name='project')),
            'unknown outcome': dict(failed, outcome='verified'),
            'pass with a failure': dict(verified, failure=failed['failure']),
            'fail without a failure': {k: v for k, v in failed.items() if k != 'failure'},
            'pass with a failed step': dict(verified, steps=failed['steps']),
            'pass with no steps': dict(verified, steps=[]),
            'fail with no failed step': dict(failed, steps=verified['steps']),
            'fail with a skipped step': dict(failed, steps=failed['steps'] + [dict(verified['steps'][0], step='extra', status='skip')]),
            'harness failure called fail': dict(failed, failure=incomplete['failure']),
            'product failure called incomplete': dict(incomplete, failure=failed['failure']),
            'failed step without excerpt': dict(failed, steps=[{k: v for k, v in tests_step.items() if k != 'failing_excerpt'}]),
            'excerpt on a passing step': dict(verified, steps=[dict(verified['steps'][0], failing_excerpt='x')]),
            'unknown step field': dict(failed, steps=[dict(tests_step, extra=1)]),
            'duplicate step': dict(failed, steps=[tests_step, tests_step]),
            'negative duration': dict(failed, steps=[dict(tests_step, duration_ms=-1)]),
            'multi-line run id': dict(failed, run_id='a\nb'),
            'no model': {k: v for k, v in failed.items() if k != 'model'},
            'no time or cost': {k: v for k, v in failed.items() if k not in ('time', 'cost')},
            'unknown top-level field': dict(failed, extra=1),
            'failure without cause': dict(failed, failure={'class': 'product'}),
            'harness cause on a product failure': dict(failed, failure=dict(failed['failure'], cause='timeout')),
            'list failure class': dict(failed, failure={'class': [], 'cause': 'test_failure'}),
            'list failure cause': dict(failed, failure={'class': 'product', 'cause': []}),
            'list step name': dict(failed, steps=[dict(tests_step, step=[])]),
            'unknown failure field': dict(failed, failure=dict(failed['failure'], extra=1)),
            'non-text failure detail': dict(failed, failure=dict(failed['failure'], detail=1)),
            'skipped step without skipped_step cause': dict(incomplete, failure=dict(incomplete['failure'], cause='timeout')),
            'skipped_step cause without a skipped step': dict(incomplete, steps=verified['steps']),
        }
        for label, document in bad.items():
            with self.subTest(label=label):
                result = self.cli(document)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn('invalid or unsupported run result', result.stderr)
                self.assertNotIn('Traceback', result.stderr)
                self.assertFalse(self.store.exists())


if __name__ == '__main__':
    unittest.main()
