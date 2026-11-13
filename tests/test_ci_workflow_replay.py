import json
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import zipfile

import ci_workflow_replay as workflow
import ci_fix_learning as ci
import ci_exact_unittest
import coldfix as c


class WorkflowReplayTests(unittest.TestCase):
    def test_nonverbose_unittest_still_reproduces(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            repo = root / 'repo'
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            git = ['git', '-C', str(repo)]
            (repo / 'test_value.py').write_text(
                'import unittest\nfrom pathlib import Path\nclass TestValue(unittest.TestCase):\n'
                ' def test_value(self): self.assertEqual(Path("value").read_text(), "good")\n')
            def commit(value):
                (repo / 'value').write_text(value)
                subprocess.run(git + ['add', '.'], check=True)
                subprocess.run(git + ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                                      'commit', '-qm', value], check=True)
                return subprocess.check_output(git + ['rev-parse', 'HEAD'], text=True).strip()
            before, after = commit('bad'), commit('good')
            def execute(*args, receipts, provision, **kwargs):
                receipts.append('/var/lib/vm02/jobs/fixture.json')
                return c.replay_tree(*args, **kwargs)
            plan = dict(command=['python3', '-m', 'unittest', 'discover'], kind='test',
                        source_command='python3 -m unittest discover', runtime_name='python3')
            with patch.object(workflow.vm, 'replay_tree', side_effect=execute):
                proof = workflow.replay_pair(repo, before, after, plan, root / 'work', 30)
            self.assertEqual(proof['verification'], 'proven', proof)
            self.assertEqual([proof[side]['status'] for side in ('before', 'after', 'reversed')],
                             ['FAIL', 'PASS', 'FAIL'])

    def test_rates_include_unattempted_pairs_in_each_original_category(self):
        rows = [
            {'reason': 'runner', 'proof': {
                'verification': 'proven', 'receipts': {'before': ['red'], 'after': ['green'], 'reversed': ['undo']},
                'before': {'status': 'FAIL'}, 'after': {'status': 'PASS'}, 'reversed': {'status': 'FAIL'},
            }},
            {'reason': 'runner', 'proof': {'verification': 'unproven', 'receipts': {'probe': ['probe']}}},
            {'reason': 'runner', 'proof': None},
            {'reason': 'workflow', 'proof': None},
        ]
        self.assertEqual(workflow.summarize_by_reason(rows), {
            'runner': {'total': 3, 'attempted': 2, 'proven': 1, 'reproduction_rate': 1 / 3},
            'workflow': {'total': 1, 'attempted': 0, 'proven': 0, 'reproduction_rate': 0},
        })

    def test_exact_workflow_runs_its_pip_preparation_step(self):
        from test_ci_fix_learning import WORKFLOW, job
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            wheel = root / 'exampledep-1.0-py3-none-any.whl'
            with zipfile.ZipFile(wheel, 'w') as archive:
                archive.writestr('exampledep.py', 'VALUE = 42\n')
                archive.writestr('exampledep-1.0.dist-info/METADATA', 'Metadata-Version: 2.1\nName: exampledep\nVersion: 1.0\n')
                archive.writestr('exampledep-1.0.dist-info/WHEEL', 'Wheel-Version: 1.0\nGenerator: fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
                archive.writestr('exampledep-1.0.dist-info/RECORD', '')
            (root / 'requirements.txt').write_text(wheel.name + '\n')
            (root / 'tests').mkdir()
            (root / 'tests/test_dep.py').write_text(
                'import unittest, exampledep\nclass TestDep(unittest.TestCase):\n'
                ' def test_value(self): self.assertEqual(exampledep.VALUE, 42)\n')
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
            subprocess.run(['git', '-C', str(root), '-c', 'user.name=Fixture', '-c',
                            'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture'], check=True)
            content = WORKFLOW.replace('      - name: Test',
                                       '      - run: python3 -m pip install -r requirements.txt\n      - name: Test')
            plan = ci.workflow_plan(content, job())
            self.assertEqual(plan['setup_commands'], ['python3 -m pip install -r requirements.txt'])
            check = root / 'check.py'
            check.write_text(ci_exact_unittest.check_script(plan))
            result = subprocess.run([sys.executable, '-I', '-S', str(check)], cwd=root, capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {'status': 'PASS'})

    def test_timeout_preserves_command_output_in_guest_log(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            repo = root / 'repo'
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            (repo / 'test_wait.py').write_text(
                'import time, unittest\nclass TestWait(unittest.TestCase):\n'
                ' def test_wait(self):\n  print("started test", flush=True)\n  time.sleep(30)\n')
            git = ['git', '-C', str(repo)]
            subprocess.run(git + ['add', '.'], check=True)
            subprocess.run(git + ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                                  'commit', '-qm', 'fixture'], check=True)
            check = root / 'check.py'
            check.write_text(ci.check_script(dict(command=['python3', '-m', 'unittest', 'discover'], kind='test')))
            log = root / 'command.log'
            with patch.dict('os.environ', {'COLDFIX_GUEST_LOG': str(log)}):
                row = c.replay_tree(repo, 'HEAD', root / 'checkout',
                                    'python3 -I -S ' + str(check), ci.EXPECTED, 3)
            self.assertEqual(row['status'], 'INCOMPLETE')
            self.assertIn('started test', log.read_text())

    def test_replays_before_after_and_reversed(self):
        self.assert_replays_pair(
            ' def test_value(self): self.assertEqual(Path("value").read_text(), "good")\n'
            ' def test_command(self): self.assertTrue(__main__.__file__.endswith("unittest/__main__.py"))\n')

    def test_replays_tests_with_docstrings(self):
        self.assert_replays_pair(
            ' def test_value(self):\n  "Value is good."\n  self.assertEqual(Path("value").read_text(), "good")\n')

    def assert_replays_pair(self, tests):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            repo = root / 'repo'
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            git = ['git', '-C', str(repo)]
            (repo / 'test_value.py').write_text(
                'import unittest, __main__\nfrom pathlib import Path\nclass TestValue(unittest.TestCase):\n' + tests)
            (repo / 'value').write_text('bad')

            def commit(message):
                subprocess.run(git + ['add', '.'], check=True)
                subprocess.run(git + ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                                      'commit', '-qm', message], check=True)
                return subprocess.check_output(git + ['rev-parse', 'HEAD'], text=True).strip()

            before = commit('before')
            (repo / 'value').write_text('good')
            after = commit('after')

            def execute(*args, receipts, provision, **kwargs):
                receipts.append('/var/lib/vm02/jobs/fixture.json')
                return c.replay_tree(*args, **kwargs)

            plan = dict(command=['python3', '-m', 'unittest', 'discover', '-v'], kind='test',
                        source_command='python3 -m unittest discover -v', runtime_name='python3')
            with patch.object(workflow.vm, 'replay_tree', side_effect=execute):
                proof = workflow.replay_pair(repo, before, after, plan, root / 'work', 30)
            self.assertEqual(proof['verification'], 'proven')
            self.assertEqual([proof[side]['status'] for side in ('before', 'after', 'reversed')],
                             ['FAIL', 'PASS', 'FAIL'])
            self.assertEqual(proof['receipts']['reversed'], ['/var/lib/vm02/jobs/fixture.json'])
