import json
import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import codex_review_source as source


def setUpModule():
    owner = unittest.mock.patch.dict('os.environ', {'COLDFIX_OWNER': 'example-owner'})
    owner.start()
    unittest.addModuleCleanup(owner.stop)


BEFORE = 'a' * 40
AFTER = 'b' * 40
MARKER = '<!-- codex-code-review-finding:v1 -->'


def finding():
    return dict(id=11, user=dict(login='chatgpt-codex-connector[bot]'),
                body=f'![P2 Badge](https://img.shields.io/badge/P2-yellow) Fix the check.\n{MARKER}',
                path='app.py', original_commit_id=BEFORE, commit_id=BEFORE,
                original_line=8, original_start_line=None, side='RIGHT',
                pull_request_review_id=7, created_at='2026-10-20T10:00:00Z',
                html_url='https://github.com/example-owner/project/pull/1#discussion_r11')


def commit(sha, parent, files=()):
    return dict(sha=sha, parents=[dict(sha=parent)],
                commit=dict(committer=dict(date='2026-10-21T10:00:00Z')),
                files=list(files))


class FakeGitHub:
    repo = 'example-owner/project'

    def __init__(self, commits, comments=None, compare=None):
        self.commits = commits
        self.comments = [finding()] if comments is None else comments
        self.compare = commits[-1]['files'] if compare is None and commits else compare

    def pages(self, endpoint):
        if endpoint == 'pulls/comments':
            return self.comments
        if endpoint == 'pulls/1/commits':
            return [dict(sha=BEFORE), *[dict(sha=row['sha']) for row in self.commits]]
        raise AssertionError(endpoint)

    def api(self, endpoint):
        if endpoint == 'pulls/1/reviews/7':
            return dict(commit_id=BEFORE, user=dict(login='chatgpt-codex-connector[bot]'),
                        body=f'**Reviewed commit:** `{BEFORE[:10]}`')
        if endpoint.startswith('commits/'):
            return next(row for row in self.commits if row['sha'] == endpoint.split('/')[1].split('?')[0])
        if endpoint == f"compare/{BEFORE}...{self.commits[-1]['sha']}":
            return dict(files=self.compare)
        raise AssertionError(endpoint)


class PairTests(unittest.TestCase):
    def collect(self, commits, comments=None, compare=None):
        return source.collect(FakeGitHub(commits, comments, compare), Path('/absent-cache'))

    def test_fixed_finding_becomes_pair(self):
        rows = self.collect([commit(AFTER, BEFORE, [dict(filename='app.py',
            patch='@@ -7,3 +7,3 @@\n context\n-bug\n+fixed\n tail')])])
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['before'], rows[0]['after'], rows[0]['priority'], rows[0]['reason']),
                         (BEFORE, AFTER, 'P2', None))

    def test_unfixed_finding_is_skipped(self):
        rows = self.collect([])
        self.assertEqual(rows[0]['reason'], 'NO_LATER_COMMIT')
        self.assertIsNone(rows[0]['after'])

    def test_later_commit_touching_other_file_is_not_pair(self):
        rows = self.collect([commit(AFTER, BEFORE, [dict(filename='other.py', patch='@@ -8 +8 @@\n-bug\n+fixed')])])
        self.assertEqual(rows[0]['reason'], 'FLAGGED_HUNK_UNCHANGED')
        self.assertIsNone(rows[0]['after'])


class BoundaryTests(unittest.TestCase):
    collect = PairTests.collect

    def test_different_hunk_in_same_file_is_not_pair(self):
        rows = self.collect([commit(AFTER, BEFORE, [dict(filename='app.py', patch='@@ -30 +30 @@\n-old\n+new')])])
        self.assertEqual(rows[0]['reason'], 'FLAGGED_HUNK_UNCHANGED')
        self.assertIsNone(rows[0]['after'])

    def test_line_numbers_follow_insertions_in_earlier_commits(self):
        middle = 'c' * 40
        rows = self.collect([
            commit(middle, BEFORE, [dict(filename='app.py', patch='@@ -1,0 +2,2 @@\n+one\n+two')]),
            commit(AFTER, middle, [dict(filename='app.py', patch='@@ -10 +10 @@\n-bug\n+fixed')])],
            compare=[dict(filename='app.py', patch='@@ -1,0 +2,2 @@\n+one\n+two\n@@ -8 +10 @@\n-bug\n+fixed')])
        self.assertEqual((rows[0]['before'], rows[0]['after'], rows[0]['reason']), (BEFORE, AFTER, None))

    def test_fix_reverted_later_in_pr_is_not_pair(self):
        middle = 'c' * 40
        rows = self.collect([
            commit(middle, BEFORE, [dict(filename='app.py', patch='@@ -8 +8 @@\n-bug\n+fixed')]),
            commit(AFTER, middle, [dict(filename='app.py', patch='@@ -8 +8 @@\n-fixed\n+bug')])],
            compare=[])
        self.assertEqual(rows[0]['reason'], 'FIX_REVERTED_IN_PR')
        self.assertIsNone(rows[0]['after'])

    def test_truncated_pr_comparison_is_held(self):
        fix = dict(filename='app.py', patch='@@ -8 +8 @@\n-bug\n+fixed')
        rows = self.collect([commit(AFTER, BEFORE, [fix])],
                            compare=[fix, *[dict(filename=f'f{n}.py') for n in range(299)]])
        self.assertEqual(rows[0]['reason'], 'COMMIT_FILES_INCOMPLETE')
        self.assertIsNone(rows[0]['after'])

    def test_parent_must_be_the_previous_pr_commit(self):
        rows = self.collect([commit(AFTER, 'c' * 40, [dict(filename='app.py', patch='@@ -8 +8 @@\n-bug\n+fixed')])])
        self.assertEqual(rows[0]['reason'], 'NONLINEAR_PR_HISTORY')
        self.assertIsNone(rows[0]['after'])

    def test_commit_made_before_finding_is_not_fix(self):
        change = commit(AFTER, BEFORE, [dict(filename='app.py', patch='@@ -8 +8 @@\n-bug\n+fixed')])
        change['commit']['committer']['date'] = '2026-10-19T10:00:00Z'
        rows = self.collect([change])
        self.assertEqual(rows[0]['reason'], 'HUNK_CHANGED_BEFORE_REVIEW')
        self.assertIsNone(rows[0]['after'])

    def test_missing_patch_is_held(self):
        rows = self.collect([commit(AFTER, BEFORE, [dict(filename='app.py')])])
        self.assertEqual(rows[0]['reason'], 'PATCH_UNAVAILABLE')
        self.assertIsNone(rows[0]['after'])

    def test_truncated_patch_is_held(self):
        rows = self.collect([commit(AFTER, BEFORE, [dict(filename='app.py', patch='@@ -8,3 +8,3 @@\n-bug\n+fixed')])])
        self.assertEqual(rows[0]['reason'], 'PATCH_UNAVAILABLE')

    def test_only_marked_bot_findings_are_read(self):
        comments = [finding(), dict(finding(), id=12, user=dict(login='someone')),
                    dict(finding(), id=13, body='Unmarked P2'), dict(finding(), id=14, in_reply_to_id=11)]
        rows = self.collect([], comments)
        self.assertEqual([(row['finding_id'], row['reason']) for row in rows], [(11, 'NO_LATER_COMMIT')])

    def test_bad_badge_is_retained_as_skipped(self):
        rows = self.collect([], [dict(finding(), body='[P7] ' + MARKER)])
        self.assertEqual(rows[0]['reason'], 'PRIORITY_BADGE_UNAVAILABLE')

    def test_review_receipt_must_match_original_commit(self):
        class WrongReview(FakeGitHub):
            def api(self, endpoint):
                result = super().api(endpoint)
                if '/reviews/' in endpoint:
                    result['commit_id'] = AFTER
                return result
        rows = source.collect(WrongReview([]), Path('/absent-cache'))
        self.assertEqual(rows[0]['reason'], 'REVIEWED_COMMIT_UNAVAILABLE')

    def test_coldvault_receipt_avoids_review_api_get(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            target = cache / 'example-owner/project/Pull-Requests/0001.md'
            target.parent.mkdir(parents=True)
            target.write_text(f'source_id: example-owner/project/pull/1\n'
                f'## review: COMMENTED · chatgpt-codex-connector[bot] · 2026-10-20T10:00:00Z\n\n'
                f'> **Reviewed commit:** `{BEFORE[:10]}`\n')
            class CachedOnly(FakeGitHub):
                repo = 'example-owner/project'

                def api(self, endpoint):
                    raise AssertionError('receipt must come from cache')
            rows = source.collect(CachedOnly([]), cache)
            self.assertEqual((rows[0]['review_metadata'], rows[0]['before'], rows[0]['reason']),
                             ('coldvault', BEFORE, 'NO_LATER_COMMIT'))

    def test_vm_missing_never_executes_reviewed_code(self):
        from unittest.mock import patch
        row = dict(before=BEFORE, after=AFTER, reason=None, red_reproduced=False, proven=False)
        with patch.object(source.ci, 'vm_replay_tree', None, create=True), patch.object(source.ci, 'run', side_effect=AssertionError('host execution forbidden')):
            source.attempt(row, Path('/unused'), 1)
        self.assertEqual((row['reason'], row['proven']), ('VM_EXECUTOR_UNAVAILABLE', False))

    def test_vm01_uses_local_executor_without_a_nested_vm(self):
        from unittest.mock import patch
        diagnostic = json.dumps(dict(reason='known local diagnostic', diagnostic='f' * 64, tests=['e' * 64]))
        proof = dict(verification='proven', before=dict(status='FAIL', commit=BEFORE, stderr=diagnostic),
                     after=dict(status='PASS', commit=AFTER, stderr=diagnostic))
        row = dict(repo='example-owner/project', before=BEFORE, after=AFTER, reason=None,
                   red_reproduced=False, proven=False, promoted=False)
        def reverse(clone, before, after, plan, temporary, timeout, result):
            result['reversed'] = dict(status='FAIL', stderr=diagnostic)
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(source.c, 'on_replay_host', return_value=True), \
                patch.object(source.ci, 'vm_replay_tree', None), \
                patch.object(source.ci, 'run', return_value=b''), \
                patch('learn_replay.SourceProjects.mirror', create=True, return_value=Path(directory)) as mirror, \
                patch.object(source, 'replay_plan', return_value=dict(command=['python3', '-m', 'unittest'])), \
                patch.object(source, 'reverse_local', side_effect=reverse), \
                patch.object(source.c, 'repo_root', return_value='a' * 40), \
                patch.object(source.ci, 'replay_pair', return_value=proof) as replay:
            source.attempt(row, Path(directory), 30)
        mirror.assert_called_once_with('example-owner/project', (BEFORE, AFTER))
        self.assertTrue(row['proven'])
        self.assertIs(replay.call_args.kwargs['executor'], source.replay_isolation.replay_tree)

    def test_vm01_records_a_replay_timeout(self):
        from unittest.mock import patch
        row = dict(repo='example-owner/project', before=BEFORE, after=AFTER, reason=None,
                   red_reproduced=False, proven=False, promoted=False)
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(source.c, 'on_replay_host', return_value=True), \
                patch.object(source.ci, 'run', side_effect=subprocess.TimeoutExpired('git', 1)), \
                patch('learn_replay.SourceProjects.mirror', create=True,
                      side_effect=subprocess.TimeoutExpired('git', 1)) as mirror:
            source.attempt(row, Path(directory), 1)
        mirror.assert_called_once_with('example-owner/project', (BEFORE, AFTER))
        self.assertEqual((row['proven'], row['reason']), (False, 'REPLAY_TIMEOUT'))


class APITests(unittest.TestCase):
    def test_reserve_blocks_next_request_and_cache_is_reused(self):
        from unittest.mock import patch
        import json
        replies = [dict(resources=dict(core=dict(remaining=1001))), [dict(id=1)]]
        calls = []
        def run(command):
            calls.append(command)
            remaining = 1001 if len(calls) == 1 else 1000
            return (f'HTTP/2.0 200 OK\nX-Ratelimit-Remaining: {remaining}\n\n' + json.dumps(replies[len(calls) - 1])).encode()
        with tempfile.TemporaryDirectory() as directory, patch.object(source.ci, 'run', side_effect=run):
            api = source.API(Path(directory) / 'http')
            self.assertEqual(api.get('repos/example-owner/project/pulls/comments'), [dict(id=1)])
            self.assertEqual(api.get('repos/example-owner/project/pulls/comments'), [dict(id=1)])
            with self.assertRaisesRegex(source.ci.Skipped, 'API_RESERVE_REACHED'):
                api.get('repos/example-owner/project/commits')
            self.assertEqual((api.requests, api.remaining, len(calls)), (2, 1000, 2))
            self.assertTrue(all(command[command.index('--method') + 1] == 'GET' for command in calls))


    def test_failed_get_is_counted_and_not_retried(self):
        from unittest.mock import patch
        replies = [b'HTTP/2.0 200 OK\nX-Ratelimit-Remaining: 1003\n\n{"resources":{"core":{"remaining":1003}}}',
                   source.ci.Skipped('read operation unavailable')]
        with tempfile.TemporaryDirectory() as directory, patch.object(source.ci, 'run', side_effect=replies) as run:
            api = source.API(Path(directory) / 'http')
            for _ in range(2):
                with self.assertRaises(source.ci.Skipped):
                    api.get('repos/example-owner/project/commits/missing')
            self.assertEqual((api.requests, api.remaining, run.call_count), (2, 1002, 2))


class ReplayTests(unittest.TestCase):
    def test_incomplete_retry_keeps_the_prior_red_observation(self):
        from unittest.mock import patch
        diagnostic = json.dumps(dict(reason='temporarily unavailable', diagnostic='f' * 64, tests=['e' * 64]))
        proof = dict(verification='unproven', before=dict(status='INCOMPLETE', stderr=diagnostic),
                     after=dict(status='INCOMPLETE', stderr=diagnostic))
        artifact = 'review-proof-' + 'a' * 32 + '.json'
        row = dict(repo='example-owner/project', before=BEFORE, after=AFTER, reason=None,
                   red_reproduced=True, proven=False, promoted=False, artifact=artifact)
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(source.c, 'on_replay_host', return_value=False), \
                patch.object(source.ci, 'vm_replay_tree', lambda: None), \
                patch.object(source.ci, 'run', return_value=b''), \
                patch('learn_replay.SourceProjects.mirror', create=True, return_value=Path(directory)) as mirror, \
                patch.object(source, 'replay_plan', return_value=dict(command=['python3', '-m', 'unittest'])), \
                patch.object(source.workflow_replay, 'replay_pair', return_value=proof):
            root = Path(directory)
            (root / artifact).write_text('{"prior":"red"}')
            source.attempt(row, root, 30)
            self.assertTrue(row['red_reproduced'])
            self.assertEqual(row['artifact'], artifact)
            self.assertEqual((root / artifact).read_text(), '{"prior":"red"}')
            row['red_reproduced'] = False
            row['reason'] = None
            proof['before']['status'] = 'FAIL'
            source.attempt(row, root, 30)
            self.assertTrue(row['red_reproduced'])
            self.assertEqual(row['artifact'], artifact)
            observed = json.loads((root / row['artifacts'][0]).read_text())
            self.assertEqual(observed['replay']['before']['status'], 'FAIL')
            mirror.assert_called_with('example-owner/project', (BEFORE, AFTER))

    def test_reverse_failure_retains_completed_red_and_green_evidence(self):
        from unittest.mock import patch
        diagnostic = json.dumps(dict(reason='known local diagnostic', diagnostic='f' * 64, tests=['e' * 64]))
        proof = dict(verification='proven', before=dict(status='FAIL', commit=BEFORE, stderr=diagnostic),
                     after=dict(status='PASS', commit=AFTER, stderr=diagnostic))
        row = dict(repo='example-owner/project', before=BEFORE, after=AFTER, reason=None,
                   red_reproduced=False, proven=False, promoted=False)
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(source.c, 'on_replay_host', return_value=True), \
                patch.object(source.ci, 'run', return_value=b''), \
                patch('learn_replay.SourceProjects.mirror', create=True, return_value=Path(directory)) as mirror, \
                patch.object(source, 'replay_plan', return_value=dict(command=['python3', '-m', 'unittest'])), \
                patch.object(source.ci, 'replay_pair', return_value=proof), \
                patch.object(source, 'reverse_local', side_effect=source.c.ColdfixError('patch unavailable')):
            root = Path(directory)
            source.attempt(row, root, 30)
            mirror.assert_called_once_with('example-owner/project', (BEFORE, AFTER))
            retained = json.loads((root / row['artifact']).read_text())
            self.assertTrue(row['red_reproduced'])
            self.assertFalse(row['proven'])
            self.assertEqual(row['reason'], 'reversed replay unavailable: patch unavailable')
            self.assertEqual([retained['replay'][side]['status'] for side in ('before', 'after')], ['FAIL', 'PASS'])

    def test_workflow_command_is_reused_without_running_it_on_host(self):
        import subprocess
        workflow = """name: verify
on: [push, pull_request]
jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Test
        run: python3 -m unittest discover -s tests -v
"""
        with tempfile.TemporaryDirectory() as directory:
            clone = Path(directory)
            target = clone / '.github/workflows/verify.yml'
            target.parent.mkdir(parents=True)
            target.write_text(workflow)
            for argv in (['git', 'init', '--quiet', str(clone)],
                         ['git', '-C', str(clone), 'add', '.github'],
                         ['git', '-C', str(clone), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test', 'commit', '--quiet', '-m', 'fixture']):
                subprocess.run(argv, check=True, capture_output=True)
            plan = source.replay_plan(clone, 10)
            self.assertEqual((plan['source_command'], plan['kind'], plan['workflow'], plan['job']),
                ('python3 -m unittest discover -s tests -v', 'test', '.github/workflows/verify.yml', 'verify'))

    def declared_plan(self, files):
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            clone = Path(directory)
            for name, content in files.items():
                (clone / name).parent.mkdir(parents=True, exist_ok=True)
                (clone / name).write_bytes(content)
            for argv in (['git', 'init', '--quiet', str(clone)], ['git', '-C', str(clone), 'add', '.'],
                         ['git', '-C', str(clone), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test',
                          'commit', '--quiet', '-m', 'fixture']):
                subprocess.run(argv, check=True, capture_output=True)
            try:
                plan = source.replay_plan(clone, 10)
            except source.ci.Skipped as error:
                return str(error)
            return plan['source_command'], plan['kind'], plan['workflow']

    def test_repository_without_a_hosted_workflow_replays_its_declared_test_command(self):
        import regression_adapter
        recipes = [regression_adapter.CANONICAL_VERIFY, regression_adapter.PARALLEL_VERIFY]
        own = Path(__file__).resolve().parents[1] / '.fleet' / 'verify.sh'
        if own.exists():
            recipes.append(own.read_bytes())
        for recipe in recipes:
            self.assertEqual(self.declared_plan({'.fleet/verify.sh': recipe, 'tests/test_a.py': b''}),
                             ('python3 -m unittest discover -s tests', 'test', '.fleet/verify.sh'))
        toml = b'[checks]\ntest = true\n[commands]\ntest = "python3 -m unittest discover -s checks"\n'
        self.assertEqual(self.declared_plan({'coldbox.toml': toml, '.fleet/verify.sh': b'#!/bin/sh\n'}),
                         ('python3 -m unittest discover -s checks', 'test', 'coldbox.toml'))
        self.assertEqual(self.declared_plan({'coldbox.toml': toml.replace(b'test = true', b'test = false'),
                                             '.fleet/verify.sh': b'#!/bin/sh\nbash verify.sh --dry-run\n'}), 'CI_COMMAND_UNAVAILABLE')
        self.assertEqual(self.declared_plan({'README.md': b'fixture\n'}), 'CI_COMMAND_UNAVAILABLE')

    def test_a_recipe_one_byte_off_a_pinned_one_is_refused(self):
        import regression_adapter
        for recipe in regression_adapter.VERIFY_RECIPES:
            for changed in (recipe[:-1], recipe + b'\n', recipe.replace(b'unittest', b'unittesT', 1)):
                self.assertEqual(self.declared_plan({'.fleet/verify.sh': changed, 'tests/test_a.py': b''}),
                                 'CI_COMMAND_UNAVAILABLE')

    def test_declared_command_is_read_at_the_reviewed_revision(self):
        import regression_adapter
        with tempfile.TemporaryDirectory() as directory:
            clone = Path(directory)
            (clone / '.fleet').mkdir()
            (clone / '.fleet/verify.sh').write_bytes(regression_adapter.PARALLEL_VERIFY)
            git = ['git', '-C', str(clone), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test']
            subprocess.run(['git', 'init', '--quiet', str(clone)], check=True, capture_output=True)
            subprocess.run(['git', '-C', str(clone), 'add', '.'], check=True, capture_output=True)
            subprocess.run(git + ['commit', '--quiet', '-m', 'reviewed'], check=True, capture_output=True)
            reviewed = subprocess.run(['git', '-C', str(clone), 'rev-parse', 'HEAD'], check=True,
                                      capture_output=True, text=True).stdout.strip()
            subprocess.run(git + ['rm', '--quiet', '.fleet/verify.sh'], check=True, capture_output=True)
            subprocess.run(git + ['commit', '--quiet', '-m', 'later'], check=True, capture_output=True)
            self.assertEqual(source.replay_plan(clone, 10, reviewed)['workflow'], '.fleet/verify.sh')
            with self.assertRaisesRegex(source.ci.Skipped, 'CI_COMMAND_UNAVAILABLE'):
                source.replay_plan(clone, 10)
        self.assertTrue(source.retryable('CI_COMMAND_UNAVAILABLE'))

    def test_pair_uses_existing_replay_and_never_promotes_on_source_finding(self):
        import json
        from unittest.mock import patch
        diagnostic = json.dumps(dict(reason='known local diagnostic', diagnostic='f' * 64, tests=['e' * 64]))
        proof = dict(verification='proven', before=dict(status='FAIL', commit=BEFORE, stderr=diagnostic),
                     after=dict(status='PASS', commit=AFTER, stderr=diagnostic),
                     reversed=dict(status='FAIL', commit=AFTER, stderr=diagnostic))
        row = dict(repo='example-owner/project', before=BEFORE, after=AFTER, reason=None,
                   red_reproduced=False, proven=False, promoted=False)
        observed = []
        def replay(clone, before, after, plan, temporary, timeout):
            observed.append((before, after, plan, timeout))
            return proof
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(source.ci, 'vm_replay_tree', lambda: None, create=True), \
                patch.object(source.ci, 'run', return_value=b''), \
                patch('learn_replay.SourceProjects.mirror', create=True, return_value=Path(directory)) as mirror, \
                patch.object(source, 'replay_plan', return_value=dict(command=['python3', '-m', 'unittest'])), \
                patch.object(source.workflow_replay, 'replay_pair', side_effect=replay), \
                patch.object(source.ci, 'promote', side_effect=AssertionError('self grading forbidden')):
            root = Path(directory)
            source.attempt(row, root, 30)
            mirror.assert_called_once_with('example-owner/project', (BEFORE, AFTER))
            retained = json.loads((root / row['artifact']).read_text())
            self.assertEqual(observed,[(BEFORE, AFTER, dict(command=['python3', '-m', 'unittest']), 30)])
            self.assertEqual((row['red_reproduced'], row['proven'], row['promoted'], row['reason']), (True, True, False, None))
            self.assertEqual(retained['replay']['before']['commit'], BEFORE)
            self.assertEqual(retained['replay']['reversed']['status'], 'FAIL')
            self.assertIn('source finding cannot grade its lesson', retained['promotion_hold'])
            passed_probe = dict(repo='example-owner/project', before=BEFORE, after=AFTER, reason=None,
                                red_reproduced=False, proven=False, promoted=False)
            probe_diagnostic = json.dumps(dict(reason='local command passed', diagnostic='', tests=[]))
            with patch.object(source.workflow_replay, 'replay_pair', return_value=dict(
                    verification='unproven', probe=dict(status='PASS', stderr=probe_diagnostic))):
                source.attempt(passed_probe, root, 30)
            self.assertEqual((passed_probe['red_reproduced'], passed_probe['proven'], passed_probe['reason']),
                             (False, False, 'local command passed'))
            unavailable = dict(row, proven=False, reason=None)
            with patch.object(source.c, 'atomic_create', side_effect=source.c.ColdfixError('storage unavailable')):
                source.attempt(unavailable, root, 30)
            self.assertEqual((unavailable['proven'], unavailable['reason']), (False, 'REPLAY_METADATA_UNAVAILABLE'))


class CliTests(unittest.TestCase):
    def test_resume_preserves_prior_proof_artifacts(self):
        import argparse
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = 'review-proof-' + 'a' * 32 + '.json'
            additional = 'review-proof-' + 'b' * 32 + '.json'
            (root / artifact).write_text('{"proof":"retained"}')
            (root / additional).write_text('{"proof":"additional"}')
            report = dict(version=1, counts={}, results=[dict(reason=None, red_reproduced=True,
                          proven=True, promoted=False, artifact=artifact, artifacts=[additional])])
            source_path = root / 'input.json'
            source_path.write_text(json.dumps(report))
            result, output = source.resume(argparse.Namespace(store=str(root / '.coldfix'),
                                                              resume=str(source_path), timeout=30))
            self.assertEqual((output / result['results'][0]['artifact']).read_text(), '{"proof":"retained"}')
            self.assertEqual((output / result['results'][0]['artifacts'][0]).read_text(), '{"proof":"additional"}')

    def test_resume_retries_only_vm_unavailable_pairs_and_records_the_reason(self):
        import argparse
        from unittest.mock import patch
        held = dict(repo='example-owner/project', before=BEFORE, after=AFTER, reason='VM_EXECUTOR_UNAVAILABLE',
                    red_reproduced=False, proven=False, promoted=False)
        timed_out = dict(held, reason='REPLAY_TIMEOUT')
        tool_timeout = dict(held, reason='tool unavailable or operation timed out')
        reverse_timeout = dict(held, reason='reversed replay unavailable: tool unavailable or operation timed out')
        reverse_permanent = dict(held, reason='reversed replay unavailable: unsafe patch')
        skipped = dict(reason='NO_LATER_COMMIT', red_reproduced=False, proven=False, promoted=False)
        legacy = dict(held, reason='CI_COMMAND_UNAVAILABLE_OR_AMBIGUOUS')
        report = dict(version=1, counts={}, results=[held, timed_out, tool_timeout, reverse_timeout, reverse_permanent, skipped, legacy])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_path = root / 'input.json'
            source_path.write_text(json.dumps(report))
            def reject(row, output, timeout):
                row['reason'] = 'CI_COMMAND_UNAVAILABLE'
            with patch.object(source, 'attempt', side_effect=reject):
                result, output = source.resume(argparse.Namespace(store=str(root / '.coldfix'),
                                                                  resume=str(source_path), timeout=30))
            self.assertEqual(result['skipped_by_reason'],
                             {'CI_COMMAND_UNAVAILABLE': 5, 'NO_LATER_COMMIT': 1,
                              'reversed replay unavailable: unsafe patch': 1})
            self.assertEqual(json.loads((output / 'codex-review-source.json').read_text()), result)
            self.assertEqual(json.loads(source_path.read_text()), report)

    def test_resume_retries_an_unavailable_pair_once_more_per_code_revision(self):
        import argparse
        from unittest.mock import patch
        row = dict(repo='example-owner/project', before=BEFORE, after=AFTER, reason='CI_COMMAND_UNAVAILABLE_OR_AMBIGUOUS',
                   red_reproduced=False, proven=False, promoted=False)
        attempts = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = root / 'input.json'
            current.write_text(json.dumps(dict(version=1, counts={}, results=[row])))
            for day in ('2026-11-01', '2026-11-02', '2026-11-03'):
                def unavailable(row, output, timeout):
                    attempts.append(day)
                    row['reason'] = 'CI_COMMAND_UNAVAILABLE_OR_AMBIGUOUS'
                with patch.object(source, 'attempt', side_effect=unavailable), \
                        patch.object(source.learn_replay, 'utc_day', return_value=day):
                    result, output = source.resume(argparse.Namespace(store=str(root / '.coldfix'),
                                                                      resume=str(current), timeout=30))
                current = output / 'codex-review-source.json' if output != current.parent else current
            self.assertEqual(attempts, ['2026-11-01', '2026-11-02'])
            self.assertEqual(result['results'][0]['reason'], 'CI_COMMAND_UNAVAILABLE_OR_AMBIGUOUS')

    def test_resume_stops_at_its_budget_and_the_next_resume_continues(self):
        import argparse
        from unittest.mock import patch
        held = dict(repo='example-owner/project', before=BEFORE, after=AFTER, reason='VM_EXECUTOR_UNAVAILABLE',
                    red_reproduced=False, proven=False, promoted=False)
        clock = [0.0]

        def slow(row, output, timeout):
            clock[0] += 100
            row['reason'] = 'local command passed'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = root / 'input.json'
            current.write_text(json.dumps(dict(version=1, counts={}, results=[dict(held) for _ in range(3)])))
            seen = []
            for _ in range(3):
                with patch.object(source, 'attempt', side_effect=slow), \
                        patch('time.monotonic', side_effect=lambda: clock[0]):
                    result, output = source.resume(argparse.Namespace(store=str(root / '.coldfix'),
                                                                      resume=str(current), timeout=30, budget=50))
                current = output / 'codex-review-source.json'
                seen.append((result.get('deferred'), [row['reason'] for row in result['results']].count(held['reason'])))
                self.assertEqual(json.loads(current.read_text()), result)
            self.assertEqual(seen, [(2, 2), (1, 1), (0, 0)])

    def test_every_due_pair_is_reached_when_each_run_affords_one_replay(self):
        import argparse
        from unittest.mock import patch
        rows = [dict(repo='example-owner/project', finding_id=index, before=BEFORE, after=AFTER,
                     reason='replay timed out or checkout unavailable', red_reproduced=False, proven=False, promoted=False)
                for index in range(3)]
        clock, attempted = [0.0], []

        def times_out(row, output, timeout):
            clock[0] += 100
            attempted.append(row['finding_id'])
            row['reason'] = 'replay timed out or checkout unavailable'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = root / 'input.json'
            current.write_text(json.dumps(dict(version=1, counts={}, results=rows)))
            for deploy in range(6):
                with patch.object(source, 'attempt', side_effect=times_out), \
                        patch.object(source.learn_replay, 'code_revision', return_value=str(deploy)), \
                        patch('time.monotonic', side_effect=lambda: clock[0]):
                    result, output = source.resume(argparse.Namespace(store=str(root / '.coldfix'),
                                                                      resume=str(current), timeout=30, budget=50))
                current = output / 'codex-review-source.json'
            self.assertEqual(attempted, [0, 1, 2, 0, 1, 2])

    def test_skips_are_reported_by_class(self):
        import argparse
        rows = [dict(repo='example-owner/project', before=BEFORE, after=None, reason='NO_LATER_COMMIT'),
                dict(repo='example-owner/project', before=BEFORE, after=AFTER, reason='REPLAY_TIMEOUT'),
                dict(repo='example-owner/project', before=BEFORE, after=AFTER, reason='local command passed'),
                dict(repo='example-owner/project', before=BEFORE, after=AFTER, reason=None)]
        for row in rows:
            row.update(red_reproduced=False, proven=False, promoted=False)
        rows[1]['retried'] = dict(revision=source.learn_replay.code_revision(), day='2026-11-01', confirmed=True)
        with tempfile.TemporaryDirectory() as directory, \
                unittest.mock.patch.object(source.learn_replay, 'utc_day', return_value='2026-11-02'):
            current = Path(directory) / 'input.json'
            current.write_text(json.dumps(dict(version=1, counts={}, results=rows)))
            result, _ = source.resume(argparse.Namespace(store=str(Path(directory) / '.coldfix'),
                                                         resume=str(current), timeout=30))
        self.assertEqual(result['skipped_by_class'], dict(no_fix_pair=dict(NO_LATER_COMMIT=1),
                                                          retry_pending=dict(REPLAY_TIMEOUT=1),
                                                          replay_failed={'local command passed': 1}))

    def test_report_counts_pairs_and_keeps_unproven_output_private(self):
        import argparse
        import json
        from unittest.mock import patch
        api = argparse.Namespace(requests=0, remaining=4000)
        github = FakeGitHub([commit(AFTER, BEFORE, [dict(filename='app.py', patch='@@ -8 +8 @@\n-bug\n+fixed')])])
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(source, 'API', return_value=api), \
                patch.object(source, 'GitHub', return_value=github), \
                patch.object(source.ci, 'vm_replay_tree', None, create=True):
            args = argparse.Namespace(repo=['example-owner/project'], owner=None,
                store=str(Path(directory) / '.coldfix'), cache='/absent-cache', timeout=1)
            report, output = source.execute(args)
            retained = json.loads((output / 'codex-review-source.json').read_text())
            self.assertEqual(retained['counts'], dict(findings_found=1, fixed_in_pr_pairs=1,
                red_reproduced=0, proven=0, promoted=0, skipped=1))
            self.assertEqual(retained['skipped_by_reason'], dict(VM_EXECUTOR_UNAVAILABLE=1))
            self.assertEqual((output.stat().st_mode & 0o077, (output / 'codex-review-source.json').stat().st_mode & 0o077), (0, 0))
            self.assertIn('Findings found 1; fixed-in-PR pairs 1; red reproduced 0; proven 0; promoted 0.', source.markdown(report))


class OwnerTests(unittest.TestCase):
    def test_only_the_configured_owner_is_replayed(self):
        self.assertTrue(source.owned('Example-Owner/project'))
        self.assertFalse(source.owned('someone-else/project'))
        self.assertTrue(source.owned('someone-else/project', 'someone-else'))
        with unittest.mock.patch.dict('os.environ', {'COLDFIX_OWNER': ''}):
            self.assertFalse(source.owned('example-owner/project'))


if __name__ == '__main__':
    unittest.main()
