import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import ci_vm_executor
import coldfix
import replay_isolation

CHECK = '''import json
import sys

sys.path.insert(0, ".")
from count import count

if count(["a", "b"]) == 2:
    print(json.dumps({"status": "PASS"}))
    sys.exit(0)
print(json.dumps({"status": "FAIL", "failure": "count-off-by-one"}))
sys.exit(1)
'''


def toy_pair(root):
    repo = root / 'toy'
    repo.mkdir()

    def commit(body, message):
        (repo / 'count.py').write_text('def count(items):\n    return ' + body + '\n')
        subprocess.run(['git', '-C', str(repo), 'add', 'count.py'], check=True)
        subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Example', '-c', 'user.email=',
                        'commit', '--quiet', '-m', message], check=True)
        return subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()

    subprocess.run(['git', 'init', '--quiet', str(repo)], check=True)
    bug = commit('len(items) + 1', 'Add count')
    fix = commit('len(items)', 'Fix count')
    check = root / 'check.py'
    check.write_text(CHECK)
    return repo, bug, fix, 'python3 -I -S ' + str(check)


class BackendTests(unittest.TestCase):
    def assert_replays_the_documented_pair(self, replay_tree, timeout, **options):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, bug, fix, command = toy_pair(root)
            rows = {side: replay_tree(repo, revision, root / side, command, 'count-off-by-one', timeout, **options)
                    for side, revision in (('before', bug), ('after', fix))}
            self.assertEqual((rows['before']['status'], rows['before']['commit']), ('FAIL', bug), rows['before']['stderr'])
            self.assertEqual((rows['after']['status'], rows['after']['commit']), ('PASS', fix), rows['after']['stderr'])

    def test_local_backend_replays_the_documented_pair(self):
        self.assert_replays_the_documented_pair(replay_isolation.replay_tree, 60)

    def test_configured_runner_replays_the_documented_pair_without_bwrap_or_coldbox(self):
        environment = {k: v for k, v in os.environ.items() if k != 'COLDFIX_VM_HOST'}
        environment['COLDFIX_REPLAY_RUNNER'] = 'sh -c {script}'
        with mock.patch.dict(os.environ, environment, clear=True), \
                mock.patch.object(replay_isolation, 'sandbox', side_effect=AssertionError('bwrap used')), \
                mock.patch.object(replay_isolation.shutil, 'which', return_value=None):
            self.assertEqual(replay_isolation.require_sandbox(), dict(sandbox='off', runner='sh -c {script}'))
            self.assert_replays_the_documented_pair(replay_isolation.replay_tree, 60)

    def test_configured_runner_stops_a_hung_check_on_the_target(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.dict(os.environ, {'COLDFIX_REPLAY_RUNNER': 'setsid sh -c {script}'}):
            root = Path(directory)
            repo, bug, _, _ = toy_pair(root)
            check = root / 'hang.py'
            check.write_text('import time\ntime.sleep(600)\n')
            started = time.monotonic()
            row = replay_isolation.replay_tree(repo, bug, root / 'hung', 'python3 -I -S ' + str(check),
                                               'count-off-by-one', 2)
            self.assertLess(time.monotonic() - started, 60)
            self.assertEqual(row['status'], 'INCOMPLETE', row)

    def test_configured_runner_returns_when_a_timed_out_check_leaves_a_detached_child(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.dict(os.environ, {'COLDFIX_REPLAY_RUNNER': 'sh -c {script}'}):
            root = Path(directory)
            repo, bug, _, _ = toy_pair(root)
            check = root / 'detach.py'
            check.write_text('import subprocess, time\n'
                             'subprocess.Popen(["setsid", "sleep", "30"])\n'
                             'time.sleep(600)\n')
            started = time.monotonic()
            row = replay_isolation.replay_tree(repo, bug, root / 'detached', 'python3 -I -S ' + str(check),
                                               'count-off-by-one', 2)
            self.assertLess(time.monotonic() - started, 20)
            self.assertEqual(row['status'], 'INCOMPLETE', row)

    def test_runner_without_script_placeholder_is_rejected(self):
        with mock.patch.dict(os.environ, {'COLDFIX_REPLAY_RUNNER': 'ssh host sh'}), \
                self.assertRaisesRegex(coldfix.ColdfixError, 'must contain {script}'):
            self.assert_replays_the_documented_pair(replay_isolation.replay_tree, 60)

    @unittest.skipUnless(os.environ.get('COLDFIX_VM_HOST'), 'set COLDFIX_VM_HOST to replay on a Coldbox VM host')
    def test_coldbox_backend_replays_the_documented_pair(self):
        receipts = []
        self.assert_replays_the_documented_pair(ci_vm_executor.replay_tree, 300, receipts=receipts)
        self.assertEqual(len(receipts), 2, receipts)
        print('Coldbox receipts:', *receipts, file=sys.stderr)


if __name__ == '__main__':
    unittest.main()
