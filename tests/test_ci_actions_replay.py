import json
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import ci_actions_replay as actions
import ci_fix_learning as ci
import ci_workflow_replay as workflow
import coldfix as c


WORKFLOW = '''name: Tests
on: [push]
jobs:
  test:
    name: test (${{ matrix.python }})
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python: ['3.11', '3.12']
    steps:
      - uses: actions/checkout@v4
      - name: Test
        run: python -m unittest -v
'''


def job(name='test (3.12)'):
    return dict(name=name, steps=[dict(name='Test', conclusion='failure')])


class ActionsReplayTests(unittest.TestCase):
    def test_lint_step_failure_is_reproduced_with_its_diagnostic(self):
        self.assert_lint_replay_proven("print(json.dumps(event))")

    def test_act_log_messages_keep_unicode_line_separators(self):
        self.assert_lint_replay_proven(
            "sys.stdout.buffer.write(json.dumps(event, ensure_ascii=False).replace('mismatch', 'mis\\u0085match').encode() + b'\\n')")

    def assert_lint_replay_proven(self, emit):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / 'repo'
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            git = ['git', '-C', str(repo)]
            def commit(value):
                (repo / 'value').write_text(value)
                subprocess.run(git + ['add', '.'], check=True)
                subprocess.run(git + ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                                      'commit', '-qm', value], check=True)
                return subprocess.check_output(git + ['rev-parse', 'HEAD'], text=True).strip()
            before, after = commit('bad'), commit('good')
            bindir = root / 'bin'
            bindir.mkdir()
            unshare = bindir / 'unshare'
            unshare.write_text('#!/bin/sh\nshift 6\nexec "$@"\n')
            unshare.chmod(0o755)
            act = bindir / 'act'
            act.write_text('#!' + sys.executable + '\n' + '''import json, pathlib, sys
ok = pathlib.Path('value').read_text() == 'good'
base = dict(jobID='test', stage='Main', step='Hub check', matrix={})
event = dict(base, raw_output=True, msg='PASS: policy\\n' if ok else 'FAIL: policy mismatch\\n')
''' + emit + '''
sys.stdout.flush()
print(json.dumps(dict(base, stepResult='success' if ok else 'failure')))
sys.exit(0 if ok else 1)
''')
            act.chmod(0o755)
            plan = dict(kind='lint', workflow='.github/workflows/ci.yml', job_id='test', event='push',
                        matrix={}, target_step='Hub check', runners=['ubuntu-latest'])
            def execute(*args, receipts, provision, **kwargs):
                receipts.append('/var/lib/vm02/jobs/fixture.json')
                return c.replay_tree(*args, **kwargs)
            with patch.dict(os.environ, {'PATH': str(bindir) + ':' + os.defpath}), \
                    patch.object(workflow.vm, 'replay_tree', side_effect=execute):
                proof = workflow.replay_pair(repo, before, after, plan, root / 'work', 30,
                                             builder=actions.check_script)
            self.assertEqual(proof['verification'], 'proven', proof)
            self.assertEqual([proof[side]['status'] for side in ('before', 'after', 'reversed')],
                             ['FAIL', 'PASS', 'FAIL'])

    @unittest.skipUnless(importlib.util.find_spec('yaml'), 'PyYAML is optional')
    def test_selects_the_failed_matrix_job(self):
        plan = actions.select_plan(WORKFLOW, '.github/workflows/tests.yml', job())
        self.assertEqual(plan['job_id'], 'test')
        self.assertEqual(plan['matrix'], {'python': '3.12'})
        self.assertEqual(plan['target_step'], 'Test')

    @unittest.skipUnless(importlib.util.find_spec('yaml'), 'PyYAML is optional')
    def test_holds_unsupported_runner_and_external_action(self):
        with self.assertRaises(ci.Skipped):
            actions.select_plan(WORKFLOW.replace('ubuntu-latest', 'windows-latest'),
                                '.github/workflows/tests.yml', job())
        with self.assertRaises(ci.Skipped):
            actions.select_plan(WORKFLOW.replace('actions/checkout@v4', 'other/action@v1'),
                                '.github/workflows/tests.yml', job())
        with self.assertRaises(ci.Skipped):
            actions.select_plan(WORKFLOW.replace('python -m unittest -v', 'npm publish'),
                                '.github/workflows/tests.yml', job())
        with self.assertRaises(ci.Skipped):
            actions.select_plan(WORKFLOW.replace('python -m unittest -v', 'python -m unittest ${{ github.ref }}'),
                                '.github/workflows/tests.yml', job())
        with self.assertRaises(ci.Skipped):
            actions.select_plan(WORKFLOW.replace('    runs-on: ubuntu-latest',
                                                 '    runs-on: ubuntu-latest\n    runs-on: windows-latest'),
                                '.github/workflows/tests.yml', job())

    @unittest.skipUnless(importlib.util.find_spec('yaml'), 'PyYAML is optional')
    def test_checks_same_failed_step_and_tests_across_three_trees(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / 'repo'
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            git = ['git', '-C', str(repo)]
            (repo / 'value').write_text('bad')
            def commit(message):
                subprocess.run(git + ['add', '.'], check=True)
                subprocess.run(git + ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                                      'commit', '-qm', message], check=True)
                return subprocess.check_output(git + ['rev-parse', 'HEAD'], text=True).strip()
            before = commit('before')
            (repo / 'value').write_text('good')
            after = commit('after')
            bin_dir = root / 'bin'
            bin_dir.mkdir()
            unshare = bin_dir / 'unshare'
            unshare.write_text('#!/bin/sh\nshift 6\nexec "$@"\n')
            unshare.chmod(0o755)
            fake = bin_dir / 'act'
            fake.write_text('#!' + sys.executable + '\n' + '''import json, pathlib, subprocess, sys
subprocess.run(['git', 'rev-parse', '--verify', 'refs/coldfix/replay'], check=True, stdout=subprocess.DEVNULL)
ok = pathlib.Path('value').read_text() == 'good'
base = dict(jobID='test', stage='Main', step='Test', matrix={'python':'3.12'})
name = 'test_value (test_value.TestValue.test_value)'
print(json.dumps(dict(base, raw_output=True, msg=(name + ' ... ok\\n') if ok else ('FAIL: ' + name + '\\nAssertionError: bad\\n'))))
print(json.dumps(dict(base, stepResult='success' if ok else 'failure')))
sys.exit(0 if ok else 1)
''')
            fake.chmod(0o755)
            plan = actions.select_plan(WORKFLOW, '.github/workflows/tests.yml', job())
            def execute(*args, receipts, provision, **kwargs):
                receipts.append('/var/lib/vm02/jobs/fixture.json')
                return c.replay_tree(*args, **kwargs)
            with patch.dict(os.environ, {'PATH': str(bin_dir) + ':' + os.defpath}), \
                    patch.object(workflow.vm, 'replay_tree', side_effect=execute):
                proof = workflow.replay_pair(repo, before, after, plan, root / 'work', 30,
                                             builder=actions.check_script)
            self.assertEqual(proof['verification'], 'proven')
            self.assertEqual([proof[side]['status'] for side in ('before', 'after', 'reversed')],
                             ['FAIL', 'PASS', 'FAIL'])
            self.assertEqual(proof['receipts']['reversed'], ['/var/lib/vm02/jobs/fixture.json'])
            fake.write_text(fake.read_text().replace("name + '\\nAssertionError", "name + ' (case=1)\\nAssertionError"))
            with patch.dict(os.environ, {'PATH': str(bin_dir) + ':' + os.defpath}), \
                    patch.object(workflow.vm, 'replay_tree', side_effect=execute):
                subtest = workflow.replay_pair(repo, before, after, plan, root / 'subtest-work', 30,
                                               builder=actions.check_script)
            self.assertEqual(subtest['verification'], 'unproven')
            self.assertIn('failed test did not pass after fix', subtest['after']['stderr'])
            fake.write_text(fake.read_text().replace(' (case=1)', ''))
            fake.write_text(fake.read_text().replace("step='Test'", "step='Setup'"))
            with patch.dict(os.environ, {'PATH': str(bin_dir) + ':' + os.defpath}), \
                    patch.object(workflow.vm, 'replay_tree', side_effect=execute):
                mismatch = workflow.replay_pair(repo, before, after, plan, root / 'other-work', 30,
                                                builder=actions.check_script)
            self.assertEqual(mismatch['verification'], 'unproven')
            self.assertEqual(mismatch['probe']['status'], 'INCOMPLETE')
            fake.write_text(fake.read_text().replace("step='Setup'", "step='Test'").replace(
                'base = dict(', "pathlib.Path('value').write_text('mutated')\nbase = dict("))
            with patch.dict(os.environ, {'PATH': str(bin_dir) + ':' + os.defpath}), \
                    patch.object(workflow.vm, 'replay_tree', side_effect=execute):
                changed = workflow.replay_pair(repo, before, after, plan, root / 'changed-work', 30,
                                               builder=actions.check_script)
            self.assertEqual(changed['verification'], 'unproven')
            self.assertEqual(changed['probe']['status'], 'INCOMPLETE')
            fake.write_text(fake.read_text().replace("pathlib.Path('value').write_text('mutated')\n", '').replace(
                "(name + ' ... ok\\n')", "(name + '\\nValue is good. ... ok\\n')"))
            self.assertIn('Value is good.', fake.read_text())
            self.assertNotIn('mutated', fake.read_text())
            with patch.dict(os.environ, {'PATH': str(bin_dir) + ':' + os.defpath}), \
                    patch.object(workflow.vm, 'replay_tree', side_effect=execute):
                documented = workflow.replay_pair(repo, before, after, plan, root / 'documented-work', 30,
                                                  builder=actions.check_script)
            self.assertEqual(documented['verification'], 'proven')
