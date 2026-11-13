import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'tests/fixtures/verify-trace-v3.jsonl'


class ColdVerifyIngestTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        self.repo = self.work / 'project'
        self.repo.mkdir()
        for args in (('init', '--quiet'), ('remote', 'add', 'origin', 'git@github.com:example/project.git')):
            subprocess.run(['git', '-C', str(self.repo), *args], check=True, capture_output=True)
        self.trace = self.repo / '.cold/verify-trace.jsonl'
        self.trace.parent.mkdir()
        self.trace.write_bytes(FIXTURE.read_bytes())
        self.store = self.work / 'store'

    def cli(self, store=None):
        return subprocess.run([sys.executable, str(ROOT / 'coldfix.py'), '--store', str(store or self.store),
                               'ingest', '--format', 'cold-verify', '--input', str(self.trace)],
                              text=True, capture_output=True, cwd=self.work)

    def records(self):
        return [json.loads(path.read_text()) for path in (self.store / 'failures').glob('*.json')]

    def test_failed_steps_keep_fields_and_reimport_skips_duplicates(self):
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['imported_records'], 2)
        records = {row['command']: row for row in self.records()}
        self.assertEqual(set(records), {'python3 -m unittest', 'python3 check.py'})
        row = records['python3 -m unittest']
        self.assertEqual((row['repo'], row['kind'], row['source']), ('example/project', 'test', 'failure-ingest'))
        evidence = json.loads(row['error'])
        self.assertEqual({key: evidence[key] for key in ('head', 'step', 'excerpt')},
                         dict(head='a' * 40, step='unit', excerpt='AssertionError: expected 2, got 3\napi_key=[REDACTED]'))
        self.assertEqual(row['source_ref'], 'verify-trace:example/project:run-1:unit')
        before = {p.name: p.read_bytes() for p in (self.store / 'failures').glob('*.json')}
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((json.loads(result.stdout)['imported_records'], json.loads(result.stdout)['skipped_duplicates']), (0, 2))
        self.assertEqual({p.name: p.read_bytes() for p in (self.store / 'failures').glob('*.json')}, before)

    def test_non_github_origin_is_rejected_with_a_clear_message(self):
        for url in ('git@gitlab.com:example/project.git', 'https://evilgithub.com/example/project.git',
                    'https://github.com.evil.test/example/project.git', 'https://github.com@evil.test/example/project.git'):
            with self.subTest(url=url):
                subprocess.run(['git', '-C', str(self.repo), 'remote', 'set-url', 'origin', url],
                               check=True, capture_output=True)
                result = self.cli()
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn('needs a GitHub origin remote', result.stderr)
                self.assertFalse(self.store.exists())

    def test_github_origin_forms_are_accepted(self):
        for url in ('https://github.com/example/project.git', 'https://github.com/example/project',
                    'ssh://git@github.com/example/project.git', 'https://alice@github.com/example/project.git',
                    'ssh://git@github.com:22/example/project.git', 'http://github.com/example/project',
                    'git://github.com/example/project.git', 'https://GitHub.com/example/project.git'):
            with self.subTest(url=url):
                subprocess.run(['git', '-C', str(self.repo), 'remote', 'set-url', 'origin', url],
                               check=True, capture_output=True)
                store = self.work / ('store-' + str(abs(hash(url))))
                result = self.cli(store)
                self.assertEqual(result.returncode, 0, result.stderr)
                rows = [json.loads(p.read_text()) for p in (store / 'failures').glob('*.json')]
                self.assertEqual({row['repo'] for row in rows}, {'example/project'})

    def test_changed_evidence_for_a_stored_step_is_rejected(self):
        self.assertEqual(self.cli().returncode, 0)
        before = {p.name: p.read_bytes() for p in (self.store / 'failures').glob('*.json')}
        self.trace.write_text(self.trace.read_text().replace('invalid sum', 'different failure'))
        result = self.cli()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('conflicts with a stored failure', result.stderr)
        self.assertEqual({p.name: p.read_bytes() for p in (self.store / 'failures').glob('*.json')}, before)

    def test_passing_and_skipped_steps_add_nothing(self):
        lines = self.trace.read_text().split('\n')
        self.trace.write_text('\n'.join(line for line in lines if line and json.loads(line)['status'] != 'fail') + '\n')
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['imported_records'], 0)
        self.assertEqual(self.records(), [])

    def test_trace_imports_share_grouping_and_recur_at_later_heads(self):
        self.assertEqual(self.cli().returncode, 0)
        legacy = self.work / 'legacy'
        result = subprocess.run([sys.executable, str(ROOT / 'failure_ingest.py'), '--store', str(legacy),
                                 'trace', '--input', str(self.trace), '--repo', 'example/project',
                                 '--occurred', '2026-10-30T12:00:00Z'], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

        def groups(store):
            result = subprocess.run([sys.executable, str(ROOT / 'coldfix.py'), '--store', str(store),
                                     'propose', '--grouping', 'normalized'], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            return {row['group_id']: row['count'] for row in json.loads(result.stdout)['groups']}

        self.assertEqual(groups(self.store), groups(legacy))
        result = self.cli(legacy)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((json.loads(result.stdout)['imported_records'], json.loads(result.stdout)['skipped_duplicates']), (0, 2))
        self.trace.write_text(self.trace.read_text().replace('run-1', 'run-2').replace('a' * 40, 'd' * 40))
        self.assertEqual(self.cli().returncode, 0)
        self.assertEqual(sorted(groups(self.store).values()), [2, 2])

    def test_trace_failures_reconcile_with_recorded_pr_membership(self):
        self.assertEqual(self.cli().returncode, 0)
        membership = self.work / 'prs.json'
        membership.write_text(json.dumps([dict(repo='example/project', pr=17, commits=['a' * 40])]))
        result = subprocess.run([sys.executable, str(ROOT / 'evidence_join.py'), '--store', str(self.store),
                                 '--pr-commits', str(membership)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        queue = json.loads((self.store / 'evidence-join.json').read_text())['missing_proof']
        self.assertEqual(len(queue), 2)
        for item in queue:
            self.assertEqual((item['pr'], item['failing_revision'], item['missing']),
                             (17, 'a' * 40, ['fix commit', 'replay']))

    def test_malformed_later_lines_exit_two_without_writing(self):
        original = self.trace.read_text()
        bad_rows = ['{not json', '[]', '{"schema_version":3}',
                    json.dumps(dict(json.loads(original.split('\n')[1]), run_id=['invalid'])),
                    json.dumps(dict(json.loads(original.split('\n')[1]), status=['fail']))]
        for bad in bad_rows:
            with self.subTest(line=bad):
                self.trace.write_text(original + bad + '\n')
                result = self.cli()
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn('invalid verify trace', result.stderr)
                self.assertNotIn('Traceback', result.stderr)
                self.assertFalse(self.store.exists())
        self.trace.write_text(original)
        self.assertEqual(self.cli().returncode, 0)
        before = {p.name: p.read_bytes() for p in (self.store / 'failures').glob('*.json')}
        self.trace.write_text(original + '{not json\n')
        self.assertEqual(self.cli().returncode, 2)
        self.assertEqual({p.name: p.read_bytes() for p in (self.store / 'failures').glob('*.json')}, before)


if __name__ == '__main__':
    unittest.main()
