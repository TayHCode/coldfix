import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import ci_vm_executor
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

    @unittest.skipUnless(os.environ.get('COLDFIX_VM_HOST'), 'set COLDFIX_VM_HOST to replay on a Coldbox VM host')
    def test_coldbox_backend_replays_the_documented_pair(self):
        receipts = []
        self.assert_replays_the_documented_pair(ci_vm_executor.replay_tree, 300, receipts=receipts)
        self.assertEqual(len(receipts), 2, receipts)
        print('Coldbox receipts:', *receipts, file=sys.stderr)


if __name__ == '__main__':
    unittest.main()
