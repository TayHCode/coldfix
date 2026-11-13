import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import ci_actions_replay as actions
import ci_vm_executor as vm
import oss_replay


WORKFLOW = '''name: Tests
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Test
        run: python -m unittest -v
'''
REPO = 'octo/demo'


def fixture_repo(root):
    repo = root / 'origin'
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    git = ['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid']
    (repo / '.github' / 'workflows').mkdir(parents=True)
    (repo / '.github' / 'workflows' / 'tests.yml').write_text(WORKFLOW)
    shas = []
    for value in ('bad', 'good'):
        (repo / 'value').write_text(value)
        subprocess.run(git + ['add', '.'], check=True)
        subprocess.run(git + ['commit', '-qm', value], check=True)
        shas.append(subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip())
    subprocess.run(['git', '-C', str(repo), 'config', 'uploadpack.allowAnySHA1InWant', 'true'], check=True)
    return repo, shas


def seed(before, after, status='PROVEN'):
    return dict(repo=REPO, before=dict(commit=before, status='FAIL'), after=dict(commit=after, status='PASS'),
                links=[f'https://github.com/{REPO}/actions/runs/7/attempts/1#job-9'], status=status)


class FakeGitHub:
    def __init__(self, before, private=False):
        self.calls = []
        self.values = {
            f'repos/{REPO}': dict(private=private, visibility='private' if private else 'public'),
            f'repos/{REPO}/actions/runs/7': dict(id=7, head_sha=before, path='.github/workflows/tests.yml',
                                                 event='push', head_repository=dict(full_name=REPO)),
            f'repos/{REPO}/actions/jobs/9': dict(run_id=7, head_sha=before, name='test',
                                                 steps=[dict(name='Test', conclusion='failure')]),
        }

    def api(self, endpoint):
        self.calls.append(endpoint)
        return self.values[endpoint]


def guest(status, commit, stdout='raw guest output with a path /home/example'):
    reason = dict(FAIL='known local diagnostic', PASS='local command passed')[status]
    return dict(commit=commit, tree='t' * 40, status=status, exit=0 if status == 'PASS' else 1, stdout=stdout,
                stderr=json.dumps(dict(reason=reason, diagnostic='d' * 64 if status == 'FAIL' else '', tests=[])))



def proving(seen):
    def replay_pair(clone, red, green, plan, scratch, timeout):
        seen.update(plan=plan, green=green, commits=[subprocess.check_output(
            ['git', '-C', str(clone), 'show', sha + ':value'], text=True) for sha in (red, green)])
        return dict(status='PASS', verification='proven', check_sha256='c' * 64,
                    receipts=dict(probe=['/r/p.json'], before=['/r/b.json'], after=['/r/a.json'],
                                  reversed=['/r/v.json']),
                    probe=guest('FAIL', red), before=guest('FAIL', red), after=guest('PASS', green),
                    reversed=guest('FAIL', green))
    return replay_pair

class OssReplayTests(unittest.TestCase):
    def test_seed_link_for_another_repository_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'seeds.json'
            row = seed('a' * 40, 'b' * 40)
            row['links'] = ['https://github.com/other/repo/actions/runs/7/attempts/1#job-9']
            path.write_text(json.dumps([row]))
            with self.assertRaises(oss_replay.c.ColdfixError):
                oss_replay.read_seeds(path)

    def test_seed_without_a_fix_commit_or_patch_is_rejected(self):
        row = seed('a' * 40, 'b' * 40)
        del row['after']
        with self.assertRaises(oss_replay.c.ColdfixError):
            oss_replay.seed_pairs([row])

    def test_private_repository_is_never_cloned(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(oss_replay, 'anonymous_clone', side_effect=AssertionError('cloned')):
            github = FakeGitHub('a' * 40, private=True)
            record = oss_replay.reproduce(github, oss_replay.seed_pairs([seed('a' * 40, 'b' * 40)])[0],
                                          Path(temporary), 30)
        self.assertEqual((record['status'], record['skip']), ('SKIPPED', 'repository is not public'))
        self.assertEqual(github.calls, [f'repos/{REPO}'])

    @unittest.skipUnless(importlib.util.find_spec('yaml'), 'PyYAML is optional')
    def test_pair_is_cloned_at_exact_commits_and_raw_output_is_dropped(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            origin, (before, after) = fixture_repo(root)
            work = root / 'work'
            work.mkdir(mode=0o700)
            seen = {}

            with patch.object(actions, 'replay_pair', proving(seen)):
                record = oss_replay.reproduce(FakeGitHub(before), oss_replay.seed_pairs([seed(before, after)])[0],
                                              work, 30, url=origin.as_uri())
            self.assertEqual(list(work.iterdir()), [])
        self.assertEqual(record['status'], 'PROVEN')
        self.assertEqual(seen['commits'], ['bad', 'good'])
        self.assertEqual((seen['plan']['job_id'], seen['plan']['target_step']), ('test', 'Test'))
        self.assertEqual(record['proof']['receipts']['reversed'], ['/r/v.json'])
        self.assertEqual([record['proof'][s]['status'] for s in ('before', 'after', 'reversed')], ['FAIL', 'PASS', 'FAIL'])
        self.assertNotIn('/home/example', json.dumps(record))

    @unittest.skipUnless(importlib.util.find_spec('yaml'), 'PyYAML is optional')
    def test_supplied_fix_is_committed_on_the_failing_commit_and_proven(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            origin, (before, after) = fixture_repo(root)
            fix = subprocess.check_output(['git', '-C', str(origin), 'diff', before, after], text=True)
            row = dict(seed(before, after), patch=fix)
            del row['after']
            work = root / 'work'
            work.mkdir(mode=0o700)
            seen = {}
            with patch.object(actions, 'replay_pair', proving(seen)):
                record = oss_replay.reproduce(FakeGitHub(before), oss_replay.seed_pairs([row])[0],
                                              work, 30, url=origin.as_uri())
        self.assertEqual(record['status'], 'PROVEN')
        self.assertEqual(seen['commits'], ['bad', 'good'])
        self.assertEqual(record['after'], seen['green'])
        self.assertNotIn(record['after'], (before, after))
        self.assertNotIn('patch', record)
        self.assertEqual(record['patch_sha256'], oss_replay.c.digest(fix))

    def test_fix_that_does_not_apply_is_skipped(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            origin, (before, after) = fixture_repo(root)
            row = dict(seed(before, after), patch='--- a/missing\n+++ b/missing\n@@ -1 +1 @@\n-x\n+y\n')
            del row['after']
            with patch.object(actions, 'replay_pair', side_effect=AssertionError('replayed')):
                record = oss_replay.reproduce(FakeGitHub(before), oss_replay.seed_pairs([row])[0],
                                              root, 30, url=origin.as_uri())
        self.assertEqual((record['status'], record['skip']), ('SKIPPED', 'fix patch does not apply to the failing commit'))

    def test_cleanup_failure_stops_the_run_and_keeps_earlier_records(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seeds = root / 'seeds.json'
            seeds.write_text(json.dumps([seed('a' * 40, 'b' * 40), seed('c' * 40, 'd' * 40)]))
            outcomes = iter([dict(oss_replay.seed_pairs([seed('a' * 40, 'b' * 40)])[0], status='SKIPPED',
                                  proof=None, skip='repository is not public'),
                             vm.CleanupFailed('VM cleanup unconfirmed')])

            clients = []

            def reproduce(github, *args, **kwargs):
                clients.append(github)
                value = next(outcomes)
                if isinstance(value, Exception):
                    raise value
                return value

            with patch.object(oss_replay, 'reproduce', reproduce), \
                    patch.object(oss_replay.gh, 'PublicGitHub', side_effect=lambda repo: object()):
                code = oss_replay.main(['--seeds', str(seeds), '--store', str(root / '.coldfix')])
            self.assertIsNot(*clients)
            oss_replay.gh.private_store(root / '.coldfix')
            reports = list((root / '.coldfix' / 'oss-replay').glob('run-*/report.json'))
            self.assertEqual(code, 2)
            self.assertEqual(len(reports), 1)
            report = json.loads(reports[0].read_text())
            self.assertEqual([r['status'] for r in report['records']], ['SKIPPED'])
            self.assertEqual(report['by_reason']['seed proven']['total'], 1)
            self.assertEqual(report['outcomes'], {'repository is not public': 1})
            self.assertIn('| repository is not public | 1 |', (reports[0].parent / 'report.md').read_text())


if __name__ == '__main__':
    unittest.main()
