import hashlib
import inspect
import json
import os
import shlex
import socket
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import check_delivery
import ci_batch
import ci_fix_learning
import coldfix as c
import coldrecall
import failure_ingest
import incident_admission
import learn_replay
import learn_report
import regression_adapter
import replay_env
import replay_repos
import replay_isolation
import test_learn

from datetime import datetime, timezone

SPILLED = "the fix's edit of the finding's flagged lines also adds or changes unflagged lines"

LOOPBACK = socket.inet_ntoa(socket.INADDR_LOOPBACK.to_bytes(4, 'big'))


class LearnReplayTests(unittest.TestCase):
    def test_coldpass_commit_survives_selection_pair_and_checkout(self):
        import learn_candidates
        before, after = 'a' * 40, 'b' * 40
        import coldpass_replay
        failure = dict(id='f' * 32, repo='example/project', kind='test', command='verify',
                       source_ref='coldpass:run:fixture:value',
                       error=json.dumps(dict(type='coldpass', repo='example/project', run_id='fixture',
                            failure_class='value', repository_id=123, commit=before, log_tail='FAIL: test_value')))
        row = learn_replay.flakiness.metadata(failure)
        self.assertEqual(learn_candidates.revision(row), before)
        pair = dict(type='coldpass', failure_id=failure['id'], source_ref=failure['source_ref'], run_id='fixture',
                    repo=failure['repo'], repository_id=123, failure_class='value', before=before, after=after,
                    source_payload_sha256=c.digest(failure['error']))
        pair['binding_sha256'] = coldpass_replay.binding_digest(pair)
        generic = dict(repo=failure['repo'], before=before, after=after)
        self.assertEqual(list(learn_replay.candidates({'failures': {failure['id']: failure}},
                         {'evidence': [failure['id']]}, [generic])), [])
        self.assertEqual(list(learn_replay.candidates({'failures': {failure['id']: failure}},
                                                      {'evidence': [failure['id']]}, [pair])), [(failure, pair)])
        with tempfile.TemporaryDirectory() as directory:
            projects = learn_replay.SourceProjects(None, Path(directory), 'example', 30)
            with patch.object(projects, 'mirror', return_value=Path(directory)) as mirror:
                projects.get(failure)
            self.assertEqual(mirror.call_args.args[1], [before])

    def test_revision_alias_conflicts_and_invalid_values_are_not_replayable(self):
        import learn_candidates
        before = 'a' * 40
        for metadata in (dict(type='coldpass'), dict(type='coldpass', commit='bad'),
                         dict(type='coldpass', commit=before, head_sha='b' * 40),
                         dict(type='coldpass', commit=before, head_sha='bad')):
            with self.subTest(metadata=metadata):
                failure = dict(id='f' * 32, repo='example/project', kind='test', error=json.dumps(metadata))
                self.assertIsNone(learn_candidates.revision(metadata))
                self.assertEqual(list(learn_replay.candidates({'failures': {failure['id']: failure}},
                    {'evidence': [failure['id']]}, [dict(repo=failure['repo'], before=value, after='b' * 40) for value in (before, None)])), [])
        self.assertEqual(learn_candidates.revision(dict(type='coldpass', commit='A' * 64)), 'a' * 64)

    def test_legacy_aliases_preserve_selection_and_replay_behavior(self):
        import learn_candidates
        good = 'a' * 40
        metadata = dict(type='ci', head_sha='bad', sha=good, head='b' * 40)
        failure = dict(id='f' * 32, repo='example/project', kind='test', source='manual', command='verify',
                       error=json.dumps(dict(metadata, log_tail='FAIL: test_value')))
        rows = learn_candidates.candidates({'failures': {failure['id']: failure}, 'passes': {}})
        self.assertEqual([row['evidence'] for row in rows], [[failure['id']]])
        coldpass = dict(failure, error=json.dumps(dict(type='coldpass', commit=good, log_tail='FAIL: test_value')))
        rows = learn_candidates.candidates({'failures': {coldpass['id']: coldpass}, 'passes': {}})
        self.assertEqual([row['evidence'] for row in rows], [[coldpass['id']]])
        pair = dict(repo=failure['repo'], before='bad', after='b' * 40)
        self.assertEqual(list(learn_replay.candidates({'failures': {failure['id']: failure}},
            {'evidence': [failure['id']]}, [pair])), [(failure, pair)])

    def test_failure_revision_checks_git_and_legacy_length(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test', '-c', 'user.email=test@example.com',
                            'commit', '--allow-empty', '-qm', 'initial'], check=True)
            sha = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
            def resolve(metadata):
                return c.failure_revision({'error': json.dumps(metadata)}, repo)
            self.assertEqual(resolve(dict(type='coldpass', commit=sha)), sha)
            self.assertEqual(resolve(dict(type='ci', head_sha=sha, sha='bad')), sha)
            with patch.object(c, 'privacy_git', return_value=b'valid'):
                self.assertIsNone(resolve(dict(type='ci', head_sha='a' * 64)))
            for metadata in (dict(type='coldpass'), dict(type='coldpass', commit='bad'),
                             dict(type='coldpass', commit=sha, head_sha='a' * 40),
                             dict(type='coldpass', commit='a' * 40), dict(type='ci', head_sha='a' * 64)):
                with self.subTest(metadata=metadata):
                    self.assertIsNone(resolve(metadata))

    def test_source_projects_requires_real_recorded_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / 'project'
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            subprocess.run(['git', '-C', str(repo), 'remote', 'add', 'origin', 'https://github.com/example/project.git'], check=True)
            subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test', '-c', 'user.email=test@example.com',
                            'commit', '--allow-empty', '-qm', 'initial'], check=True)
            sha = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
            projects = learn_replay.SourceProjects(root, root, 'example', 30)
            failure = dict(repo='example/project', error=json.dumps(dict(type='coldpass', commit=sha)))
            self.assertEqual(projects.get(failure), repo)
            (repo / 'test_value.py').write_text('def test_value():\n    assert True\n')
            subprocess.run(['git', '-C', str(repo), 'add', 'test_value.py'], check=True)
            subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test', '-c', 'user.email=test@example.com',
                            'commit', '-qm', 'fix'], check=True)
            after = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
            failure['error'] = json.dumps(dict(type='coldpass', commit=sha, log_tail='FAIL: test_value'))
            self.assertEqual(learn_replay.prepare(failure, dict(before=sha, after=after), projects, 30),
                             (repo, sha, after, 'test_value.py', 'FAIL: test_value'))
            failure['error'] = json.dumps(dict(type='coldpass', commit='a' * 40))
            with self.assertRaises(c.ColdfixError):
                projects.get(failure)

    def test_a_pytest_failure_prepares_each_named_test_file_and_node_in_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / 'project'
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            subprocess.run(['git', '-C', str(repo), 'remote', 'add', 'origin', 'https://github.com/example/project.git'], check=True)
            commit = ['git', '-C', str(repo), '-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '-q']
            subprocess.run(commit + ['--allow-empty', '-m', 'initial'], check=True)
            sha = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
            (repo / 'value.py').write_text('value = 2\n')
            subprocess.run(['git', '-C', str(repo), 'add', 'value.py'], check=True)
            subprocess.run(commit + ['-m', 'fix'], check=True)
            after = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
            first, second = 'FAILED tests/test_value.py::Tests::test_value', 'FAILED tests/unit/test_other.py::test_other[1-2]'
            log = f'2026-01-01T00:00:00Z {first} - AssertionError: 1 != 2\n{second} - assert 1 == 2\n2 failed in 0.1s'
            failure = dict(repo='example/project', error=json.dumps(dict(type='coldpass', commit=sha, log_tail=log)))
            projects = learn_replay.SourceProjects(root, root, 'example', 30)
            pair = dict(before=sha, after=after)
            self.assertEqual(learn_replay.prepare(failure, pair, projects, 30), (repo, sha, after, 'tests/test_value.py', first))
            self.assertEqual(learn_replay.prepare(failure, pair, projects, 30, skip={('tests/test_value.py', first)}),
                             (repo, sha, after, 'tests/unit/test_other.py', second))
            self.assertIsNone(learn_replay.recorded_command(failure, repo))
            mixed = 'FAIL: test_missing (tests.test_value.Tests.test_missing)\n' + log
            failure['error'] = json.dumps(dict(type='coldpass', commit=sha, log_tail=mixed))
            self.assertEqual(learn_replay.prepare(failure, pair, projects, 30), (repo, sha, after, 'tests/test_value.py', first))

    def test_a_pytest_failure_whose_every_node_lacks_an_assertion_message_is_rejected_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / 'project'
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            subprocess.run(['git', '-C', str(repo), 'remote', 'add', 'origin', 'https://github.com/example/project.git'], check=True)
            commit = ['git', '-C', str(repo), '-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '-q']
            subprocess.run(commit + ['--allow-empty', '-m', 'initial'], check=True)
            before = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
            subprocess.run(commit + ['--allow-empty', '-m', 'fix'], check=True)
            after = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
            nodes = [f'FAILED tests/test_{name}.py::test_{name}' for name in ('a', 'b', 'c')]
            environment = [nodes[0] + " - AttributeError: module 'os' has no attribute 'getuid'",
                           nodes[1] + ' - Failed: async def functions are not natively supported.',
                           nodes[2] + " - AttributeError: module 'os' has no attribute 'getuid'"]
            failure = dict(id='f' * 32, repo='example/project', kind='ci',
                           error=json.dumps(dict(type='coldpass', commit=before, log_tail='\n'.join(environment))))
            projects = learn_replay.SourceProjects(root, root, 'example', 30)
            pair = dict(before=before, after=after)
            ledger = dict(runs=[], drafts={'draft': dict(verdict='PASS', evidence=[failure['id']])})
            (root / 'learn-drafts.json').write_text(json.dumps(ledger))
            pairs = root / 'pairs.json'
            pairs.write_text(json.dumps(dict(results=[])))
            admitted = []

            def admit(args, root, store):
                admitted.append(args)
                raise c.ColdfixError('recorded pytest failure has no assertion message')

            with patch.object(learn_replay, 'utc_day', return_value='2026-11-13'), \
                    patch.object(learn_replay.c, 'load_store', return_value=(root, {'failures': {failure['id']: failure}})), \
                    patch.object(learn_replay, 'prior_proof', return_value=None), \
                    patch.object(learn_replay, 'replay_candidates', side_effect=lambda *args: iter([(failure, pair)])), \
                    patch.object(learn_replay, 'admission_args', side_effect=lambda *args: args[4:6]), \
                    patch.object(learn_replay.incident_admission, 'admit', side_effect=admit):
                for _ in range(2):
                    learn_replay.run_in_workspace(root, ledger, projects, pairs, 30)
            entry = json.loads((root / 'learn-drafts.json').read_text())['drafts']['draft']
            self.assertEqual(admitted, [])
            self.assertEqual([row['reason'] for row in entry['replay_attempts']],
                             ['recorded pytest failure has no assertion message'])
            self.assertEqual(entry['replay']['status'], 'rejected')
            self.assertEqual(learn_replay.classify_reason(entry['replay']['reason']),
                             [('not_provable', 'recorded pytest failure has no assertion message')])

            for later in (nodes[2] + ' - AssertionError: 1 != 2', nodes[0] + ' - assert 1 == 2'):
                with self.subTest(later=later):
                    failure['error'] = json.dumps(dict(type='coldpass', commit=before,
                                                       log_tail='\n'.join(environment + [later])))
                    self.assertEqual(learn_replay.prepare(failure, pair, projects, 30),
                                     (repo, before, after, 'tests/test_a.py', nodes[0]))

    def test_failed_invalidated_refresh_keeps_the_draft_proven_with_its_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proof = dict(id='a' * 32, lesson_id='b' * 32, receipt_id='c' * 32, status='PASS', verification='proven')
            entry = dict(verdict='PASS', evidence=['failure'], replay=dict(status='proven', repo=str(root), proof=proof))
            ledger = dict(drafts={'candidate': entry}, runs=[])
            (root / 'learn-drafts.json').write_text(json.dumps(ledger))
            pairs = root / 'pairs.json'
            pairs.write_text(json.dumps(dict(results=[])))
            with patch.object(learn_replay.c, 'read_invalidations', return_value={proof['lesson_id']: proof['id']}), \
                    patch.object(learn_replay, 'refresh_proof', side_effect=c.ColdfixError('replay failed')):
                learn_replay.run(root, ledger, projects=root, pairs_path=pairs)
            self.assertEqual(entry['replay']['status'], 'proven')
            self.assertIn('replay failed', entry['replay']['reason'])

    def test_a_proof_reused_from_a_prior_lesson_runs_the_full_suite_before_it_is_kept(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = self.settle_ledger(root)
            proof = dict(id='a' * 32, lesson_id='b' * 32, receipt_id='c' * 32, status='PASS', verification='proven')
            store = dict(receipts={'c' * 32: dict(before=dict(commit='e' * 40), after=dict(commit='d' * 40))})
            suite = dict(status='FAIL', new_failures=['test_y'])
            with patch.object(learn_replay.incident_admission, 'full_suite', return_value=suite) as ran:
                self.settle_replays(root, ledger, '2026-11-01', None, store=store,
                                    prior=lambda *args: (root, dict(proof)))
            self.assertEqual(ran.call_args.args[1:4], (root, 'e' * 40, 'd' * 40))
            self.assertEqual(ledger['drafts']['draft']['replay']['proof']['full_suite'], suite)

    def test_recorded_command_witness_supports_linked_worktree(self):
        replay_isolation.require_sandbox()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / 'repo'
            (repo / 'tests').mkdir(parents=True)
            def git(*args):
                return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()
            git('init', '-q')
            git('config', 'user.name', 'Fixture')
            git('config', 'user.email', 'fixture@example.invalid')
            (repo / 'tests/test_value.py').write_text('import unittest\nclass T(unittest.TestCase):\n def test_value(self): pass\n')
            git('add', '.')
            git('commit', '-qm', 'test')
            worktree = root / 'worktree'
            git('worktree', 'add', '-q', '--detach', str(worktree))
            script, _, _ = regression_adapter.build_command(['python3', '-m', 'unittest', 'discover', '-s', 'tests'],
                                                               'FAILED (', git('rev-parse', 'HEAD:tests'))
            check = root / 'check.py'
            check.write_text(script)
            result = subprocess.run([sys.executable, str(check)], cwd=worktree, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalidated_retired_lesson_is_not_replayed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = dict(id='a' * 32, lesson_id='b' * 32, receipt_id='c' * 32, status='PASS', verification='proven')
            entry = dict(verdict='PASS', evidence=['failure'], replay=dict(status='proven', repo=str(root), proof=old))
            ledger = dict(drafts={'candidate': entry}, runs=[])
            (root / 'learn-drafts.json').write_text(json.dumps(ledger))
            pairs = root / 'pairs.json'
            pairs.write_text(json.dumps(dict(results=[])))
            latest = {'id': 'f' * 32, 'status': 'retired', 'admission': {'source_ref': 'ref'}}
            store = dict(receipts={old['receipt_id']: dict(before={'commit': '1' * 40}, after={'commit': '2' * 40})})
            with patch.object(learn_replay.c, 'read_invalidations', return_value={old['lesson_id']: old['id']}), \
                    patch.object(learn_replay.c, 'load_store', return_value=(root, store)), \
                    patch.object(learn_replay.c, 'latest_lessons', return_value={old['lesson_id']: latest}), \
                    patch.object(learn_replay.c, 'replay') as replay:
                learn_replay.run(root, ledger, projects=root, pairs_path=pairs)
            replay.assert_not_called()
            self.assertEqual(entry['replay'], dict(status='proven', repo=str(root), proof=old))

    def test_recorded_command_rejects_an_entirely_skipped_suite(self):
        replay_isolation.require_sandbox()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / 'repo'
            (repo / 'tests').mkdir(parents=True)
            def git(*args):
                return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()
            git('init', '-q')
            git('config', 'user.name', 'Fixture')
            git('config', 'user.email', 'fixture@example.invalid')
            (repo / 'tests/test_skip.py').write_text('import unittest\nclass T(unittest.TestCase):\n @unittest.skip("skip")\n def test_value(self): pass\n')
            git('add', '.')
            git('commit', '-qm', 'skipped')
            script, _, _ = regression_adapter.build_command(['python3', '-m', 'unittest', 'discover', '-s', 'tests'],
                                                               'FAILED (', git('rev-parse', 'HEAD:tests'))
            check = root / 'check.py'
            check.write_text(script)
            result = subprocess.run([sys.executable, str(check)], cwd=repo, capture_output=True)
            self.assertEqual(result.returncode, 2)
            (repo / 'tests/test_skip.py').write_text('import unittest\nclass T(unittest.TestCase):\n @unittest.expectedFailure\n def test_value(self): self.fail()\n')
            git('add', '.')
            git('commit', '-qm', 'expected failure')
            script, _, _ = regression_adapter.build_command(['python3', '-m', 'unittest', 'discover', '-s', 'tests'],
                                                               'FAILED (', git('rev-parse', 'HEAD:tests'))
            check.write_text(script)
            result = subprocess.run([sys.executable, str(check)], cwd=repo, capture_output=True)
            self.assertEqual(result.returncode, 2)

    def test_parallel_recorded_verify_follows_the_serial_skip_rule(self):
        replay_isolation.require_sandbox()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / 'repo'
            (repo / 'tests').mkdir(parents=True)
            (repo / '.fleet').mkdir()
            (repo / '.fleet/verify.sh').write_bytes(regression_adapter.PARALLEL_VERIFY)
            (repo / '.fleet/verify.sh').chmod(0o755)
            def git(*args):
                return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()
            git('init', '-q')
            git('config', 'user.name', 'Fixture')
            git('config', 'user.email', 'fixture@example.invalid')
            (repo / 'tests/test_pass.py').write_text('import unittest\nclass T(unittest.TestCase):\n def test_pass(self): pass\n')
            check = root / 'check.py'
            outcomes = []
            for body in ('@unittest.skip("skip")\n def test_value(self): pass\n', 'def test_value(self): pass\n'):
                (repo / 'tests/test_value.py').write_text('import unittest\nclass T(unittest.TestCase):\n ' + body)
                git('add', '.')
                git('commit', '-qm', 'tests')
                script, _, _ = regression_adapter.build_command(['./.fleet/verify.sh'], 'FAILED (', git('rev-parse', 'HEAD:tests'),
                                                                   entrypoint=regression_adapter.PARALLEL_VERIFY, runs=2)
                check.write_text(script)
                result = subprocess.run([sys.executable, str(check)], cwd=repo, capture_output=True, text=True)
                outcomes.append((result.returncode, result.stdout.strip()))
            self.assertEqual(outcomes, [(2, ''), (0, '{"status": "PASS"}')])

    def test_automatic_replay_cannot_read_or_write_outside_its_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / 'repo'
            repo.mkdir()
            def git(*args):
                return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()
            git('init', '-q')
            git('config', 'user.name', 'Fixture')
            git('config', 'user.email', 'fixture@example.invalid')
            (repo / 'value').write_text('x')
            git('add', '.')
            git('commit', '-qm', 'fixture')
            secret = root / 'private'
            secret.write_text('private')
            output = root / 'outside'
            check = root / 'check.py'
            check.write_text('import json, os\nfrom pathlib import Path\n'
                + 'assert "COLDFIX_ISOLATION_CANARY" not in os.environ\n'
                + f'assert not Path({str(secret)!r}).exists()\n'
                + f'assert not Path({str(output)!r}).parent.exists()\n'
                + 'print(json.dumps({"status":"PASS"}))\n')
            with patch.dict('os.environ', {'COLDFIX_ISOLATION_CANARY': 'private'}):
                result = learn_replay.isolated_replay_tree(repo, git('rev-parse', 'HEAD'), root / 'scratch',
                                                           'python3 ' + str(check), 'failure', 30)
            self.assertEqual(result['status'], 'PASS', result['stderr'])
            self.assertFalse(output.exists())

    def test_delivery_witness_cannot_access_host_environment_or_network(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket() as server:
            root = Path(directory)
            repo = root / 'repo'
            repo.mkdir()
            secret = root / 'private'
            secret.write_text('private')
            server.bind((LOOPBACK, 0))
            server.listen()
            port = server.getsockname()[1]
            (repo / 'witness.py').write_text('import json, os, socket\nfrom pathlib import Path\n'
                + 'assert "COLDFIX_ISOLATION_CANARY" not in os.environ\n'
                + f'assert not Path({str(secret)!r}).exists()\n'
                + f's=socket.socket(); s.settimeout(1); assert s.connect_ex(({LOOPBACK!r}, {port})) != 0\n'
                + 'print(json.dumps({"status":"PASS"}))\n')
            command = 'python3 witness.py'
            fact = dict(_command=command, _digest=c.digest(command + '\n'), _failure='failure')
            with patch.dict('os.environ', {'COLDFIX_ISOLATION_CANARY': 'private'}):
                result = coldrecall.run_witness(repo, fact, 'head', 30,
                                                             executor=replay_isolation.witness_process)
            self.assertEqual(result['result'], 'pass')

    def test_listed_public_repositories_from_other_owners_are_cloned_without_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'projects').mkdir()
            self.assertIn('public', inspect.signature(learn_replay.SourceProjects).parameters)
            sources = learn_replay.SourceProjects(root / 'projects', root, 'example', 30, public=['Other/Lib'])
            calls = []
            injected = dict(GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='http.extraHeader', GIT_CONFIG_VALUE_0='Authorization: x',
                            GIT_CONFIG_PARAMETERS="'credential.helper'='store'")
            with patch.dict(os.environ, injected), \
                    patch.object(learn_replay, 'clone_source', side_effect=lambda command, timeout, env: calls.append((command, env))):
                self.assertEqual(sources.get(dict(repo='other/lib', error='{}')), root / 'other/lib')
                sources.get(dict(repo='example/own', error='{}'))
                with self.assertRaisesRegex(c.ColdfixError, '^source repository owner is not configured$'):
                    sources.get(dict(repo='stranger/lib', error='{}'))
            (public, public_env), (own, own_env) = calls
            self.assertEqual(public[-2], 'https://github.com/other/lib.git')
            self.assertEqual((public_env['GIT_CONFIG_GLOBAL'], public_env['GIT_ASKPASS']), (os.devnull, '/bin/false'))
            self.assertEqual([key for key in injected if key in public_env], [])
            self.assertEqual(own[-2], 'https://github.com/example/own.git')
            self.assertEqual(own_env.get('GIT_CONFIG_GLOBAL'), os.environ.get('GIT_CONFIG_GLOBAL'))

    def local_clone_source(self, root):
        origin = root / 'origin.git'
        original = root / 'original'
        original.mkdir()
        subprocess.run(['git', 'init', '--bare', '-q', str(origin)], check=True)
        def git(*args):
            return subprocess.check_output(['git', '-C', str(original), *args], text=True).strip()
        git('init', '-q')
        git('config', 'user.name', 'Fixture')
        git('config', 'user.email', 'fixture@example.invalid')
        (original / 'tests').mkdir()
        (original / 'tests/test_value.py').write_text('import unittest\nclass T(unittest.TestCase):\n def test_value(self): pass\n')
        git('add', '.')
        git('commit', '-qm', 'base')
        git('remote', 'add', 'origin', str(origin))
        git('push', '-q', 'origin', 'HEAD:refs/heads/main')
        subprocess.run(['git', '--git-dir', str(origin), 'symbolic-ref', 'HEAD', 'refs/heads/main'], check=True)
        projects, workspace = root / 'projects', root / 'workspace'
        projects.mkdir()
        workspace.mkdir()
        def clone(command, timeout, env):
            self.assertEqual(env.get('GIT_TERMINAL_PROMPT'), '0')
            command = list(command)
            if command[1] == 'clone':
                command[-2] = str(origin)
                subprocess.run(command, check=True, env=env, capture_output=True, timeout=timeout)
                subprocess.run(['git', '-C', command[-1], 'remote', 'set-url', 'origin',
                                'https://github.com/example/project.git'], check=True)
                subprocess.run(['git', '-C', command[-1], 'config', f'url.{origin}.insteadOf',
                                'https://github.com/example/project.git'], check=True)
            else:
                subprocess.run(command, check=True, env=env, capture_output=True, timeout=timeout)
        return original, git, learn_replay.SourceProjects(projects, workspace, 'example', 30), clone

    def test_cached_clone_recovers_after_failed_fetch_for_another_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            original, git, projects, clone = self.local_clone_source(Path(directory))
            absent = dict(repo='example/project', error=json.dumps(dict(head_sha='a' * 40, log_tail='FAIL: test_value')))
            with patch.object(learn_replay, 'clone_source', side_effect=clone), \
                    patch.object(learn_replay.settle_codex_lessons, 'slug_of', return_value='example/project'):
                with self.assertRaisesRegex(c.ColdfixError, '^recorded commit not available on GitHub$'):
                    learn_replay.source_repo(absent, projects)
                (original / 'value.py').write_text('value = 1\n')
                git('add', '.')
                git('commit', '-qm', 'before')
                before = git('rev-parse', 'HEAD')
                (original / 'value.py').write_text('value = 2\n')
                git('commit', '-qam', 'after')
                after = git('rev-parse', 'HEAD')
                git('push', '-q', 'origin', 'HEAD:refs/heads/new')
                failure = dict(repo='example/project', error=json.dumps(dict(head_sha=before, log_tail='FAIL: test_value')))
                prepared = learn_replay.prepare(failure, dict(before=before, after=after), projects, 30)
                self.assertEqual(prepared[1:3], (before, after))
                self.assertEqual(prepared[-1], 'FAIL: test_value')

    def test_history_finds_a_fix_kept_only_by_a_merged_pull_request(self):
        with tempfile.TemporaryDirectory() as directory:
            original, git, projects, clone = self.local_clone_source(Path(directory))
            (original / 'value.py').write_text('value = 1\n')
            git('add', '.')
            git('commit', '-qm', 'failing pull request commit')
            failed = git('rev-parse', 'HEAD')
            (original / 'tests/test_value.py').write_text('import unittest\nclass T(unittest.TestCase):\n'
                                                          ' def test_value(self): self.assertTrue(True)\n')
            git('commit', '-qam', 'fix on the deleted pull request branch')
            fixed = git('rev-parse', 'HEAD')
            git('push', '-q', 'origin', 'HEAD:refs/pull/7/head')
            failure = dict(repo='example/project', error=json.dumps(dict(head_sha=failed, log_tail='FAIL: test_value')))
            with patch.object(learn_replay, 'clone_source', side_effect=clone), \
                    patch.object(learn_replay.settle_codex_lessons, 'slug_of', return_value='example/project'):
                try:
                    pairs = list(learn_replay.history_candidates(failure, projects, 30))
                except c.ColdfixError as error:
                    self.fail(str(error))
            self.assertEqual(pairs, [dict(repo='example/project', before=failed, after=fixed)])

    def test_a_cached_mirror_from_before_pull_heads_learns_them_on_its_next_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            original, git, projects, clone = self.local_clone_source(Path(directory))
            (original / 'value.py').write_text('value = 1\n')
            git('add', '.')
            git('commit', '-qm', 'failing pull request commit')
            failed = git('rev-parse', 'HEAD')
            git('push', '-q', 'origin', 'HEAD:refs/heads/main')
            failure = dict(repo='example/project', error=json.dumps(dict(head_sha=failed, log_tail='FAIL: test_value')))
            with patch.object(learn_replay, 'clone_source', side_effect=clone):
                mirror = projects.get(failure)
            subprocess.run(['git', '-C', str(mirror), 'config', '--replace-all', 'remote.origin.fetch',
                            '+refs/heads/*:refs/remotes/origin/*'], check=True)
            (original / 'tests/test_value.py').write_text('import unittest\nclass T(unittest.TestCase):\n'
                                                          ' def test_value(self): self.assertTrue(True)\n')
            git('commit', '-qam', 'fix on the deleted pull request branch')
            fixed = git('rev-parse', 'HEAD')
            git('push', '-q', 'origin', 'HEAD:refs/pull/7/head')
            with patch.object(learn_replay, 'clone_source', side_effect=clone), \
                    patch.object(learn_replay, 'REFRESHED', set()), \
                    patch.object(learn_replay.settle_codex_lessons, 'slug_of', return_value='example/project'):
                try:
                    pairs = list(learn_replay.history_candidates(failure, projects, 30))
                except c.ColdfixError as error:
                    self.fail(str(error))
            self.assertEqual(pairs, [dict(repo='example/project', before=failed, after=fixed)])

    def test_replay_normalizes_each_failure_once_however_often_it_is_grouped(self):
        record = dict(command='python3 -m unittest', error='FAIL: test_grouping_cost at 2026-11-07T00:00:00Z',
                      source='manual', id='9' * 32)
        with patch.object(c, 'normalized', wraps=c.normalized) as normalized:
            signatures = {c.signature(dict(record)) for _ in range(50)}
        self.assertEqual(len(signatures), 1)
        self.assertLessEqual(normalized.call_count, 2)

    def test_history_looks_past_a_local_checkout_that_lacks_the_pull_request_fix(self):
        with tempfile.TemporaryDirectory() as directory:
            original, git, projects, clone = self.local_clone_source(Path(directory))
            (original / 'value.py').write_text('value = 1\n')
            git('add', '.')
            git('commit', '-qm', 'failing pull request commit')
            failed = git('rev-parse', 'HEAD')
            subprocess.run(['git', 'clone', '-q', str(original), str(projects.projects / 'project')], check=True)
            (original / 'tests/test_value.py').write_text('import unittest\nclass T(unittest.TestCase):\n'
                                                          ' def test_value(self): self.assertTrue(True)\n')
            git('commit', '-qam', 'fix on the deleted pull request branch')
            fixed = git('rev-parse', 'HEAD')
            git('push', '-q', 'origin', 'HEAD:refs/pull/7/head')
            failure = dict(repo='example/project', error=json.dumps(dict(head_sha=failed, log_tail='FAIL: test_value')))
            with patch.object(learn_replay, 'clone_source', side_effect=clone), \
                    patch.object(learn_replay.settle_codex_lessons, 'slug_of', return_value='example/project'):
                try:
                    pairs = list(learn_replay.history_candidates(failure, projects, 30))
                except c.ColdfixError as error:
                    self.fail(str(error))
            self.assertEqual(pairs, [dict(repo='example/project', before=failed, after=fixed)])

    def test_unavailable_recorded_commit_has_specific_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            _, _, projects, clone = self.local_clone_source(Path(directory))
            failure = dict(repo='example/project', error=json.dumps(dict(head_sha='b' * 40)))
            with patch.object(learn_replay, 'clone_source', side_effect=clone):
                with self.assertRaisesRegex(c.ColdfixError, '^recorded commit not available on GitHub$'):
                    learn_replay.source_repo(failure, projects)

    def test_unavailable_pair_commit_has_specific_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            _, git, projects, clone = self.local_clone_source(Path(directory))
            before = git('rev-parse', 'HEAD')
            failure = dict(repo='example/project', error=json.dumps(dict(head_sha=before, log_tail='FAIL: test_value')))
            with patch.object(learn_replay, 'clone_source', side_effect=clone), \
                    patch.object(learn_replay.settle_codex_lessons, 'slug_of', return_value='example/project'):
                with self.assertRaisesRegex(c.ColdfixError, '^recorded commit not available on GitHub$'):
                    learn_replay.prepare(failure, dict(before=before, after='c' * 40), projects, 30)

    def test_commit_gone_from_github_is_fetched_from_a_replay_peer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original, git, projects, clone = self.local_clone_source(root)
            (original / 'value.py').write_text('value = 1\n')
            git('add', '.')
            git('commit', '-qm', 'force-pushed away')
            before = git('rev-parse', 'HEAD')
            git('reset', '-q', '--hard', 'HEAD~1')
            git('reflog', 'expire', '--expire=now', '--all')
            peer = root / 'peer'
            peer.mkdir()
            original.rename(peer / 'project')
            failure = dict(repo='example/project', error=json.dumps(dict(head_sha=before, log_tail='FAIL: test_value')))
            with patch.dict(learn_replay.os.environ, COLDFIX_REPLAY_PEERS=str(peer)), \
                    patch.object(learn_replay, 'clone_source', side_effect=clone), \
                    patch.object(learn_replay.settle_codex_lessons, 'slug_of', return_value='example/project'):
                try:
                    repo = learn_replay.source_repo(failure, learn_replay.SourceProjects(
                        projects.projects, projects.workspace, 'example', 30))
                except c.ColdfixError as error:
                    self.fail(str(error))
            self.assertEqual(subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'refs/coldfix/' + before],
                                                     text=True).strip(), before)

    def test_repository_gone_from_github_is_mirrored_from_a_replay_peer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original, git, projects, clone = self.local_clone_source(root)
            before = git('rev-parse', 'HEAD')
            peer = root / 'peer'
            peer.mkdir()
            original.rename(peer / 'project')
            def github_gone(command, timeout, env):
                if not any(str(peer) in part for part in command):
                    raise subprocess.CalledProcessError(128, command)
                clone(command, timeout, env)
            failure = dict(repo='example/project', error=json.dumps(dict(head_sha=before, log_tail='FAIL: test_value')))
            with patch.dict(learn_replay.os.environ, COLDFIX_REPLAY_PEERS=str(peer)), \
                    patch.object(learn_replay, 'clone_source', side_effect=github_gone):
                try:
                    repo = learn_replay.source_repo(failure, learn_replay.SourceProjects(
                        projects.projects, projects.workspace, 'example', 30))
                except (c.ColdfixError, subprocess.CalledProcessError) as error:
                    self.fail(str(error))
            self.assertEqual(subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'refs/coldfix/' + before],
                                                     text=True).strip(), before)

    def test_replay_cache_keeps_source_clones_across_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, git, _, clone = self.local_clone_source(root)
            head = git('rev-parse', 'HEAD')
            failure = dict(repo='example/project', error=json.dumps(dict(head_sha=head, log_tail='FAIL: test_value')))
            store = root / 'store'
            store.mkdir()
            clones, repos = [], []
            def counted(command, timeout, env):
                if command[1] == 'clone':
                    clones.append(command[-1])
                return clone(command, timeout, env)
            def replay(root, ledger, sources, pairs_path, timeout):
                repos.append(sources.get(failure))
                return {}
            with patch.dict(learn_replay.os.environ, COLDFIX_REPLAY_CACHE=str(root / 'cache'), COLDFIX_OWNER='example'), \
                    patch.object(learn_replay, 'clone_source', side_effect=counted), \
                    patch.object(learn_replay, 'run_in_workspace', side_effect=replay):
                for _ in range(2):
                    learn_replay.run(store, dict(drafts={}, runs=[]))
            self.assertEqual(len(clones), 1)
            self.assertEqual(repos, [root / 'cache/repos/example/project'] * 2)
            self.assertTrue(repos[0].is_dir())

    def test_cached_mirror_is_refreshed_each_run_and_pins_replayed_commits(self):
        with tempfile.TemporaryDirectory() as directory:
            original, git, sources, clone = self.local_clone_source(Path(directory))
            head = git('rev-parse', 'HEAD')
            failure = dict(repo='example/project', error=json.dumps(dict(head_sha=head, log_tail='FAIL: test_value')))
            with patch.object(learn_replay, 'clone_source', side_effect=clone):
                mirror = sources.get(failure)
                git('commit', '-q', '--allow-empty', '-m', 'later fix')
                git('push', '-q', 'origin', 'HEAD:refs/heads/main')
                with patch.object(learn_replay, 'REFRESHED', set(), create=True):
                    self.assertEqual(learn_replay.SourceProjects(sources.projects, sources.workspace, 'example', 30)
                                     .get(failure), mirror)
            refs = subprocess.check_output(['git', '-C', str(mirror), 'for-each-ref', '--format=%(refname) %(objectname)'],
                                           text=True).splitlines()
            self.assertIn('refs/remotes/origin/main ' + git('rev-parse', 'HEAD'), refs)
            self.assertIn('refs/coldfix/' + head + ' ' + head, refs)

    def test_cached_replay_tools_are_visible_to_the_isolated_check(self):
        with tempfile.TemporaryDirectory() as directory:
            tools = Path(directory) / 'tools'
            (tools / 'bin').mkdir(parents=True)
            with patch.dict(learn_replay.os.environ, COLDFIX_REPLAY_CACHE=directory):
                argv = replay_isolation.sandbox(Path(directory), ['true'])
                script = ci_fix_learning.check_script(dict(command=['python3', '-m', 'unittest'], kind='test'))
            self.assertIn(['--ro-bind', str(tools), '/opt/coldfix-replay'],
                          [argv[index:index + 3] for index in range(len(argv) - 2)])
            self.assertIn("'PATH': '/opt/coldfix-replay/bin:", script)

    def test_invalidated_saved_proof_is_replayed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = dict(id='a' * 32, lesson_id='b' * 32, receipt_id='c' * 32, status='PASS', verification='proven')
            new = dict(id='d' * 32, lesson_id=old['lesson_id'], receipt_id='e' * 32, status='PASS', verification='proven')
            entry = dict(verdict='PASS', evidence=['failure'], replay=dict(status='proven', repo=str(root), proof=old))
            ledger = dict(drafts={'candidate': entry}, runs=[])
            (root / 'learn-drafts.json').write_text(json.dumps(ledger))
            pairs = root / 'pairs.json'
            pairs.write_text(json.dumps(dict(results=[])))
            latest = {'id': 'f' * 32, 'status': 'proposed', 'admission': {'source_ref': 'ref'}, 'check_script': 'placeholder'}
            store = dict(lessons={old['id']: {'id': old['id'], 'admission': {'source_ref': 'ref'}}},
                         failures={'other': {'repo': 'example/other', 'source_ref': 'other'},
                                   'failure': {'repo': 'example/project', 'source_ref': 'ref'}}, receipts={old['receipt_id']:
                dict(before={'commit': '1' * 40}, after={'commit': '2' * 40})})
            with patch.object(learn_replay.c, 'read_invalidations', return_value={old['lesson_id']: old['id']}), \
                    patch.object(learn_replay.c, 'load_store', return_value=(root, store)), \
                    patch.object(learn_replay.c, 'latest_lessons', return_value={old['lesson_id']: latest}), \
                    patch.object(learn_replay.regression_adapter, 'payload', return_value={}), \
                    patch.object(learn_replay, 'source_repo', return_value=root) as source, \
                    patch.object(learn_replay.c, 'replay', return_value=new) as replay:
                learn_replay.run(root, ledger, projects=root, pairs_path=pairs)
            self.assertEqual(replay.call_args.args[0].before, '1' * 40)
            self.assertEqual(replay.call_args.args[0].lesson, latest['id'])
            self.assertEqual(source.call_args.args[0]['repo'], 'example/project')
            self.assertEqual(entry['replay']['proof'], new)

    def test_recorded_check_rejects_symlinked_tests_and_changed_verify_script(self):
        replay_isolation.require_sandbox()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / 'repo'
            (repo / 'actual_tests').mkdir(parents=True)
            (repo / '.fleet').mkdir()
            (repo / 'actual_tests/test_value.py').write_text('import unittest\nclass T(unittest.TestCase):\n def test_value(self): pass\n')
            (repo / 'tests').symlink_to('actual_tests')
            verify = repo / '.fleet/verify.sh'
            verify.write_text('#!/bin/sh\npython3 -m unittest discover -s tests\n')
            verify.chmod(0o755)
            def git(*args):
                return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()
            git('init', '-q')
            git('config', 'user.name', 'Fixture')
            git('config', 'user.email', 'fixture@example.invalid')
            git('add', '.')
            git('commit', '-qm', 'symlinked tests')
            script, _, _ = regression_adapter.build_command(['python3', '-m', 'unittest', 'discover', '-s', 'tests'],
                                                               'FAILED (', git('rev-parse', 'HEAD:tests'))
            check = root / 'check.py'
            check.write_text(script)
            self.assertEqual(subprocess.run([sys.executable, str(check)], cwd=repo, capture_output=True).returncode, 2)
            (repo / 'tests').unlink()
            (repo / 'tests').mkdir()
            (repo / 'tests/test_value.py').write_text('import unittest\nclass T(unittest.TestCase):\n def test_value(self): pass\n')
            git('add', '-A')
            git('commit', '-qm', 'real tests')
            script, _, _ = regression_adapter.build_command(['./.fleet/verify.sh'], 'FAILED (', git('rev-parse', 'HEAD:tests'))
            check.write_text(script)
            self.assertEqual(subprocess.run([sys.executable, str(check)], cwd=repo, capture_output=True).returncode, 2)

    def test_recorded_command_does_not_pass_when_no_tests_run(self):
        replay_isolation.require_sandbox()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / 'repo'
            (repo / 'tests').mkdir(parents=True)
            def git(*args):
                return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()
            git('init', '-q')
            git('config', 'user.name', 'Fixture')
            git('config', 'user.email', 'fixture@example.invalid')
            (repo / 'tests/test_empty.py').write_text('print("Ran 1 test in 0.001s")\n')
            git('add', '.')
            git('commit', '-qm', 'no tests')
            script, _, _ = regression_adapter.build_command(['python3', '-m', 'unittest', 'discover', '-s', 'tests'],
                                                               'FAILED (', git('rev-parse', 'HEAD:tests'))
            check = root / 'check.py'
            check.write_text(script)
            result = subprocess.run([sys.executable, str(check)], cwd=repo, capture_output=True)
            self.assertEqual(result.returncode, 2)

    def test_recorded_check_preserves_checkout_and_rejects_missing_tests(self):
        replay_isolation.require_sandbox()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / 'repo'
            (repo / 'tests').mkdir(parents=True)
            def git(*args):
                return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()
            git('init', '-q')
            git('config', 'user.name', 'Fixture')
            git('config', 'user.email', 'fixture@example.invalid')
            (repo / 'victim').write_text('original')
            test = repo / 'tests/test_value.py'
            test.write_text('import unittest\nfrom pathlib import Path\nclass T(unittest.TestCase):\n def test_run(self): Path("victim").write_text("changed")\n')
            git('add', '.')
            git('commit', '-qm', 'test')
            tree = git('rev-parse', 'HEAD:tests')
            script, _, _ = regression_adapter.build_command(['python3', '-m', 'unittest', 'discover', '-s', 'tests'], 'FAILED (', tree)
            check = root / 'check.py'
            check.write_text(script)
            result = subprocess.run([sys.executable, str(check)], cwd=repo, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((repo / 'victim').read_text(), 'original')
            test.unlink()
            result = subprocess.run([sys.executable, str(check)], cwd=repo, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)

    def test_missing_source_uses_a_throwaway_full_clone(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / 'original'
            original.mkdir()
            def git(*args):
                return subprocess.check_output(['git', '-C', str(original), *args], text=True).strip()
            git('init', '-q')
            git('config', 'user.name', 'Fixture')
            git('config', 'user.email', 'fixture@example.invalid')
            (original / 'value').write_text('one')
            git('add', 'value')
            git('commit', '-qm', 'one')
            before = git('rev-parse', 'HEAD')
            (original / 'value').write_text('two')
            git('commit', '-qam', 'two')
            failure = dict(repo='example/project', error=json.dumps(dict(head_sha=before)))
            projects = root / 'projects'
            projects.mkdir()
            workspace = root / 'workspace'
            workspace.mkdir()
            def clone(command, timeout, env):
                command = list(command)
                command[-2] = str(original)
                subprocess.run(command, check=True, env=env)
                subprocess.run(['git', '-C', command[-1], 'remote', 'set-url', 'origin',
                                'https://github.com/example/project.git'], check=True)
            with patch.object(learn_replay, 'clone_source', side_effect=clone):
                source = learn_replay.SourceProjects(projects, workspace, 'example', 30)
                repo = learn_replay.source_repo(failure, source)
                self.assertEqual(subprocess.check_output(['git', '-C', str(repo), 'rev-parse', '--is-shallow-repository'], text=True).strip(), 'false')
                self.assertEqual(subprocess.check_output(['git', '-C', str(repo), 'cat-file', '-t', before], text=True).strip(), 'commit')
                self.assertFalse((projects / 'project').exists())

    def test_rejected_draft_is_replayed_at_most_once_per_day(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            today = datetime.now(timezone.utc).date().isoformat()
            ledger = dict(runs=[], drafts={
                name: dict(verdict='PASS', evidence=[name], replay=dict(status='rejected', reason='no pair'),
                           replay_attempts=[dict(day=day, failure=None, before=None, after=None, reason='no pair')])
                for name, day in (('today', today), ('yesterday', '2000-01-01'))})
            (root / 'learn-drafts.json').write_text(json.dumps(ledger))
            pairs = root / 'pairs.json'
            pairs.write_text(json.dumps(dict(results=[])))
            with patch.object(learn_replay.c, 'load_store', return_value=(root, {})), \
                    patch.object(learn_replay, 'prior_proof', return_value=None), \
                    patch.object(learn_replay, 'replay_candidates', return_value=[]) as replayed:
                for _ in range(2):
                    learn_replay.run(root, ledger, projects=root, pairs_path=pairs)
            self.assertEqual([call.args[1]['evidence'] for call in replayed.call_args_list], [['yesterday']])
            self.assertEqual(len(ledger['drafts']['today']['replay_attempts']), 1)
            self.assertEqual([row['day'] for row in ledger['drafts']['yesterday']['replay_attempts']], ['2000-01-01', today])

    def test_rejection_after_utc_midnight_counts_for_the_new_day(self):
        class Clock:
            moments = iter([datetime(2000, 1, 1, 23, 59, 59, tzinfo=timezone.utc)])
            later = datetime(2000, 1, 2, 0, 0, 1, tzinfo=timezone.utc)

            @classmethod
            def now(cls, tz=None):
                return next(cls.moments, cls.later)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = dict(runs=[], drafts={'draft': dict(verdict='PASS', evidence=['draft'],
                replay=dict(status='rejected', reason='no pair'),
                replay_attempts=[dict(day='1999-12-31', failure=None, before=None, after=None, reason='no pair')])})
            (root / 'learn-drafts.json').write_text(json.dumps(ledger))
            pairs = root / 'pairs.json'
            pairs.write_text(json.dumps(dict(results=[])))
            with patch.object(learn_replay, 'datetime', Clock), \
                    patch.object(learn_replay.c, 'load_store', return_value=(root, {})), \
                    patch.object(learn_replay, 'prior_proof', return_value=None), \
                    patch.object(learn_replay, 'replay_candidates', return_value=[]) as replayed:
                for _ in range(2):
                    learn_replay.run(root, ledger, projects=root, pairs_path=pairs)
            self.assertEqual(replayed.call_count, 1)
            self.assertEqual([row['day'] for row in ledger['drafts']['draft']['replay_attempts']], ['1999-12-31', '2000-01-02'])

    def settle_replays(self, root, ledger, day, prepare, revision='first', rules=1, prior=None, expired=None,
                       store=None, results=(), candidates=None, admit=None, load=None, report=None):
        pairs = root / 'pairs.json'
        pairs.write_text(json.dumps(dict(results=list(results))) if report is None else report)
        failure = dict(id='f' * 32, kind='ci')
        with patch.object(learn_replay, 'utc_day', return_value=day), \
                patch.object(learn_replay, 'code_revision', return_value=revision), \
                patch.object(learn_replay, 'REPLAY_RULES', rules, create=True), \
                patch.object(learn_replay.c, 'load_store', side_effect=load, return_value=(root, store or {})), \
                patch.object(learn_replay, 'prior_proof', side_effect=prior, return_value=None), \
                patch.object(learn_replay, 'expired', side_effect=expired or (lambda deadline: False)), \
                patch.object(learn_replay, 'prepare', side_effect=prepare), \
                patch.object(learn_replay, 'admission_args', side_effect=lambda *args: args[4:6]), \
                patch.object(learn_replay.incident_admission, 'admit', side_effect=admit), \
                patch.object(learn_replay, 'replay_candidates', side_effect=candidates,
                             return_value=[(failure, dict(before='e' * 40, after='d' * 40))]) as replayed:
            learn_replay.run(root, ledger, projects=root, pairs_path=pairs)
        return replayed.call_count

    def settle_ledger(self, root):
        ledger = dict(runs=[], drafts={'draft': dict(verdict='PASS', evidence=['draft'])})
        (root / 'learn-drafts.json').write_text(json.dumps(ledger))
        return ledger

    def test_terminal_rejection_waits_for_new_replay_rules_or_a_week_not_new_code(self):
        prepared = []

        def prepare(*args, **kwargs):
            prepared.append(1)
            raise c.ColdfixError('no changed unittest matches the recorded failure')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = self.settle_ledger(root)
            self.assertEqual([self.settle_replays(root, ledger, day, prepare, revision=day)
                              for day in ('2026-11-01', '2026-11-02', '2026-11-03')], [1, 0, 0])
            self.assertEqual([self.settle_replays(root, ledger, day, prepare, rules=2)
                              for day in ('2026-11-03', '2026-11-04')], [1, 0])
            self.assertEqual([self.settle_replays(root, ledger, day, prepare, rules=2)
                              for day in ('2026-11-10', '2026-11-11')], [1, 0])
            self.assertEqual(len(prepared), 3)
            self.assertEqual(ledger['drafts']['draft']['replay']['status'], 'rejected')

    def test_the_same_withheld_rejection_on_four_days_settles_for_a_week(self):
        withheld = 'incident not admitted; flagged lines reverted INCOMPLETE; expected FAIL; cause withheld by the privacy screen'

        def prepare(*args, **kwargs):
            raise c.ColdfixError(withheld)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = self.settle_ledger(root)
            entry = ledger['drafts']['draft']
            calls = []
            for day in ('2026-11-01', '2026-11-02', '2026-11-03', '2026-11-04', '2026-11-05', '2026-11-10'):
                calls.append(self.settle_replays(root, ledger, day, prepare))
                if day == '2026-11-03':
                    self.assertFalse(entry['replay_settled']['confirmed'])
            self.assertEqual(calls, [1, 1, 1, 1, 0, 0])
            self.assertTrue(entry['replay_settled']['confirmed'])
            self.assertEqual(entry['replay'], dict(status='rejected', reason=withheld))
            self.assertEqual(self.settle_replays(root, ledger, '2026-11-11', prepare), 1)

    def test_a_repeated_infrastructure_rejection_stays_unsettled(self):
        for reason in ('replay sandbox unavailable',
                       'incident not admitted; flagged lines reverted INCOMPLETE; expected FAIL; Check timed out',
                       'incident not admitted; flagged lines reverted INCOMPLETE; expected FAIL'):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory:
                def prepare(*args, **kwargs):
                    raise c.ColdfixError(reason)
                root = Path(directory)
                ledger = self.settle_ledger(root)
                self.assertEqual([self.settle_replays(root, ledger, day, prepare)
                                  for day in ('2026-11-01', '2026-11-02', '2026-11-03', '2026-11-04', '2026-11-05')], [1, 1, 1, 1, 1])
                self.assertFalse(ledger['drafts']['draft']['replay_settled']['confirmed'])

    def test_a_changed_flagged_range_does_not_settle_on_its_first_attempt(self):
        withheld = 'incident not admitted; flagged lines reverted INCOMPLETE; expected FAIL; cause withheld by the privacy screen'

        def prepare(*args, **kwargs):
            raise c.ColdfixError(withheld)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = self.settle_ledger(root)
            for day in ('2026-11-01', '2026-11-02', '2026-11-03'):
                self.settle_replays(root, ledger, day, prepare)
            ledger['drafts']['draft']['replay_settled']['inputs'] = 'an older flagged range'
            self.assertEqual(self.settle_replays(root, ledger, '2026-11-04', prepare), 1)
            self.assertFalse(ledger['drafts']['draft']['replay_settled']['confirmed'])

    def test_a_repeated_rejection_from_a_new_pair_restarts_the_count(self):
        withheld = 'incident not admitted; flagged lines reverted INCOMPLETE; expected FAIL; cause withheld by the privacy screen'
        failure = dict(id='f' * 32, kind='ci')
        afters = iter(['d' * 40] * 3 + ['c' * 40] * 4)

        def candidates(*args, **kwargs):
            return [(failure, dict(before='e' * 40, after=next(afters)))]

        def prepare(*args, **kwargs):
            raise c.ColdfixError(withheld)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = self.settle_ledger(root)
            days = ('2026-11-01', '2026-11-02', '2026-11-03', '2026-11-04', '2026-11-05', '2026-11-06', '2026-11-07', '2026-11-08')
            self.assertEqual([self.settle_replays(root, ledger, day, prepare, candidates=candidates) for day in days],
                             [1, 1, 1, 1, 1, 1, 1, 0])

    def test_a_rejection_that_changes_between_days_stays_unsettled(self):
        reasons = iter(['incident not admitted; flagged lines reverted INCOMPLETE; expected FAIL; NameError: name ' + repr(name)
                        + ' is not defined' for name in ('a', 'b', 'a', 'b', 'a')])

        def prepare(*args, **kwargs):
            raise c.ColdfixError(next(reasons))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = self.settle_ledger(root)
            self.assertEqual([self.settle_replays(root, ledger, day, prepare)
                              for day in ('2026-11-01', '2026-11-02', '2026-11-03', '2026-11-04', '2026-11-05')], [1, 1, 1, 1, 1])
            self.assertFalse(ledger['drafts']['draft']['replay_settled']['confirmed'])

    def test_a_narrow_rules_bump_reopens_only_the_rejections_it_names(self):
        missing = ("incident not admitted; flagged lines reverted INCOMPLETE; expected FAIL; "
                   "ModuleNotFoundError: No module named 'vault_kit'")
        days = ('2026-11-01', '2026-11-02', '2026-11-03', '2026-11-04')
        env_bump = getattr(learn_replay, 'REOPENS', {}).get(4, ())
        for reason, settle_days, reopened_by_narrow_bump in (
                ('no changed unittest matches the recorded failure', days[:1], 0), (missing, days, 1),
                ('incident not admitted; red FAIL, green FAIL, reversed-patch FAIL; expected FAIL, PASS, FAIL', days, 0),
                ('incident not admitted; red PASS, green PASS, reversed-patch PASS; expected FAIL, PASS, FAIL', days, 0),
                ('incident not admitted; flagged lines reverted PASS; expected FAIL', days, 0),
                ('Coldpass replay input: wrong_failure', days[:1], 1),
                ('generated test: ' + learn_replay.UNUSABLE_REASK, days[:1], 1)):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory, \
                    patch.object(learn_replay, 'REOPENS', {2: env_bump}, create=True):
                def prepare(*args, **kwargs):
                    raise c.ColdfixError(reason)
                root = Path(directory)
                ledger = self.settle_ledger(root)
                for day in settle_days:
                    self.settle_replays(root, ledger, day, prepare)
                settled = dict(ledger['drafts']['draft']['replay_settled'])
                self.assertTrue(settled['confirmed'])
                self.assertEqual(self.settle_replays(root, ledger, '2026-11-05', prepare, rules=2), reopened_by_narrow_bump)
                ledger['drafts']['draft']['replay_settled'] = settled
                self.assertEqual(self.settle_replays(root, ledger, '2026-11-06', prepare, rules=3), 1)

    def test_a_narrow_rules_bump_reopens_a_rejection_whose_proposed_fix_hit_a_missing_module(self):
        def prepare(*args, **kwargs):
            raise c.ColdfixError('no changed unittest matches the recorded failure')
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(learn_replay, 'REOPENS', {2: getattr(learn_replay, 'REOPENS', {}).get(4, ())}, create=True):
            root = Path(directory)
            ledger = self.settle_ledger(root)
            self.settle_replays(root, ledger, '2026-11-01', prepare)
            entry = ledger['drafts']['draft']
            settled = dict(entry['replay_settled'])
            self.assertTrue(settled['confirmed'])
            self.assertEqual(self.settle_replays(root, ledger, '2026-11-02', prepare, rules=2), 0)
            entry['replay_settled'] = settled
            entry['proposed_fix'] = dict(status='rejected', reason=(
                "incident not admitted; flagged lines reverted INCOMPLETE; expected FAIL; "
                "ModuleNotFoundError: No module named 'vault_kit'"))
            self.assertEqual(self.settle_replays(root, ledger, '2026-11-03', prepare, rules=2), 1)

    def test_a_narrow_rules_bump_reopens_a_fallback_pass_only_when_the_env_can_supply_its_module(self):
        env_bump = learn_replay.REOPENS[4]
        for module, outside, declared, reopened in (('vault_kit', '', set(), 1), ('httpx', ' outside this repo', {'httpx'}, 1),
                                                    ('readtrial', ' outside this repo', {'httpx'}, 0),
                                                    ('yaml.constructor', ' outside this repo', {'yaml'}, 1),
                                                    ('readtrial.cli', ' outside this repo', {'httpx'}, 0),
                                                    ('readtrial', ' outside this repo', None, 1)):
            reason = ("incident not admitted; flagged lines reverted PASS; expected FAIL; "
                      f"ModuleNotFoundError: No module named '{module}'{outside}")
            with self.subTest(module=module, declared=declared), tempfile.TemporaryDirectory() as directory, \
                    patch.object(learn_replay, 'REOPENS', {2: env_bump}), \
                    patch.object(learn_replay, 'declared_modules', create=True,
                                 side_effect=c.ColdfixError('no checkout') if declared is None else None,
                                 return_value=declared) as looked_up:
                def prepare(*args, **kwargs):
                    raise c.ColdfixError(reason)
                root = Path(directory)
                ledger = self.settle_ledger(root)
                self.settle_replays(root, ledger, '2026-11-01', prepare)
                self.assertTrue(ledger['drafts']['draft']['replay_settled']['confirmed'])
                self.assertEqual(self.settle_replays(root, ledger, '2026-11-02', prepare, rules=2), reopened)
                if outside:
                    self.assertEqual([call.args[1]['replay_attempts'][0]['before'] for call in looked_up.call_args_list], ['e' * 40])

    def test_declared_modules_cover_every_replayed_revision(self):
        entry = dict(replay_attempts=[dict(failure='f' * 32, before='a' * 40), dict(failure=None, before=None),
                                      dict(failure='f' * 32, before='b' * 40, after='c' * 40),
                                      dict(failure='f' * 32, before='b' * 40, after='c' * 40)])
        store = dict(failures={'f' * 32: dict(id='f' * 32)})
        found = {'a' * 40: {'httpx'}, 'b' * 40: {'yaml'}, 'c' * 40: {'openpyxl'}}
        with patch.object(learn_replay, 'source_repo', return_value=Path('repo')), \
                patch.object(learn_replay.replay_env, 'supplied', create=True,
                             side_effect=lambda cache, repo, revision, *rest: found[revision]) as supplied:
            self.assertEqual(learn_replay.declared_modules(store, entry, Path('projects')), {'httpx', 'yaml', 'openpyxl'})
            self.assertEqual(sorted(call.args[2] for call in supplied.call_args_list), ['a' * 40, 'b' * 40, 'c' * 40])
            supplied.reset_mock()
            learn_replay.declared_modules(store, entry, Path('projects'), 123.0)
        self.assertEqual({call.args[3:] for call in supplied.call_args_list}, {(123.0,)})
        with self.assertRaises(c.ColdfixError):
            learn_replay.declared_modules(store, dict(replay_attempts=[]), Path('projects'))

    def test_retries_whose_missing_module_the_env_can_supply_run_first(self):
        def rejected(reason):
            return dict(verdict='PASS', replay=dict(status='rejected', reason=reason),
                        replay_attempts=[dict(day='2026-10-31', failure='f' * 32, before='e' * 40, after='d' * 40, reason=reason)])
        missing = "incident not admitted; flagged lines reverted INCOMPLETE; expected FAIL; ModuleNotFoundError: No module named '{}'"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = dict(runs=[], drafts={name: dict(rejected(reason), evidence=[name]) for name, reason in (
                ('plain', 'no changed unittest matches the recorded failure'),
                ('unresolved', missing.format('httpx') + ' outside this repo'),
                ('cross', missing.format('readtrial') + ' outside this repo'),
                ('declared', missing.format('httpx') + ' outside this repo'),
                ('intree', missing.format('vault_kit')))})
            (root / 'learn-drafts.json').write_text(json.dumps(ledger))
            pairs = root / 'pairs.json'
            pairs.write_text(json.dumps(dict(results=[])))
            failure = dict(id='f' * 32, kind='ci')
            prepared = []

            def prepare(failure, pair, *args, **kwargs):
                prepared.append(pair['name'])
                raise c.ColdfixError('no changed unittest matches the recorded failure')

            def declared(store, entry, *args):
                if entry['evidence'] == ['unresolved']:
                    raise c.ColdfixError('no checkout')
                return {'httpx'}
            with patch.object(learn_replay, 'utc_day', return_value='2026-11-01'), \
                    patch.object(learn_replay, 'expired', side_effect=lambda deadline: len(prepared) >= 2), \
                    patch.object(learn_replay.c, 'load_store', return_value=(root, {})), \
                    patch.object(learn_replay, 'prior_proof', return_value=None), \
                    patch.object(learn_replay, 'declared_modules', create=True, side_effect=declared), \
                    patch.object(learn_replay, 'prepare', side_effect=prepare), \
                    patch.object(learn_replay, 'replay_candidates', side_effect=lambda store, entry, *args: [
                        (failure, dict(before='e' * 40, after='d' * 40, name=entry['evidence'][0]))]):
                learn_replay.run(root, ledger, projects=root, pairs_path=pairs)
            self.assertEqual(sorted(prepared), ['declared', 'intree'])

    def test_flagged_lines_that_arrive_the_same_day_reopen_a_review_rejection(self):
        url = 'https://github.com/datarobot-oss/datarobot-genai/pull/772#discussion_r4605753840'
        failure = dict(id='draft', kind='review', repo='datarobot-oss/datarobot-genai', source_ref=url, error='Finding')
        pair = dict(repo='datarobot-oss/datarobot-genai', before='a' * 40, after='b' * 40, finding_url=url)
        flagged = dict(pair, path='src/datarobot_genai/core/auth/workload_identity/source.py', start_line=96, end_line=96)

        def prepare(*args, **kwargs):
            raise c.ColdfixError('a review finding is proven only with its flagged lines')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = self.settle_ledger(root)
            self.assertEqual([self.settle_replays(root, ledger, '2026-11-19', prepare, store=dict(failures=dict(draft=failure)),
                                                  results=[row]) for row in (pair, pair, flagged, flagged)], [1, 0, 1, 0])

    def test_a_new_fix_pair_reopens_a_settled_rejection(self):
        failure = dict(id='draft', kind='ci', repo='example-owner/project', error=json.dumps(dict(head_sha='a' * 40)))
        store = dict(failures=dict(draft=failure))
        pair = dict(repo='example-owner/project', before='a' * 40, after='b' * 40)

        def prepare(*args, **kwargs):
            raise c.ColdfixError('no changed unittest matches the recorded failure')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = self.settle_ledger(root)
            self.assertEqual([self.settle_replays(root, ledger, day, prepare, store=store)
                              for day in ('2026-11-01', '2026-11-02')], [1, 0])
            self.assertEqual([self.settle_replays(root, ledger, day, prepare, store=store, results=[pair])
                              for day in ('2026-11-03', '2026-11-04')], [1, 0])

    def test_a_failed_history_lookup_keeps_a_terminal_rejection_unsettled(self):
        failure = dict(id='f' * 32, kind='ci')

        def candidates(store, entry, pairs, projects, timeout, history_reasons, history=True):
            yield failure, dict(before='e' * 40, after='d' * 40)
            history_reasons.append(failure['id'] + ': git fetch timed out')

        def prepare(*args, **kwargs):
            raise c.ColdfixError('no changed unittest matches the recorded failure')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = self.settle_ledger(root)
            self.assertEqual([self.settle_replays(root, ledger, day, prepare, candidates=candidates)
                              for day in ('2026-11-01', '2026-11-02', '2026-11-03')], [1, 1, 1])
            self.assertFalse(ledger['drafts']['draft']['replay_settled']['confirmed'])

    def history_settles(self, history_error, with_pair, expired=None, report=None):
        failure = dict(id='f' * 32, kind='ci', repo='example-owner/project', error=json.dumps(dict(head_sha='a' * 40)))
        store = dict(failures={'f' * 32: failure})
        real = learn_replay.replay_candidates
        pair = dict(repo='example-owner/project', before='a' * 40, after='b' * 40)

        def prepare(*args, **kwargs):
            raise c.ColdfixError('no changed unittest matches the recorded failure')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = dict(runs=[], drafts={'draft': dict(verdict='PASS', evidence=['f' * 32])})
            (root / 'learn-drafts.json').write_text(json.dumps(ledger))
            with patch.object(learn_replay, 'history_candidates', side_effect=history_error):
                calls = [self.settle_replays(root, ledger, day, prepare, store=store, candidates=real,
                                             results=[pair] if with_pair else [], expired=expired, report=report)
                         for day in ('2026-11-01', '2026-11-02', '2026-11-03')]
            return calls, ledger['drafts']['draft']

    def test_terminal_history_exhaustion_settles_with_a_rejected_pair(self):
        calls, entry = self.history_settles(
            c.ColdfixError('no descendant contains a unittest matching the recorded failure'), True)
        self.assertEqual(calls, [1, 0, 0])
        self.assertTrue(entry['replay_settled']['confirmed'])

    def test_terminal_history_exhaustion_settles_without_any_pair(self):
        calls, entry = self.history_settles(
            c.ColdfixError('no descendant contains a unittest matching the recorded failure'), False)
        self.assertEqual(calls, [1, 0, 0])
        self.assertTrue(entry['replay_settled']['confirmed'])

    def test_multiple_terminal_history_failures_settle_without_any_pair(self):
        refs = ['a' * 32, 'b' * 32]
        store = dict(failures={ref: dict(id=ref, kind='ci', repo='example/project',
                                         error=json.dumps(dict(head_sha='c' * 40))) for ref in refs})
        failure = c.ColdfixError('no descendant contains a unittest matching the recorded failure')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = self.settle_ledger(root)
            entry = ledger['drafts']['draft']
            entry['evidence'] = refs
            with patch.object(learn_replay, 'history_candidates', side_effect=failure):
                calls = [self.settle_replays(root, ledger, day, None, store=store,
                                             candidates=learn_replay.replay_candidates)
                         for day in ('2026-11-01', '2026-11-02', '2026-11-03')]
            self.assertEqual(calls, [1, 0, 0])
            self.assertTrue(entry['replay_settled']['confirmed'])

    def test_an_unreadable_fix_pair_report_never_settles(self):
        pair = dict(repo='example-owner/project', before='a' * 40, after='b' * 40)
        for history in (c.ColdfixError('no descendant contains a unittest matching the recorded failure'),
                        lambda *args: [pair]):
            calls, entry = self.history_settles(history, False, report='{')
            self.assertEqual(calls, [1, 1, 1])
            self.assertFalse(entry['replay_settled']['confirmed'])

    def test_a_settled_draft_is_replayed_while_the_fix_pair_report_is_unreadable(self):
        def prepare(*args, **kwargs):
            raise c.ColdfixError('no changed unittest matches the recorded failure')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = self.settle_ledger(root)
            self.settle_replays(root, ledger, '2026-11-01', prepare)
            self.assertTrue(ledger['drafts']['draft']['replay_settled']['confirmed'])
            self.assertEqual(self.settle_replays(root, ledger, '2026-11-02', prepare, report='{'), 1)

    def test_a_timed_out_history_lookup_never_settles(self):
        calls, entry = self.history_settles(subprocess.TimeoutExpired(['git', 'log'], 1), True)
        self.assertEqual(calls, [1, 1, 1])
        self.assertFalse(entry['replay_settled']['confirmed'])

    def test_a_deadline_passed_during_history_never_settles(self):
        for with_pair in (True, False):
            late = []

            def history(*args):
                late.append(1)
                raise c.ColdfixError('no descendant contains a unittest matching the recorded failure')
            _, entry = self.history_settles(history, with_pair, expired=lambda deadline: bool(late))
            self.assertFalse(entry['replay_settled']['confirmed'])

    def test_a_resume_after_terminal_history_exhaustion_settles(self):
        failure = dict(id='f' * 32, kind='ci')
        late = []
        timing_out = [1]

        def candidates(store, entry, pairs, projects, timeout, history_reasons, history=True):
            history_reasons.append(failure['id'] + ': no descendant contains a unittest matching the recorded failure')
            yield failure, dict(before='e' * 40, after='d' * 40)

        def prepare(*args, **kwargs):
            if timing_out:
                late.append(1)
                raise subprocess.TimeoutExpired('replay', 1)
            raise c.ColdfixError('no changed unittest matches the recorded failure')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = self.settle_ledger(root)
            self.settle_replays(root, ledger, '2026-11-01', prepare, candidates=candidates,
                                expired=lambda deadline: bool(late))
            self.assertIn('replay_resume', ledger['drafts']['draft'])
            timing_out.clear()
            self.settle_replays(root, ledger, '2026-11-01', prepare, candidates=candidates)
            self.assertTrue(ledger['drafts']['draft']['replay_settled']['confirmed'])

    def test_an_infrastructure_history_error_with_a_terminal_message_never_settles(self):
        calls, entry = self.history_settles(OSError('no descendant contains a unittest matching the recorded failure'), True)
        self.assertEqual(calls, [1, 1, 1])
        self.assertFalse(entry['replay_settled']['confirmed'])

    def test_a_store_error_while_checking_a_settled_draft_does_not_escape(self):
        def prepare(*args, **kwargs):
            raise c.ColdfixError('no changed unittest matches the recorded failure')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = self.settle_ledger(root)
            self.settle_replays(root, ledger, '2026-11-01', prepare)
            self.assertTrue(ledger['drafts']['draft']['replay_settled']['confirmed'])
            with patch.object(learn_replay, 'pair_inputs', side_effect=OSError('store unreadable')):
                self.settle_replays(root, ledger, '2026-11-02', prepare)
            self.assertEqual(ledger['drafts']['draft']['replay_settled']['day'], '2026-11-02')
            self.assertFalse(ledger['drafts']['draft']['replay_settled']['confirmed'])

    def test_a_store_error_while_settling_a_rejection_does_not_escape(self):
        rejected = []

        def prior(*args):
            rejected.append(1)
            raise c.ColdfixError('no changed unittest matches the recorded failure')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = self.settle_ledger(root)

            def load(path):
                if rejected:
                    raise OSError('store unreadable')
                return root, {}
            self.settle_replays(root, ledger, '2026-11-01', None, prior=prior, load=load)
            self.assertEqual(ledger['drafts']['draft']['replay']['status'], 'rejected')
            self.assertFalse(ledger['drafts']['draft']['replay_settled']['confirmed'])

    def test_a_cached_pair_with_several_terminal_reasons_settles(self):
        outcomes = ['no changed unittest matches the recorded failure', "the fix does not change the finding's flagged lines"]

        def prepare(*args, **kwargs):
            raise AssertionError('a cached terminal pair is not replayed')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = self.settle_ledger(root)
            ledger['drafts']['draft']['replay_terminal'] = {' '.join(('f' * 32, 'e' * 40, 'd' * 40)): dict(
                day='2026-11-01', reason='; '.join(outcomes), reasons=outcomes, generated_route=True, rules=1)}
            self.settle_replays(root, ledger, '2026-11-01', prepare)
            self.assertTrue(ledger['drafts']['draft']['replay_settled']['confirmed'])

    def test_an_unversioned_terminal_pair_record_is_replayed_again(self):
        prepared = []

        def prepare(*args, **kwargs):
            prepared.append(1)
            raise c.ColdfixError('no changed unittest matches the recorded failure')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = self.settle_ledger(root)
            ledger['drafts']['draft']['replay_terminal'] = {' '.join(('f' * 32, 'e' * 40, 'd' * 40)): dict(
                day='2026-11-01', reason='no changed unittest matches the recorded failure', generated_route=True)}
            self.settle_replays(root, ledger, '2026-11-01', prepare)
            self.assertEqual(len(prepared), 1)

    def test_an_infrastructure_error_after_a_skipped_test_never_settles(self):
        def prepare(failure, pair, projects, timeout, skip=()):
            if skip:
                raise OSError('no changed unittest matches the recorded failure')
            return Path('.'), pair['before'], pair['after'], 'tests/test_x.py', 'FAIL: test_x'

        def admit(args, root, store):
            raise c.ColdfixError('local command passed')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = self.settle_ledger(root)
            self.assertEqual([self.settle_replays(root, ledger, day, prepare, admit=admit)
                              for day in ('2026-11-01', '2026-11-02')], [1, 1])
            self.assertFalse(ledger['drafts']['draft']['replay_settled']['confirmed'])
            self.assertNotIn('replay_terminal', ledger['drafts']['draft'])

    def test_an_infrastructure_error_before_a_deadline_is_not_cached_after_the_resume(self):
        late = []

        def prepare(failure, pair, projects, timeout, skip=()):
            if skip:
                raise c.ColdfixError('no changed unittest matches the recorded failure')
            return Path('.'), pair['before'], pair['after'], 'tests/test_x.py', 'FAIL: test_x'

        def admit(args, root, store):
            late.append(1)
            raise OSError('local command passed')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = self.settle_ledger(root)
            self.settle_replays(root, ledger, '2026-11-01', prepare, admit=admit, expired=lambda deadline: bool(late))
            self.assertIn('replay_resume', ledger['drafts']['draft'])
            late.clear()
            self.settle_replays(root, ledger, '2026-11-01', prepare, admit=admit)
            self.assertNotIn('replay_terminal', ledger['drafts']['draft'])
            self.assertFalse(ledger['drafts']['draft']['replay_settled']['confirmed'])

    def test_a_spent_deadline_skips_the_settled_input_check(self):
        def prepare(*args, **kwargs):
            raise c.ColdfixError('no changed unittest matches the recorded failure')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = self.settle_ledger(root)
            self.settle_replays(root, ledger, '2026-11-01', prepare)
            with patch.object(learn_replay, 'pair_inputs') as inputs:
                self.assertEqual(self.settle_replays(root, ledger, '2026-11-02', prepare,
                                                     expired=lambda deadline: True), 0)
            inputs.assert_not_called()

    def test_an_infrastructure_error_inside_a_pair_never_settles_or_caches_the_pair(self):
        prepared = []

        def prepare(*args, **kwargs):
            prepared.append(1)
            raise OSError('no changed unittest matches the recorded failure')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = self.settle_ledger(root)
            for day in ('2026-11-01', '2026-11-02', '2026-11-03'):
                self.settle_replays(root, ledger, day, prepare)
            self.assertEqual(len(prepared), 3)
            self.assertFalse(ledger['drafts']['draft']['replay_settled']['confirmed'])
            self.assertNotIn('replay_terminal', ledger['drafts']['draft'])

    def test_flagged_lines_rules_are_terminal_rejections(self):
        for reason in ("the fix does not change the finding's flagged lines",
                       "the fix's edit of the finding's flagged lines also adds or changes unflagged lines"):
            self.assertTrue(learn_replay.terminal([reason]))

    def test_a_test_importing_a_module_outside_the_repo_is_a_terminal_rejection(self):
        flagged = 'incident not admitted; flagged lines reverted INCOMPLETE; expected FAIL; ModuleNotFoundError: '
        for module in ('agent_sandbox', 'sampletool', 'coldfix_missing_module'):
            outside = flagged + f"No module named '{module}' outside this repo"
            for reason in (outside, 'generated test: ' + outside):
                with self.subTest(reason=reason):
                    self.assertTrue(learn_replay.terminal([reason]))
                    [bucket] = learn_replay.reason_buckets([reason])
                    self.assertEqual(bucket, 'not_provable')
        for reason in (flagged + "No module named 'agent_sandbox'",
                       flagged + "No module named 'agent_sandbox' outside this repo; and more",
                       'incident not admitted; flagged lines reverted INCOMPLETE; expected FAIL',
                       'incident not admitted; red INCOMPLETE, green INCOMPLETE, reversed-patch INCOMPLETE; '
                       "expected FAIL, PASS, FAIL; red, green, reversed: ModuleNotFoundError: No module named 'agent_sandbox'"):
            with self.subTest(reason=reason):
                self.assertFalse(learn_replay.terminal([reason]))

    def test_unfixable_incomplete_causes_are_terminal_on_every_incomplete_side(self):
        admitted = 'incident not admitted; red {}, green {}, reversed-patch {}; expected FAIL, PASS, FAIL'
        flagged = 'incident not admitted; flagged lines reverted INCOMPLETE; expected FAIL; '
        outside = "ModuleNotFoundError: No module named 'sampletool' outside this repo"
        for reason in (admitted.format('INCOMPLETE', 'INCOMPLETE', 'INCOMPLETE') + '; red, green, reversed: ' + outside,
                       admitted.format('INCOMPLETE', 'INCOMPLETE', 'INCOMPLETE') + '; red, green, reversed: NO TESTS RAN',
                       admitted.format('INCOMPLETE', 'PASS', 'INCOMPLETE') + '; red, reversed: test patch does not apply',
                       admitted.format('INCOMPLETE', 'INCOMPLETE', 'INCOMPLETE') + '; red, reversed: NO TESTS RAN; green: ' + outside,
                       flagged + 'NO TESTS RAN', flagged + 'test patch does not apply'):
            for prefixed in (reason, 'generated test: ' + reason):
                with self.subTest(reason=prefixed):
                    self.assertTrue(learn_replay.terminal([prefixed]))
                    self.assertEqual(list(learn_replay.reason_buckets([prefixed])), ['not_provable'])
        for reason in (admitted.format('INCOMPLETE', 'INCOMPLETE', 'INCOMPLETE') + '; red, reversed: NO TESTS RAN',
                       admitted.format('INCOMPLETE', 'INCOMPLETE', 'INCOMPLETE') + '; red, reversed: NO TESTS RAN; green: KeyError: x',
                       admitted.format('INCOMPLETE', 'PASS', 'INCOMPLETE') + "; red, reversed: ModuleNotFoundError: No module named 'vault_kit'",
                       admitted.format('INCOMPLETE', 'PASS', 'INCOMPLETE') + '; red, reversed: cause withheld by the privacy screen',
                       admitted.format('INCOMPLETE', 'PASS', 'INCOMPLETE') + '; red, reversed: NO TESTS RAN and more',
                       admitted.format('INCOMPLETE', 'PASS', 'INCOMPLETE'),
                       flagged + 'NO TESTS RAN; and more', flagged + 'Check timed out'):
            with self.subTest(reason=reason):
                self.assertFalse(learn_replay.terminal([reason]))
                self.assertEqual(list(learn_replay.reason_buckets([reason])), ['ours'])

    def test_incomplete_rejections_are_retried_each_day(self):
        def prepare(*args, **kwargs):
            raise c.ColdfixError('incident not admitted; red INCOMPLETE, green INCOMPLETE, reversed-patch INCOMPLETE; '
                                 'expected FAIL, PASS, FAIL')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = self.settle_ledger(root)
            self.assertEqual([self.settle_replays(root, ledger, day, prepare)
                              for day in ('2026-11-01', '2026-11-01', '2026-11-02', '2026-11-03')], [1, 0, 1, 1])
            self.assertFalse(ledger['drafts']['draft']['replay_settled']['confirmed'])

    def week_of_days_replays(self, root, ledger, days, afters, blocked, **kwargs):
        failure = dict(id='f' * 32, kind='ci')
        proof = dict(id='a' * 32, lesson_id='b' * 32, receipt_id='c' * 32, status='PASS', verification='proven')
        admitted = []

        def prepare(failure, pair, projects, timeout, skip=()):
            if skip:
                raise c.ColdfixError('no changed unittest matches the recorded failure')
            return Path('.'), pair['before'], pair['after'], 'tests/test_x.py', 'FAIL: test_x'

        def admit(args, root, store):
            admitted.append((day, args[1][0]))
            if args[1][0] == '9':
                return proof
            raise blocked if args[1][0] == '3' else c.ColdfixError(
                'incident not admitted; red FAIL, green FAIL, reversed-patch FAIL; expected FAIL, PASS, FAIL')
        for day in days:
            self.settle_replays(root, ledger, f'2026-11-{day:02}', prepare, revision=f'deployed on {day}', admit=admit,
                                candidates=lambda *args: iter([(failure, dict(before='e' * 40, after=digit * 40))
                                                               for digit in afters]), **kwargs)
        return [day for day, digit in admitted if digit == afters[-1]]

    def test_a_pair_rejected_on_a_week_of_days_is_replayed_weekly_until_the_rules_change(self):
        withheld = c.ColdfixError('incident not admitted; red INCOMPLETE, green INCOMPLETE, reversed-patch INCOMPLETE; '
                                  'expected FAIL, PASS, FAIL; red, green, reversed: cause withheld by the privacy screen')
        for afters, blocked in (('13', withheld), ('3', OSError('replay sandbox unavailable'))):
            with self.subTest(afters=afters, blocked=blocked), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                ledger = self.settle_ledger(root)
                entry = ledger['drafts']['draft']
                self.assertEqual(self.week_of_days_replays(root, ledger, range(1, 22), afters, blocked), [*range(1, 8), 14, 21])
                self.assertEqual(entry['replay']['status'], 'rejected')
                self.assertIn(str(blocked), entry['replay']['reason'])
                self.assertFalse(entry['replay_settled']['confirmed'])
                self.assertEqual(self.week_of_days_replays(root, ledger, (22, 23), afters, blocked, rules=2), [22])

    def test_a_new_pair_is_proven_while_a_pair_rejected_on_a_week_of_days_waits(self):
        missing = c.ColdfixError('incident not admitted; red FAIL, green FAIL, reversed-patch INCOMPLETE; '
                                 "expected FAIL, PASS, FAIL; reversed: ModuleNotFoundError: No module named 'record'")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = self.settle_ledger(root)
            self.assertEqual(self.week_of_days_replays(root, ledger, range(1, 9), '13', missing), [*range(1, 8)])
            self.assertEqual(self.week_of_days_replays(root, ledger, (9,), '139', missing), [9])
            self.assertEqual(ledger['drafts']['draft']['replay']['status'], 'proven')
            self.assertEqual([row['after'][0] for row in ledger['drafts']['draft']['replay_attempts']].count('3'), 7)

    def test_a_privacy_screened_rejection_settles_until_its_inputs_change(self):
        failure = dict(id='draft', kind='ci', repo='example-owner/project', error=json.dumps(dict(head_sha='a' * 40)))
        store = dict(failures=dict(draft=failure))
        pair = dict(repo='example-owner/project', before='a' * 40, after='b' * 40)

        def prepare(*args, **kwargs):
            raise c.ColdfixError('incident cannot be exported; privacy screen blocked: {"credential": 1, "hostname": 3}')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = self.settle_ledger(root)
            self.assertEqual([self.settle_replays(root, ledger, day, prepare, store=store)
                              for day in ('2026-11-01', '2026-11-02')], [1, 0])
            self.assertTrue(ledger['drafts']['draft']['replay_settled']['confirmed'])
            self.assertEqual([self.settle_replays(root, ledger, day, prepare, store=store, results=[pair],
                                                  candidates=lambda *args: [(failure, pair)])
                              for day in ('2026-11-03', '2026-11-04')], [1, 0])
            self.assertEqual([a['day'] for a in ledger['drafts']['draft']['replay_attempts']], ['2026-11-01', '2026-11-03'])
            self.assertTrue(ledger['drafts']['draft']['replay_settled']['confirmed'])

    def deterministic_rejection_settles(self, reason, prior=None):
        def prepare(*args, **kwargs):
            raise c.ColdfixError(reason)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = self.settle_ledger(root)
            calls = [self.settle_replays(root, ledger, day, prepare, prior=prior)
                     for day in ('2026-11-01', '2026-11-02', '2026-11-03')]
            entry = ledger['drafts']['draft']
            self.assertEqual(entry['replay'], dict(status='rejected', reason=reason))
            self.assertTrue(entry['replay_settled']['confirmed'])
            self.assertEqual(len({a['day'] for a in entry['replay_attempts']}), 1)
            return calls

    def test_a_review_finding_without_flagged_lines_settles(self):
        self.assertEqual(self.deterministic_rejection_settles('a review finding is proven only with its flagged lines'),
                         [1, 0, 0])

    def test_a_fix_that_leaves_the_requested_paths_unchanged_settles(self):
        self.assertEqual(self.deterministic_rejection_settles(
            'no change between the replay revisions for the requested paths'), [1, 0, 0])

    def test_a_pytest_test_or_its_inputs_changed_between_revisions_settles(self):
        for reason in ('pytest test changed between revisions', 'pytest test inputs changed between revisions'):
            with self.subTest(reason=reason):
                self.assertEqual(self.deterministic_rejection_settles(reason), [1, 0, 0])

    def test_an_invalidated_review_lesson_settles(self):
        def prior(*args):
            raise c.ColdfixError('an invalidated review lesson is re-proven only under the flagged-lines rule')
        self.assertEqual(self.deterministic_rejection_settles(
            'an invalidated review lesson is re-proven only under the flagged-lines rule', prior=prior), [0, 0, 0])

    def test_an_infrastructure_error_never_settles_a_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = self.settle_ledger(root)
            attempts = []

            def prior(*args):
                attempts.append(1)
                raise OSError('no changed unittest matches the recorded failure')
            for day in ('2026-11-01', '2026-11-01', '2026-11-02', '2026-11-03'):
                self.settle_replays(root, ledger, day, None, prior=prior)
            self.assertEqual(len(attempts), 3)
            self.assertEqual(ledger['drafts']['draft']['replay']['status'], 'rejected')
            self.assertFalse(ledger['drafts']['draft']['replay_settled']['confirmed'])

    def test_a_deadline_or_timeout_is_deferred_and_never_settled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = self.settle_ledger(root)
            late = []

            def prepare(*args, **kwargs):
                late.append(1)
                raise subprocess.TimeoutExpired('replay', 1)
            for day in ('2026-11-01', '2026-11-02'):
                late.clear()
                self.settle_replays(root, ledger, day, prepare, expired=lambda deadline: bool(late))
                entry = ledger['drafts']['draft']
                self.assertNotIn('replay_settled', entry)
                self.assertNotIn('replay', entry)
                self.assertIn('replay_resume', entry)
            self.assertEqual(late, [1])

    def test_never_attempted_drafts_replay_before_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            retry = dict(verdict='PASS', evidence=['retry'], replay=dict(status='rejected', reason='no pair'),
                         replay_attempts=[dict(day='2026-10-31', failure=None, before=None, after=None, reason='no pair')])
            ledger = dict(runs=[], replay_cursor='first', drafts={
                'first': dict(json.loads(json.dumps(retry)), evidence=['first']), 'retry': retry,
                'new': dict(verdict='PASS', evidence=['new'])})
            (root / 'learn-drafts.json').write_text(json.dumps(ledger))
            pairs = root / 'pairs.json'
            pairs.write_text(json.dumps(dict(results=[])))
            with patch.object(learn_replay, 'utc_day', return_value='2026-11-01'), \
                    patch.object(learn_replay.c, 'load_store', return_value=(root, {})), \
                    patch.object(learn_replay, 'prior_proof', return_value=None), \
                    patch.object(learn_replay, 'replay_candidates', return_value=[]) as replayed:
                learn_replay.run(root, ledger, projects=root, pairs_path=pairs)
            self.assertEqual([call.args[1]['evidence'] for call in replayed.call_args_list],
                             [['new'], ['retry'], ['first']])

    def test_earlier_day_retries_share_the_deadline_with_a_growing_new_draft_queue(self):
        def rejected(day):
            return dict(verdict='PASS', replay=dict(status='rejected', reason='no pair'),
                        replay_attempts=[dict(day=day, failure=None, before=None, after=None, reason='no pair')])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = dict(runs=[], drafts={name: dict(rejected(day), evidence=[name]) for name, day in (
                ('today', '2026-11-01'), ('old1', '2026-10-30'), ('old2', '2026-10-31'), ('old3', '2026-10-31'))})
            pairs = root / 'pairs.json'
            pairs.write_text(json.dumps(dict(results=[])))
            failure = dict(id='f' * 32, kind='ci')

            def prepare(failure, pair, *args, **kwargs):
                prepared.append(pair['name'])
                raise c.ColdfixError('no changed unittest matches the recorded failure')
            runs = []
            for collect in range(4):
                if collect < 3:
                    for index in range(3):
                        ledger['drafts'][f'new{collect}{index}'] = dict(verdict='PASS', evidence=[f'new{collect}{index}'])
                (root / 'learn-drafts.json').write_text(json.dumps(ledger))
                prepared = []
                limit = 2 if collect < 3 else 99
                with patch.object(learn_replay, 'utc_day', return_value='2026-11-01'), \
                        patch.object(learn_replay, 'expired', side_effect=lambda deadline: len(prepared) >= limit), \
                        patch.object(learn_replay.c, 'load_store', return_value=(root, {})), \
                        patch.object(learn_replay, 'prior_proof', return_value=None), \
                        patch.object(learn_replay, 'prepare', side_effect=prepare), \
                        patch.object(learn_replay, 'replay_candidates', side_effect=lambda store, entry, *args: [
                            (failure, dict(before='e' * 40, after='d' * 40, name=entry['evidence'][0]))]):
                    learn_replay.run(root, ledger, projects=root, pairs_path=pairs)
                runs.append(prepared)
            self.assertEqual(runs[:3], [['new00', 'old1'], ['new01', 'old2'], ['new02', 'old3']])
            self.assertEqual(sorted(runs[3]), ['new10', 'new11', 'new12', 'new20', 'new21', 'new22'])
            self.assertEqual({entry['replay_attempts'][-1]['day'] for entry in ledger['drafts'].values()}, {'2026-11-01'})

    def test_retries_cut_off_by_the_deadline_rotate_through_every_earlier_day_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = dict(runs=[], replay_cursor='old1', drafts={name: dict(
                verdict='PASS', evidence=[name], replay=dict(status='rejected', reason='no pair'),
                replay_attempts=[dict(day=day, failure=None, before=None, after=None, reason='no pair')])
                for name, day in (('old1', '2026-10-30'), ('old2', '2026-10-30'), ('old3', '2026-10-31'))})
            pairs = root / 'pairs.json'
            pairs.write_text(json.dumps(dict(results=[])))
            failure = dict(id='f' * 32, kind='ci')

            def prepare(failure, pair, *args, **kwargs):
                prepared.append(pair['name'])
                raise subprocess.TimeoutExpired('replay', 1)
            runs = []
            for _ in range(3):
                (root / 'learn-drafts.json').write_text(json.dumps(ledger))
                prepared = []
                with patch.object(learn_replay, 'utc_day', return_value='2026-11-01'), \
                        patch.object(learn_replay, 'expired', side_effect=lambda deadline: bool(prepared)), \
                        patch.object(learn_replay.c, 'load_store', return_value=(root, {})), \
                        patch.object(learn_replay, 'prior_proof', return_value=None), \
                        patch.object(learn_replay, 'prepare', side_effect=prepare), \
                        patch.object(learn_replay, 'replay_candidates', side_effect=lambda store, entry, *args: [
                            (failure, dict(before='e' * 40, after='d' * 40, name=entry['evidence'][0]))]):
                    learn_replay.run(root, ledger, projects=root, pairs_path=pairs)
                runs.append(prepared)
            self.assertEqual(runs, [['old2'], ['old3'], ['old1']])
            self.assertTrue(all('replay_resume' in entry for entry in ledger['drafts'].values()))

    def test_due_retries_come_before_proven_retired_and_tried_today_drafts(self):
        def rejected(day, reason='no pair'):
            return dict(verdict='PASS', replay=dict(status='rejected', reason=reason),
                        replay_attempts=[dict(day=day, failure=None, before=None, after=None, reason=reason)])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proof = dict(lesson_id='l', id='p', receipt_id='r')
            ledger = dict(runs=[], drafts=dict(
                proven=dict(verdict='PASS', replay=dict(status='proven', proof=proof, repo=str(root))),
                retired=rejected('2026-10-30', 'no exact pair: DUPLICATE_FINDING'),
                today=rejected('2026-11-01'),
                due=rejected('2026-10-31')))
            for name, entry in ledger['drafts'].items():
                entry['evidence'] = [name]
            (root / 'learn-drafts.json').write_text(json.dumps(ledger))
            pairs = root / 'pairs.json'
            pairs.write_text(json.dumps(dict(results=[])))
            failure = dict(id='f' * 32, kind='ci')
            prepared = []

            def prepare(failure, pair, *args, **kwargs):
                prepared.append(pair['name'])
                raise c.ColdfixError('no changed unittest matches the recorded failure')
            with patch.object(learn_replay, 'utc_day', return_value='2026-11-01'), \
                    patch.object(learn_replay, 'expired', side_effect=lambda deadline: bool(prepared)), \
                    patch.object(learn_replay.c, 'load_store', return_value=(root, {})), \
                    patch.object(learn_replay, 'prior_proof', return_value=None), \
                    patch.object(learn_replay, 'refresh_proof', return_value=(root, proof)) as refreshed, \
                    patch.object(learn_replay, 'revalidate'), \
                    patch.object(learn_replay, 'prepare', side_effect=prepare), \
                    patch.object(learn_replay, 'replay_candidates', side_effect=lambda store, entry, *args: [
                        (failure, dict(before='e' * 40, after='d' * 40, name=entry['evidence'][0]))]):
                learn_replay.run(root, ledger, projects=root, pairs_path=pairs)
            self.assertEqual(prepared, ['due'])
            self.assertEqual(refreshed.call_count, 1)

    def test_replay_rules_change_with_the_terminal_sets(self):
        snapshot = {1: '610de3b1', 2: '0ebec27d', 3: '0ebec27d', 4: '0ebec27d'}
        digest = hashlib.sha256(json.dumps([sorted(learn_replay.TERMINAL), sorted(learn_replay.NOT_PROVABLE)]).encode())
        self.assertEqual(digest.hexdigest()[:8], snapshot.get(learn_replay.REPLAY_RULES),
                         'TERMINAL or NOT_PROVABLE changed: bump learn_replay.REPLAY_RULES and add its snapshot here')

    def test_pairs_failing_on_both_sides_are_not_replayed_again_but_a_new_provable_pair_is(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = dict(runs=[], drafts={'draft': dict(verdict='PASS', evidence=['draft'])})
            (root / 'learn-drafts.json').write_text(json.dumps(ledger))
            pairs = root / 'pairs.json'
            pairs.write_text(json.dumps(dict(results=[])))
            failure = dict(id='f' * 32, kind='ci')
            outcome = {'1': 'red FAIL, green FAIL, reversed-patch FAIL', '2': 'red PASS, green PASS, reversed-patch PASS',
                       '3': 'red INCOMPLETE, green INCOMPLETE, reversed-patch INCOMPLETE'}
            proof = dict(id='a' * 32, lesson_id='b' * 32, receipt_id='c' * 32, status='PASS', verification='proven')
            admitted = []

            def prepare(failure, pair, projects, timeout, skip=()):
                if skip:
                    raise c.ColdfixError('no changed unittest matches the recorded failure')
                return root, pair['before'], pair['after'], 'tests/test_x.py', 'FAIL: test_x'

            def admit(args, root, store):
                admitted.append(args[1][0])
                if args[1][0] == '9':
                    return proof
                raise c.ColdfixError('incident not admitted; ' + outcome[args[1][0]] + '; expected FAIL, PASS, FAIL')

            def replays(day, afters):
                admitted.clear()
                candidates = [(failure, dict(before='e' * 40, after=digit * 40)) for digit in afters]
                with patch.object(learn_replay, 'utc_day', return_value=day), \
                        patch.object(learn_replay, 'code_revision', return_value='deployed on ' + day), \
                        patch.object(learn_replay.c, 'load_store', return_value=(root, {})), \
                        patch.object(learn_replay, 'prior_proof', return_value=None), \
                        patch.object(learn_replay, 'replay_candidates', side_effect=lambda *args: iter(candidates)), \
                        patch.object(learn_replay, 'prepare', side_effect=prepare), \
                        patch.object(learn_replay, 'admission_args', side_effect=lambda *args: args[4:6]), \
                        patch.object(learn_replay.incident_admission, 'admit', side_effect=admit):
                    learn_replay.run(root, ledger, projects=root, pairs_path=pairs)
                entry = json.loads((root / 'learn-drafts.json').read_text())['drafts']['draft']
                return list(admitted), entry['replay']['status'], len(entry['replay_attempts'])

            self.assertEqual(replays('2026-11-08', '123'), (['1', '2', '3'], 'rejected', 3))
            self.assertEqual(replays('2026-11-08', '123'), ([], 'rejected', 3))
            self.assertEqual(replays('2026-11-09', '123'), (['3'], 'rejected', 4))
            self.assertIn('red FAIL, green FAIL', ledger['drafts']['draft']['replay']['reason'])
            self.assertIn('red PASS, green PASS', ledger['drafts']['draft']['replay']['reason'])
            self.assertEqual(replays('2026-11-10', '1239'), (['3', '9'], 'proven', 5))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = dict(runs=[], drafts={'draft': dict(verdict='PASS', evidence=['draft'])})
            (root / 'learn-drafts.json').write_text(json.dumps(ledger))
            pairs = root / 'pairs.json'
            pairs.write_text(json.dumps(dict(results=[])))
            self.assertEqual(replays('2026-11-08', '12'), (['1', '2'], 'rejected', 2))
            self.assertEqual(replays('2026-11-14', '12'), ([], 'rejected', 2))
            self.assertEqual(replays('2026-11-15', '12'), (['1', '2'], 'rejected', 4))

    def test_review_pair_whose_generated_test_passes_on_both_sides_is_not_replayed_again(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = dict(runs=[], drafts={'draft': dict(verdict='PASS', evidence=['draft'])})
            (root / 'learn-drafts.json').write_text(json.dumps(ledger))
            pairs = root / 'pairs.json'
            pairs.write_text(json.dumps(dict(results=[])))
            candidate = (dict(id='f' * 32, kind='review'), dict(before='a' * 40, after='b' * 40))
            started = []

            def prepare(failure, pair, projects, timeout, skip=()):
                started.append('prepare')
                raise c.ColdfixError('no changed unittest matches the recorded failure')

            def generate(*args):
                started.append('generated')
                raise c.ColdfixError('incident not admitted; red PASS, green PASS, reversed-patch PASS; '
                                     'expected FAIL, PASS, FAIL')

            runs = []
            for day in ('2026-11-08', '2026-11-09'):
                started.clear()
                with patch.object(learn_replay, 'utc_day', return_value=day), \
                        patch.object(learn_replay, 'code_revision', return_value='deployed on ' + day), \
                        patch.object(learn_replay.c, 'load_store', return_value=(root, {})), \
                        patch.object(learn_replay, 'prior_proof', return_value=None), \
                        patch.object(learn_replay, 'replay_candidates', side_effect=lambda *args: iter([candidate])), \
                        patch.object(learn_replay, 'prepare', side_effect=prepare), \
                        patch.object(learn_replay, 'generated_proof', side_effect=generate):
                    learn_replay.run(root, ledger, projects=root, pairs_path=pairs)
                runs.append((list(started), ledger['drafts']['draft']['replay']['status']))
            self.assertEqual(runs, [(['prepare', 'generated'], 'rejected'), ([], 'rejected')])
            self.assertIn('generated test: incident not admitted; red PASS', ledger['drafts']['draft']['replay']['reason'])

    def test_replay_budget_defers_the_rest_of_a_draft_and_resumes_where_it_stopped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = dict(runs=[], drafts={'draft': dict(verdict='PASS', evidence=['draft'])})
            (root / 'learn-drafts.json').write_text(json.dumps(ledger))
            pairs = root / 'pairs.json'
            pairs.write_text(json.dumps(dict(results=[])))
            failure = dict(id='f' * 32, kind='ci')
            candidates = [(failure, dict(before=digit * 40, after='e' * 40)) for digit in '135']
            clock, tried, runs = [0.0], [], []

            def slow_rejection(failure, pair, projects, timeout, skip=()):
                tried.append(pair['before'][0])
                clock[0] += 100
                raise c.ColdfixError('reason ' + pair['before'][0])

            for _ in range(3):
                tried.clear()
                with patch.object(learn_replay, 'REPLAY_SECONDS', 50, create=True), \
                        patch('time.monotonic', side_effect=lambda: clock[0]), \
                        patch.object(learn_replay, 'utc_day', return_value='2026-11-08'), \
                        patch.object(learn_replay.c, 'load_store', return_value=(root, {})), \
                        patch.object(learn_replay, 'prior_proof', return_value=None), \
                        patch.object(learn_replay, 'replay_candidates', side_effect=lambda *args: iter(candidates)), \
                        patch.object(learn_replay, 'prepare', side_effect=slow_rejection):
                    learn_replay.run(root, ledger, projects=root, pairs_path=pairs)
                runs.append((list(tried), ledger['drafts']['draft'].get('replay', {}).get('status')))
            self.assertEqual(runs, [(['1'], None), (['3'], None), (['5'], 'rejected')])
            self.assertEqual(ledger['drafts']['draft']['replay']['reason'], 'reason 1; reason 3; reason 5')
            self.assertEqual(json.loads((root / 'learn-drafts.json').read_text())['drafts']['draft']['replay']['status'],
                             'rejected')

    def test_a_draft_deferred_by_the_deadline_does_not_load_the_store(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = dict(runs=[], drafts={name: dict(verdict='PASS', evidence=[name]) for name in ('first', 'second')})
            (root / 'learn-drafts.json').write_text(json.dumps(ledger))
            pairs = root / 'pairs.json'
            pairs.write_text(json.dumps(dict(results=[])))
            candidate = (dict(id='f' * 32, kind='ci'), dict(before='1' * 40, after='e' * 40))
            clock, loads = [0.0], []

            def slow_rejection(failure, pair, projects, timeout, skip=()):
                clock[0] += 100
                raise c.ColdfixError('slow rejection')

            def load_store(root):
                loads.append(clock[0])
                return root, {}

            with patch.object(learn_replay, 'REPLAY_SECONDS', 50), \
                    patch('time.monotonic', side_effect=lambda: clock[0]), \
                    patch.object(learn_replay, 'utc_day', return_value='2026-11-08'), \
                    patch.object(learn_replay.c, 'load_store', side_effect=load_store), \
                    patch.object(learn_replay, 'prior_proof', return_value=None), \
                    patch.object(learn_replay, 'replay_candidates', side_effect=lambda *args: iter([candidate])), \
                    patch.object(learn_replay, 'prepare', side_effect=slow_rejection):
                counts = learn_replay.run(root, ledger, projects=root, pairs_path=pairs)
            self.assertEqual(counts['deferred'], 1)
            self.assertEqual([at for at in loads if at >= 50], [])

    def test_the_replay_reads_the_store_once_and_sees_its_own_writes(self):
        def failure(source):
            return c.event_record(dict(command='python3 -m unittest', error='FAIL: test_value', repo='example/app',
                                       agent='codex', kind='test', source_ref=source,
                                       occurred='2026-11-15T12:00:00Z'), 'failure-ingest')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = [failure('https://example.invalid/runs/' + name) for name in ('1', '2')]
            for row in rows:
                c.save(root, 'failures', row)
            written = failure('https://example.invalid/runs/3')
            ledger = dict(runs=[], drafts={row['id']: dict(verdict='PASS', evidence=[row['id']]) for row in rows})
            (root / 'learn-drafts.json').write_text(json.dumps(ledger))
            pairs = root / 'pairs.json'
            pairs.write_text(json.dumps(dict(results=[])))
            read_store = c.read_store
            reads, seen = [], []

            def counted(path):
                reads.append(path)
                return read_store(path)

            def prepare(failure, pair, projects, timeout, skip=()):
                if skip:
                    raise c.ColdfixError('no changed unittest matches the recorded failure')
                return root, '1' * 40, 'e' * 40, 'tests/t.py', 'FAIL: test_value'

            def admit(args, store_root, store):
                seen.append((len(reads), c.load_store(store_root)[1] == read_store(store_root)))
                c.save(store_root, 'failures', written)
                raise c.ColdfixError('rejected')

            with patch.object(c, 'read_store', side_effect=counted), \
                    patch.object(learn_replay, 'utc_day', return_value='2026-11-15'), \
                    patch.object(learn_replay, 'prior_proof', return_value=None), \
                    patch.object(learn_replay, 'replay_candidates',
                                 side_effect=lambda store, entry, *args: iter([(store['failures'][entry['evidence'][0]],
                                                                                dict(before='1' * 40, after='e' * 40))])), \
                    patch.object(learn_replay, 'prepare', side_effect=prepare), \
                    patch.object(learn_replay, 'admission_args', return_value=None), \
                    patch.object(learn_replay.incident_admission, 'admit', side_effect=admit):
                counts = learn_replay.run(root, ledger, projects=root, pairs_path=pairs)
            self.assertEqual(counts['rejected'], 2)
            self.assertEqual(len(seen), 2)
            self.assertEqual(seen[0][0], seen[1][0])
            self.assertEqual([same for _, same in seen], [True, True])
            self.assertIn(written['id'], c.load_store(root)[1]['failures'])

    def replay_one_pair_on_a_clock(self, kind, matching_tests, runs, cost=100, resume=None, prepare_costs=(),
                                   generated_results=(), admit_error='incident not admitted; red PASS, green PASS, reversed-patch PASS ',
                                   admit_timeouts=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = dict(runs=[], drafts={'draft': dict(verdict='PASS', evidence=['draft'])})
            if resume is not None:
                ledger['drafts']['draft']['replay_resume'] = dict(revision=learn_replay.code_revision(), keys=[],
                                                                  reasons=[], pair=resume)
            (root / 'learn-drafts.json').write_text(json.dumps(ledger))
            pairs = root / 'pairs.json'
            pairs.write_text(json.dumps(dict(results=[])))
            failure = dict(id='f' * 32, kind=kind)
            candidate = (failure, dict(before='a' * 40, after='b' * 40))
            clock, started, outcomes, cursors = [0.0], [], [], []
            prepare_costs, generated_results = list(prepare_costs), list(generated_results)

            def prepare(failure, pair, projects, timeout, skip=()):
                clock[0] += prepare_costs.pop(0) if prepare_costs else 0
                if len(skip) == matching_tests:
                    raise c.ColdfixError('no changed unittest matches the recorded failure')
                return root, pair['before'], pair['after'], 'tests/test_%d.py' % len(skip), 'FAIL: test_%d' % len(skip)

            def admit(test_path, root, store):
                started.append(test_path)
                clock[0] += cost
                raise c.ColdfixError(admit_error + test_path if admit_error.endswith(' ') else admit_error)

            def generate(*args):
                started.append('generated')
                clock[0] += cost
                if generated_results:
                    return root, generated_results.pop(0)
                raise c.ColdfixError('generated failed')

            for _ in range(runs):
                started.clear()
                with patch.object(learn_replay, 'REPLAY_SECONDS', 50), \
                        patch('time.monotonic', side_effect=lambda: clock[0]), \
                        patch.object(learn_replay, 'utc_day', return_value='2026-11-08'), \
                        patch.object(learn_replay.c, 'load_store', return_value=(root, {})), \
                        patch.object(learn_replay, 'prior_proof', return_value=None), \
                        patch.object(learn_replay, 'replay_candidates', side_effect=lambda *args: iter([candidate])), \
                        patch.object(learn_replay, 'prepare', side_effect=prepare), \
                        patch.object(learn_replay, 'admission_args',
                                     side_effect=lambda *args: admit_timeouts is not None and admit_timeouts.append(args[8]) or args[6]), \
                        patch.object(learn_replay.incident_admission, 'admit', side_effect=admit), \
                        patch.object(learn_replay, 'generated_proof', side_effect=generate):
                    counts = learn_replay.run(root, ledger, projects=root, pairs_path=pairs)
                outcomes.append((list(started), counts.get('deferred'),
                                 ledger['drafts']['draft'].get('replay', {}).get('status')))
                cursors.append(json.loads((root / 'learn-drafts.json').read_text())['drafts']['draft']
                               .get('replay_resume', {}).get('pair'))
            return outcomes, ledger['drafts']['draft'], cursors

    def test_replay_budget_starts_no_further_test_in_a_pair_and_resumes_at_the_next_untried_test(self):
        outcomes, entry, cursors = self.replay_one_pair_on_a_clock('ci', 3, 4)
        self.assertEqual(outcomes, [(['tests/test_0.py'], 1, None), (['tests/test_1.py'], 1, None),
                                    (['tests/test_2.py'], 1, None), ([], None, 'rejected')])
        self.assertEqual([[path for path, _ in cursor['skipped']] if cursor else None for cursor in cursors],
                         [['tests/test_0.py'], ['tests/test_0.py', 'tests/test_1.py'],
                          ['tests/test_0.py', 'tests/test_1.py', 'tests/test_2.py'], None])
        self.assertEqual(cursors[0]['key'], ['f' * 32, 'a' * 40, 'b' * 40])
        self.assertEqual(entry['replay']['reason'], '; '.join(
            'incident not admitted; red PASS, green PASS, reversed-patch PASS tests/test_%d.py' % index
            for index in range(3)))

    def test_replay_budget_defers_a_generated_proof_to_the_next_run(self):
        outcomes, entry, _ = self.replay_one_pair_on_a_clock('review', 1, 2)
        self.assertEqual(outcomes, [(['tests/test_0.py'], 1, None), (['generated'], None, 'rejected')])
        self.assertEqual(entry['replay']['reason'],
                         'incident not admitted; red PASS, green PASS, reversed-patch PASS tests/test_0.py; '
                         'generated test: generated failed')

    def test_an_admission_cut_off_by_the_replay_budget_is_deferred_not_rejected(self):
        outcomes, entry, cursors = self.replay_one_pair_on_a_clock('review', 1, 2, admit_error='replay budget exhausted')
        self.assertEqual(outcomes, [(['tests/test_0.py'], 1, None)] * 2)
        self.assertEqual([cursor['skipped'] for cursor in cursors], [[], []])
        self.assertNotIn('replay budget exhausted', json.dumps(entry))

    def test_only_a_review_admission_splits_the_replay_budget_for_its_extra_run(self):
        timeouts = {}
        for kind in ('ci', 'review'):
            timeouts[kind] = []
            self.replay_one_pair_on_a_clock(kind, 1, 1, admit_timeouts=timeouts[kind])
        self.assertEqual(timeouts, dict(ci=[600], review=[12.5]))

    def test_replay_budget_spent_exactly_starts_no_further_test(self):
        outcomes, _, _ = self.replay_one_pair_on_a_clock('ci', 2, 1, cost=50)
        self.assertEqual(outcomes, [(['tests/test_0.py'], 1, None)])

    def test_replay_budget_spent_selecting_a_test_starts_no_admission(self):
        outcomes, _, cursors = self.replay_one_pair_on_a_clock('ci', 1, 3, prepare_costs=[60])
        self.assertEqual(outcomes, [([], 1, None), (['tests/test_0.py'], 1, None), ([], None, 'rejected')])
        self.assertEqual(cursors[0]['skipped'], [])

    def test_generated_proof_deferred_at_the_deadline_resumes_on_the_next_run(self):
        outcomes, _, cursors = self.replay_one_pair_on_a_clock('review', 1, 2, cost=10, generated_results=[None])
        self.assertEqual(outcomes, [(['tests/test_0.py', 'generated'], 1, None), (['generated'], None, 'rejected')])
        self.assertEqual([path for path, _ in cursors[0]['skipped']], ['tests/test_0.py'])

    def test_a_generated_proof_splits_the_remaining_budget_by_its_number_of_runs(self):
        timeouts = []
        repo = Path('/nonexistent-repo')
        with patch('time.monotonic', return_value=0.0), \
                patch.object(learn_replay, 'source_repo', return_value=repo), \
                patch.object(learn_replay.settle_codex_lessons, 'ancestor', return_value=True), \
                patch.object(learn_replay, 'stored_test',
                             return_value=dict(path='tests/test_generated.py', source='def test_x(self): pass\n')), \
                patch.object(learn_replay, 'test_names', return_value=['test_x']), \
                patch.object(learn_replay, 'admission_args',
                             side_effect=lambda *args: timeouts.append(args[8]) or learn_replay.argparse.Namespace()), \
                patch.object(learn_replay, 'new_file_patch', return_value='patch'), \
                patch.object(learn_replay.incident_admission, 'admit', return_value='proof'):
            for kind in ('ci', 'transcript', 'review'):
                learn_replay.generated_proof(Path('.'), {}, {}, dict(kind=kind), dict(before='a' * 40, after='b' * 40),
                                             None, 600, deadline=60)
        self.assertEqual(timeouts, [20, 20, 15])

    def test_generated_proof_starts_no_admission_once_writing_the_test_reaches_the_deadline(self):
        clock, admitted = [0.0], []

        def write(*args):
            clock[0] += 100
            return dict(path='tests/test_generated.py', source='def test_x(self): pass\n')

        repo = Path('/nonexistent-repo')
        with patch('time.monotonic', side_effect=lambda: clock[0]), \
                patch.object(learn_replay, 'source_repo', return_value=repo), \
                patch.object(learn_replay.settle_codex_lessons, 'ancestor', return_value=True), \
                patch.object(learn_replay, 'stored_test', side_effect=write), \
                patch.object(learn_replay, 'test_names', return_value=['test_x']), \
                patch.object(learn_replay, 'admission_args', side_effect=lambda *args: learn_replay.argparse.Namespace()), \
                patch.object(learn_replay, 'new_file_patch', return_value='patch'), \
                patch.object(learn_replay.incident_admission, 'admit', side_effect=lambda *args: admitted.append(1) or 'proof'):
            pair = dict(before='a' * 40, after='b' * 40)
            self.assertEqual(learn_replay.generated_proof(Path('.'), {}, {}, {}, pair, None, 1, deadline=50), (repo, None))
            self.assertEqual(admitted, [])
            self.assertEqual(learn_replay.generated_proof(Path('.'), {}, {}, {}, pair, None, 1, deadline=500), (repo, 'proof'))
            self.assertEqual(admitted, [1])

    def test_malformed_pair_cursor_rejects_the_draft_without_starting_a_test(self):
        for cursor in ('junk', dict(skipped=[])):
            outcomes, entry, _ = self.replay_one_pair_on_a_clock('ci', 1, 1, resume=cursor)
            self.assertEqual(outcomes, [([], None, 'rejected')])
            self.assertNotIn('replay_resume', entry)

    def test_recorded_verify_command_is_available_for_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / '.fleet').mkdir()
            (repo / '.fleet/verify.sh').write_text('#!/bin/sh\nexit 0\n')
            failure = dict(source='failure-ingest', source_ref='coldbox:fixture', error=json.dumps(dict(type='coldbox', command='./.fleet/verify.sh', log_tail='gate failed')))
            self.assertEqual(learn_replay.recorded_command(failure, repo), ['./.fleet/verify.sh'])

    def test_recorded_unsafe_command_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            failure = dict(source='failure-ingest', source_ref='coldbox:fixture', error=json.dumps(dict(type='coldbox', command='python3 -m unittest; rm -rf /', log_tail='failed')))
            with self.assertRaisesRegex(c.ColdfixError, 'unsafe recorded command'):
                learn_replay.recorded_command(failure, Path(directory))

    def test_recorded_command_cannot_use_an_unpinned_test_directory(self):
        failure = dict(source='failure-ingest', source_ref='coldbox:fixture', error=json.dumps(
            dict(type='coldbox', command='python3 -m unittest discover -s other_tests', log_tail='failed')))
        with self.assertRaisesRegex(c.ColdfixError, 'test inputs cannot be pinned'):
            learn_replay.recorded_command(failure, Path('.'))

    def test_recorded_verify_entrypoint_can_be_selected_from_an_unchecked_out_clone(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / 'repo'
            (repo / '.fleet').mkdir(parents=True)
            subprocess.run(['git', '-C', str(repo), 'init', '-q'], check=True)
            subprocess.run(['git', '-C', str(repo), 'config', 'user.name', 'Fixture'], check=True)
            subprocess.run(['git', '-C', str(repo), 'config', 'user.email', 'fixture@example.invalid'], check=True)
            script = repo / '.fleet/verify.sh'
            script.write_text('#!/bin/sh\nexit 0\n')
            script.chmod(0o755)
            subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
            subprocess.run(['git', '-C', str(repo), 'commit', '-qm', 'verify'], check=True)
            sha = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
            script.unlink()
            failure = dict(source='failure-ingest', source_ref='coldbox:fixture', error=json.dumps(
                dict(type='coldbox', command='./.fleet/verify.sh', head_sha=sha, log_tail='gate failed')))
            self.assertEqual(learn_replay.recorded_command(failure, repo), ['./.fleet/verify.sh'])

    def test_named_test_takes_precedence_over_recorded_command(self):
        failure = dict(source='failure-ingest', source_ref='coldbox:fixture', error=json.dumps(dict(type='coldbox', command='python3 -m unittest',
                                             log_tail='FAIL: test_value (test_value.Test.test_value)')))
        self.assertIsNone(learn_replay.recorded_command(failure, Path('.')))

    def test_ci_replay_ignores_failure_headers_quoted_by_the_failed_test(self):
        prefix = 'verify\tRun suite\t2026-11-12T04:55:03Z '
        quoted = r"AssertionError: 'value' not found in 'output\nFAIL: test_fixture (fixture.T)\n'"
        command = 'python3 -m unittest discover -s tests'
        metadata = dict(type='ci', failed_step_command=command, log_tail=prefix + quoted)
        failure = dict(source='failure-ingest', source_ref='https://github.com/example/project/actions/runs/1',
                       error=json.dumps(metadata))
        self.assertEqual(learn_replay.recorded_command(failure, Path('.')), command.split())
        metadata['log_tail'] = prefix + 'FAIL: test_outer (outer.Tests.test_outer)\n' + prefix + quoted
        failure['error'] = json.dumps(metadata)
        self.assertIsNone(learn_replay.recorded_command(failure, Path('.')))
        self.assertEqual(learn_replay.failed_tests(failure), ['test_outer'])
        logs = [metadata['log_tail'], json.dumps(dict(type='verify-fix-pair',
                failing_check=dict(excerpt=metadata['log_tail']), passing_check=dict(excerpt='FAIL: test_unrelated')))]
        for error in logs:
            with self.subTest(error=error):
                self.assertEqual(learn_replay.failed_tests(dict(error=error)), ['test_outer'])

    def test_missing_recorded_command_has_a_clear_reason(self):
        failure = dict(error=json.dumps(dict(type='ci', log_tail='verify failed')))
        with self.assertRaisesRegex(c.ColdfixError, 'no recorded failing command'):
            learn_replay.recorded_command(failure, Path('.'))

    def test_recorded_command_proves_red_green_and_reverse(self):
        self.check_recorded_command('python3  -m unittest discover -s tests',
                                    ['python3', '-m', 'unittest', 'discover', '-s', 'tests'])

    def test_recorded_verify_script_proves_red_green_and_reverse(self):
        self.check_recorded_command('./.fleet/verify.sh', ['./.fleet/verify.sh'])

    def test_previously_admitted_verify_checks_keep_their_exact_bytes(self):
        script, _, _ = regression_adapter.build_command(['./.fleet/verify.sh'], 'FAIL: test_value', 'a' * 40, 'FAIL: test_value')
        self.assertEqual(hashlib.sha256(script.encode()).hexdigest(),
                         '44e04ff2fbf60d29c049439eb5e7c1780bb8a165b80148c3586df588447bf9e1')

    def test_recorded_parallel_verify_script_proves_red_green_and_reverse(self):
        self.check_recorded_command('./.fleet/verify.sh', ['./.fleet/verify.sh'], recipe=regression_adapter.PARALLEL_VERIFY)

    def test_recorded_junit_parallel_verify_script_proves_red_green_and_reverse(self):
        self.check_recorded_command('./.fleet/verify.sh', ['./.fleet/verify.sh'], recipe=regression_adapter.PARALLEL_VERIFY_JUNIT)

    def test_github_verify_failure_without_a_recorded_command_proves_red_green_and_reverse(self):
        log = ('verify\tRun acme verify --ci\t2026-11-06T00:00:00.0000000Z FAILED (failures=1)\n'
               'verify\tRun acme verify --ci\t2026-11-06T00:00:01.0000000Z FAIL: real verification command failed')
        self.check_recorded_command('./.fleet/verify.sh', ['./.fleet/verify.sh'], metadata=dict(type='ci', log_tail=log),
                                    source_ref='https://github.com/example/project/actions/runs/7#attempt-1')

    def check_recorded_command(self, command, expected_argv, metadata=None, source_ref='coldbox:fixture',
                               recipe=regression_adapter.CANONICAL_VERIFY):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / 'project'
            (repo / 'tests').mkdir(parents=True)
            def git(*args):
                return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()
            git('init', '-q')
            git('config', 'user.name', 'Fixture')
            git('config', 'user.email', 'fixture@example.invalid')
            git('remote', 'add', 'origin', 'https://github.com/example/project.git')
            (repo / 'value.py').write_text('value = 1\n')
            (repo / 'tests/test_value.py').write_text('import unittest\nimport value\nclass TestValue(unittest.TestCase):\n def test_value(self): self.assertEqual(value.value, 2)\n')
            if command == './.fleet/verify.sh':
                (repo / '.fleet').mkdir()
                script = repo / '.fleet/verify.sh'
                script.write_bytes(recipe)
                if recipe != regression_adapter.CANONICAL_VERIFY:
                    (repo / 'tests/test_other.py').write_text('import unittest\nclass TestOther(unittest.TestCase):\n def test_other(self): pass\n')
                script.chmod(0o755)
            git('add', '.')
            git('commit', '-qm', 'broken')
            before = git('rev-parse', 'HEAD')
            (repo / 'value.py').write_text('value = 2\n')
            git('commit', '-qam', 'fix value')
            after = git('rev-parse', 'HEAD')
            store = root / '.coldfix'
            store.mkdir()
            metadata = dict(metadata or dict(type='coldbox', command=command, log_tail='FAILED (failures=1)'), head_sha=before)
            failure = c.event_record(dict(kind='ci', repo='example/project', command=command,
                error=json.dumps(metadata), agent='unknown', source_ref=source_ref,
                occurred='2026-11-06T00:00:00Z'), 'failure-ingest')
            c.save(store, 'failures', failure)
            entry = dict(verdict='PASS', evidence=[failure['id']], headline='Values must match.',
                         draft=dict(kind='test', title='Keep values correct', rule='Keep values correct.'))
            ledger = dict(drafts={'candidate': entry}, runs=[])
            (store / 'learn-drafts.json').write_text(json.dumps(ledger))
            pairs = root / 'pairs.json'
            pairs.write_text(json.dumps(dict(results=[dict(repo='example/project', before=before, after=after)])))
            result = learn_replay.run(store, ledger, projects=root, pairs_path=pairs)
            self.assertEqual(result['proven'], 1, entry.get('replay'))
            receipt = json.loads((store / 'receipts' / (entry['replay']['proof']['receipt_id'] + '.json')).read_text())
            self.assertEqual([receipt[x]['status'] for x in ('before', 'after', 'reversed')], ['FAIL', 'PASS', 'FAIL'])
            self.assertEqual(receipt['command_argv'], expected_argv)

    def correction_draft(self, root, lead=None, command='transcript correction'):
        failure = c.event_record(dict(kind='correction', repo='example/project', command=command,
            error=json.dumps(dict(type='transcript_correction', evidence='assistant [a1]: I copied the helper.')),
            agent='codex', source_ref='transcript:fixture#messages=a1', occurred='2026-11-07T00:00:00Z'), 'transcripts')
        c.save(root, 'failures', failure)
        if lead is not None:
            (root / 'transcript-lessons.json').write_text(json.dumps(dict(leads={failure['id']: lead}, batches=[])))
        entry = dict(verdict='PASS', evidence=[failure['id']], headline='Use the shared helper.',
                     draft=dict(kind='correction', title='Reuse helpers', rule='Reuse the shared helper.'))
        ledger = dict(drafts={'candidate': entry}, runs=[])
        (root / 'learn-drafts.json').write_text(json.dumps(ledger))
        pairs = root / 'pairs.json'
        pairs.write_text(json.dumps(dict(results=[])))
        with patch.object(learn_replay, 'replay_candidates') as replayed:
            results = []
            for day in ('2026-11-08', '2026-11-09'):
                with patch.object(learn_replay, 'utc_day', return_value=day):
                    results.append(learn_replay.run(root, ledger, projects=root, pairs_path=pairs))
        replayed.assert_not_called()
        return entry, results

    def test_transcript_correction_drafts_take_the_transcript_route_verdict(self):
        cases = (('no_lesson', dict(status='no_lesson', reason='no_mistake')),
                 ('unprovable', dict(status='unprovable', reason='code mistake needs repository replay')),
                 ('duplicate', dict(status='duplicate', reason='coldlocker:lessons/L16-keep-going')),
                 ('proven', dict(status='proven', proof=dict(replay_applies=0.86))))
        prefix = 'not_replayable_correction_only'
        for reason, lead, command in [(f'{prefix}: transcript {v}', lead, 'transcript correction') for v, lead in cases] + [
                (prefix, None, 'correction'), (prefix, None, 'scope')]:
            with self.subTest(reason, command=command), tempfile.TemporaryDirectory() as directory:
                entry, results = self.correction_draft(Path(directory), lead, command)
                self.assertEqual(results, [dict(proven=0, rejected=1), dict(proven=0, rejected=0)])
                self.assertEqual(entry['replay'], dict(status='rejected', reason=reason))
                self.assertTrue(entry['replay_settled']['confirmed'])
                self.assertEqual([a['day'] for a in entry['replay_attempts']], ['2026-11-08'])

    def test_transcript_correction_drafts_stay_open_until_the_transcript_route_settles(self):
        cases = (('missing', None), ('retrying', dict(status='retry', attempts=1, reason='jev check failed')),
                 ('needs approval', dict(status='needs_approval')),
                 ('proven without replay', dict(status='proven', proof=dict())))
        for name, lead in cases:
            with self.subTest(name), tempfile.TemporaryDirectory() as directory:
                entry, results = self.correction_draft(Path(directory), lead)
                self.assertEqual(results, [dict(proven=0, rejected=1), dict(proven=0, rejected=1)])
                self.assertEqual(entry['replay'], dict(status='rejected',
                                                       reason='not_replayable_correction_only: transcript route pending'))
                self.assertFalse(entry['replay_settled']['confirmed'])

    def test_transcript_correction_drafts_are_not_replayed_as_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            failure = c.event_record(dict(kind='correction', repo='example/project', command='correction',
                error='Use the shared helper.', agent='codex', source_ref='transcript:fixture',
                occurred='2026-11-07T00:00:00Z'), 'transcripts')
            c.save(root, 'failures', failure)
            entry = dict(verdict='PASS', evidence=[failure['id']], headline='Use the shared helper.',
                         draft=dict(kind='correction', title='Reuse helpers', rule='Reuse the shared helper.'))
            ledger = dict(drafts={'candidate': entry}, runs=[])
            (root / 'learn-drafts.json').write_text(json.dumps(ledger))
            pairs = root / 'pairs.json'
            pairs.write_text(json.dumps(dict(results=[])))
            with patch.object(learn_replay, 'replay_candidates') as replayed:
                results = []
                for day in ('2026-11-08', '2026-11-09'):
                    with patch.object(learn_replay, 'utc_day', return_value=day):
                        results.append(learn_replay.run(root, ledger, projects=root, pairs_path=pairs))
            replayed.assert_not_called()
            self.assertEqual(results, [dict(proven=0, rejected=1), dict(proven=0, rejected=0)])
            self.assertEqual(entry['replay'], dict(status='rejected', reason='not_replayable_correction_only'))
            self.assertTrue(entry['replay_settled']['confirmed'])
            self.assertEqual([a['day'] for a in entry['replay_attempts']], ['2026-11-08'])

    def test_renamed_repository_is_cloned_from_its_recorded_new_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = learn_replay.SourceProjects(root / 'projects', root, 'example', 30)
            failure = dict(id='f' * 32, repo='example/project', error=json.dumps(dict(head_sha='a' * 40)))
            commands = []
            with patch.object(learn_replay, 'moved', {'f' * 32: 'example/project-private'}, create=True), \
                    patch.object(learn_replay, 'clone_source', side_effect=lambda command, timeout, env: commands.append(command)):
                with self.assertRaisesRegex(c.ColdfixError, 'recorded commit not available on GitHub'):
                    learn_replay.source_repo(failure, source)
            self.assertEqual(commands[0][-2], 'https://github.com/example/project-private.git')

    def test_without_a_checkouts_root_sources_come_from_a_mirror(self):
        with tempfile.TemporaryDirectory() as directory:
            source = learn_replay.SourceProjects(None, Path(directory), 'example', 30)
            failure = dict(id='f' * 32, repo='example/project', error=json.dumps(dict(head_sha='a' * 40)))
            commands = []
            with patch.object(learn_replay, 'clone_source', side_effect=lambda command, timeout, env: commands.append(command)):
                with self.assertRaisesRegex(c.ColdfixError, 'recorded commit not available on GitHub'):
                    learn_replay.source_repo(failure, source)
            self.assertEqual(commands[0][-2], 'https://github.com/example/project.git')

    def test_renamed_repository_is_found_under_its_new_name_in_a_projects_root(self):
        with tempfile.TemporaryDirectory() as directory:
            projects = Path(directory)
            (projects / 'project-private').mkdir()
            failure = dict(id='f' * 32, repo='example/project', error='{}')
            with patch.object(learn_replay, 'moved', {'f' * 32: 'example/project-private'}, create=True), \
                    patch.object(learn_replay.settle_codex_lessons, 'slug_of', return_value='example/project-private'), \
                    patch.object(c, 'privacy_git', return_value=b'false\n'):
                try:
                    repo = learn_replay.source_repo(failure, projects)
                except c.ColdfixError as error:
                    self.fail(f'relocated checkout was not used: {error}')
            self.assertEqual(repo.name, 'project-private')

    def test_backfill_maps_a_renamed_repository_by_its_run_and_commit(self):
        import importlib.util
        self.assertIsNotNone(importlib.util.find_spec('replay_repos'), 'rename backfill is missing')
        import replay_repos
        head = 'a' * 40
        runs = {'example/project-private': head, 'example/project-old': 'b' * 40}
        def gh(command, raw=False, missing=None):
            endpoint = command[1] if command[1] != '--paginate' else command[2]
            if endpoint == 'rate_limit':
                return dict(resources=dict(core=dict(remaining=4000)))
            if endpoint.startswith('user/repos'):
                return 'example/project\nexample/project-old\nexample/project-private\nexample/other\n'
            if endpoint == 'repositories/42':
                return dict(full_name='example/project-private')
            slug = endpoint.split('/actions/')[0].removeprefix('repos/')
            return dict(head_sha=runs[slug]) if slug in runs else None
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records = []
            for number, extra in ((7, {}), (8, dict(repository_id=42))):
                failure = c.event_record(dict(kind='ci', repo='example/project', command='verify',
                    error=json.dumps(dict(type='ci', head_sha=head, run_id=number, log_tail='failed', **extra)),
                    agent='unknown', source_ref=f'https://github.com/example/project/actions/runs/{number}#attempt-1',
                    occurred='2026-11-06T00:00:00Z'), 'failure-ingest')
                c.save(root, 'failures', failure)
                records.append(failure['id'])
            attempts = [dict(day='2026-11-06', failure=records[0], reason='recorded commit not available on GitHub'),
                        dict(day='2026-11-06', failure=None, reason='0' * 32 + ': no recorded failing command; '
                             + records[1] + ': recorded commit not available on GitHub')]
            (root / 'learn-drafts.json').write_text(json.dumps(dict(runs=[], drafts={'d': dict(replay_attempts=attempts)})))
            with patch.object(replay_repos, 'gh', side_effect=gh):
                first = replay_repos.backfill(root, 'example', 20)
                second = replay_repos.backfill(root, 'example', 20)
            self.assertEqual(replay_repos.load(root), {ref: 'example/project-private' for ref in records})
            self.assertEqual((first['checked'], second['checked'], second['moved']), (2, 0, 2))

    def test_backfill_retries_a_repository_it_could_not_locate_before(self):
        import importlib.util
        self.assertIsNotNone(importlib.util.find_spec('replay_repos'), 'rename backfill is missing')
        import replay_repos
        head = 'a' * 40
        runs = {}
        def gh(command, raw=False, missing=None):
            endpoint = command[1] if command[1] != '--paginate' else command[2]
            if endpoint == 'rate_limit':
                return dict(resources=dict(core=dict(remaining=4000)))
            if endpoint == 'repositories/42':
                return dict(full_name='example/project-private')
            slug = endpoint.split('/actions/')[0].removeprefix('repos/')
            return dict(head_sha=runs[slug]) if slug in runs else None
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(replay_repos.backfill(root, 'example', 20)['checked'], 0)
            failure = c.event_record(dict(kind='ci', repo='example/project', command='verify',
                error=json.dumps(dict(type='ci', head_sha=head, run_id=7, log_tail='failed', repository_id=42)),
                agent='unknown', source_ref='https://github.com/example/project/actions/runs/7#attempt-1',
                occurred='2026-11-06T00:00:00Z'), 'failure-ingest')
            c.save(root, 'failures', failure)
            attempts = [dict(day='2026-11-06', failure=failure['id'], reason='recorded commit not available on GitHub')]
            (root / 'learn-drafts.json').write_text(json.dumps(dict(runs=[], drafts={'d': dict(replay_attempts=attempts)})))
            with patch.object(replay_repos, 'gh', side_effect=gh):
                replay_repos.backfill(root, 'example', 20)
                runs['example/project-private'] = head
                replay_repos.backfill(root, 'example', 20)
            self.assertEqual(replay_repos.load(root), {failure['id']: 'example/project-private'})

    def rename_store(self, root):
        head = 'a' * 40
        rows = (('review', 'review', 'example/project', None), ('correction', 'correction', 'EXAMPLE/Project', None),
                ('look-alike', 'review', 'example/project-old', None), ('ci-renamed', 'ci', 'example/project', 8),
                ('ci-other-head', 'ci', 'example/project', 9), ('coldbox', 'coldbox', 'example/project', 10),
                ('ci-no-run', 'ci', 'example/project', None), ('coldbox-local', 'coldbox', 'example/project', None),
                ('other-kind', 'test', 'example/project', None))
        records = {}
        for name, kind, repo, run in rows:
            if kind == 'coldbox':
                error = dict(type='coldbox', sha=head, github=dict(run_id=run) if run else {}, log_tail=name)
                source_ref = 'coldbox:' + name
            elif run or name == 'ci-no-run':
                error = dict(type='ci', head_sha=head, log_tail=name)
                source_ref = f'https://github.com/{repo}/actions/runs/{run}#attempt-1' if run else 'ci:' + name
            else:
                error, source_ref = None, f'https://github.com/{repo}/pull/1#discussion_r1'
            failure = c.event_record(dict(kind='ci' if kind == 'coldbox' else kind, repo=repo, command='verify',
                error=json.dumps(error) if error else name, agent='unknown', source_ref=source_ref,
                occurred='2026-11-06T00:00:00Z'), 'github')
            c.save(root, 'failures', failure)
            records[name] = failure['id']
        return records

    def rename_gh(self, calls, full_name='example/project-private'):
        heads = {'8': 'a' * 40, '9': 'b' * 40, '10': 'a' * 40}
        def gh(command, raw=False, missing=None):
            calls.append(command[1])
            if command[1] == 'repos/example/project':
                return dict(full_name=full_name)
            prefix = 'repos/example/project-private/actions/runs/'
            if command[1].startswith(prefix):
                return dict(head_sha=heads[command[1][len(prefix):]])
            self.fail('unexpected GitHub call: ' + command[1])
        return gh

    def test_rename_maps_only_the_old_name_and_ci_only_by_its_run(self):
        import replay_repos
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records = self.rename_store(root)
            self.assertEqual(replay_repos.unmapped(root, 'example/project'), 5)
            with patch.object(replay_repos, 'gh', side_effect=self.rename_gh(calls)):
                result = replay_repos.rename(root, 'example/project', 'example/project-private', 10)
            new = 'example/project-private'
            self.assertEqual(replay_repos.load(root), {records['review']: new, records['correction']: new,
                                                       records['ci-renamed']: new, records['coldbox']: new,
                                                       records['ci-other-head']: None})
            self.assertEqual(result, dict(calls=4, unchecked=0, unmapped=1, unmappable=3))
            self.assertEqual(calls[0], 'repos/example/project')
            self.assertEqual(replay_repos.unmapped(root, 'example/project-old'), 1)
            look_alike = dict(id=records['look-alike'], repo='example/project-old', error='{}')
            (root / 'projects' / 'project-old').mkdir(parents=True)
            with patch.object(learn_replay, 'moved', replay_repos.load(root)), \
                    patch.object(learn_replay.settle_codex_lessons, 'slug_of', return_value=new), \
                    patch.object(c, 'privacy_git', return_value=b'false\n'), \
                    self.assertRaisesRegex(c.ColdfixError, 'source repository checkout unavailable'):
                learn_replay.source_repo(look_alike, root / 'projects')

    def test_rename_spends_at_most_its_budget_and_the_next_run_continues(self):
        import replay_repos
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.rename_store(root)
            gh = self.rename_gh(calls)
            with patch.object(replay_repos, 'gh', side_effect=gh):
                first = replay_repos.rename(root, 'example/project', 'example/project-private', 2)
            self.assertEqual((len(calls), first['calls'], first['unchecked']), (2, 2, 2))
            calls.clear()
            with patch.object(replay_repos, 'gh', side_effect=gh):
                second = replay_repos.rename(root, 'example/project', 'example/project-private', 3)
            self.assertEqual(len(calls), 3)
            self.assertEqual(second['unchecked'], 0)
            self.assertEqual(len(replay_repos.load(root)), 5)

    def test_rename_refuses_unless_github_redirects_old_to_new(self):
        import replay_repos
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.rename_store(root)
            for full_name, new in (('example/project', 'example/project-private'),
                                   ('example/project-private', 'example/project-privat'),
                                   ('example/project-private2', 'example/project-private')):
                with self.subTest(full_name=full_name, new=new), \
                        patch.object(replay_repos, 'gh', side_effect=self.rename_gh([], full_name)), \
                        self.assertRaisesRegex(c.ColdfixError, 'does not redirect'):
                    replay_repos.rename(root, 'example/project', new, 10)
            self.assertFalse((root / replay_repos.PATH).exists())
            with self.assertRaisesRegex(c.ColdfixError, 'budget'):
                replay_repos.rename(root, 'example/project', 'example/project-private', 0)

    def test_rename_never_replaces_an_existing_mapping(self):
        import replay_repos
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records = self.rename_store(root)
            (root / replay_repos.PATH).write_text(json.dumps({records['ci-renamed']: 'example/elsewhere'}))
            with patch.object(replay_repos, 'gh', side_effect=self.rename_gh([])):
                replay_repos.rename(root, 'example/project', 'example/project-private', 10)
            self.assertEqual(replay_repos.load(root)[records['ci-renamed']], 'example/elsewhere')

    def test_rename_maps_a_coldbox_pr_merge_commit_only_when_github_shows_the_run_head_as_its_merged_parent(self):
        import replay_repos
        base, head, other = 'b' * 40, 'a' * 40, 'c' * 40
        cases = (('merge', 'coldbox', 21, 'pull_request', [base, head]),
                 ('head-first', 'coldbox', 22, 'pull_request', [head, base]),
                 ('other-parent', 'coldbox', 23, 'pull_request', [base, other]),
                 ('push', 'coldbox', 24, 'push', [base, head]),
                 ('ci', 'ci', 25, 'pull_request', [base, head]),
                 ('three-parents', 'coldbox', 26, 'pull_request', [base, head, other]))
        records, runs, commits, calls = {}, {}, {}, []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, kind, run, event, parents in cases:
                sha = f'{run:040d}'
                if kind == 'coldbox':
                    error, source_ref = dict(type='coldbox', sha=sha, github=dict(run_id=run)), 'coldbox:' + name
                else:
                    error = dict(type='ci', head_sha=sha)
                    source_ref = f'https://github.com/example/project/actions/runs/{run}#attempt-1'
                failure = c.event_record(dict(kind='ci', repo='example/project', command='verify',
                    error=json.dumps(error), agent='unknown', source_ref=source_ref,
                    occurred='2026-11-06T00:00:00Z'), 'github')
                c.save(root, 'failures', failure)
                records[name] = failure['id']
                runs[str(run)] = dict(head_sha=head, event=event)
                commits[sha] = dict(parents=[dict(sha=parent) for parent in parents])
            def gh(command, raw=False, missing=None):
                calls.append(command[1])
                if command[1] == 'repos/example/project':
                    return dict(full_name='example/project-private')
                kind, key = command[1].removeprefix('repos/example/project-private/').rsplit('/', 1)
                return {'actions/runs': runs, 'commits': commits}[kind][key]
            expected = dict.fromkeys(records.values())
            expected[records['merge']] = 'example/project-private'
            for budget in range(1, 13):
                calls.clear()
                (root / replay_repos.PATH).unlink(missing_ok=True)
                with self.subTest(budget=budget), patch.object(replay_repos, 'gh', side_effect=gh):
                    spent = replay_repos.rename(root, 'example/project', 'example/project-private', budget)['calls']
                    self.assertEqual(spent, len(calls))
                    self.assertLessEqual(spent, budget)
                    self.assertIsNot(replay_repos.load(root).get(records['merge'], 'unchecked'), None)
                    replay_repos.rename(root, 'example/project', 'example/project-private', 20)
                    self.assertEqual(replay_repos.load(root), expected)
            calls.clear()
            (root / replay_repos.PATH).unlink()
            with patch.object(replay_repos, 'gh', side_effect=gh):
                result = replay_repos.rename(root, 'example/project', 'example/project-private', 20)
            self.assertEqual(replay_repos.load(root), expected)
            self.assertEqual((result['calls'], result['unmapped']), (11, 5))
            self.assertEqual(sorted(call for call in calls if '/commits/' in call),
                             [f'repos/example/project-private/commits/{run:040d}' for run in (21, 22, 23, 26)])

    def test_unmapped_and_rename_from_the_command_line(self):
        import replay_repos
        from contextlib import redirect_stdout
        import io
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / '.coldfix'
            root.mkdir(mode=0o700)
            self.rename_store(root)
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(replay_repos.main(['--store', str(root), '--unmapped', 'example/project']), 0)
                with patch.object(replay_repos, 'gh', side_effect=self.rename_gh([])):
                    self.assertEqual(replay_repos.main(['--store', str(root), '--rename', 'example/project',
                                                        'example/project-private', '--budget', '10']), 0)
                self.assertEqual(replay_repos.main(['--store', str(root), '--unmapped', 'example/project']), 0)
            lines = out.getvalue().split('\n')
            self.assertEqual((lines[0], json.loads(lines[1])['unmapped'], lines[2]), ('5', 1, '1'))
            with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()), patch('sys.stderr', io.StringIO()):
                replay_repos.main(['--store', str(root), '--rename', 'example/project', 'example/project-private'])

    def test_preparation_timeout_after_a_rejected_test_stops_the_pair(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            failure = c.event_record(dict(kind='ci', repo='example/project', command='verify', error='FAIL: test_value',
                agent='unknown', source_ref='https://example.invalid/run/1', occurred='2026-11-05T00:00:00Z'), 'failure-ingest')
            c.save(root, 'failures', failure)
            entry = dict(verdict='PASS', evidence=[failure['id']], headline='A value failed.',
                         draft=dict(kind='test', title='Keep values correct', rule='Keep values correct.'))
            ledger = dict(drafts={'candidate': entry}, runs=[])
            (root / 'learn-drafts.json').write_text(json.dumps(ledger))
            pairs = root / 'pairs.json'
            pairs.write_text(json.dumps(dict(results=[])))
            pair = dict(before='a' * 40, after='b' * 40)
            prepared = (root, pair['before'], pair['after'], 'tests/test_a.py', 'FAIL: test_value')
            with patch.object(learn_replay, 'replay_candidates', return_value=iter([(failure, pair)])), \
                    patch.object(learn_replay, 'prepare', side_effect=[prepared, subprocess.TimeoutExpired('git', 1)]) as prepare, \
                    patch.object(learn_replay.c, 'privacy_git', return_value=b'Fix the value'), \
                    patch.object(learn_replay.incident_admission, 'admit', side_effect=c.ColdfixError('wrong test')):
                result = learn_replay.run(root, ledger, pairs_path=pairs)
            self.assertEqual(result, dict(proven=0, rejected=2))
            self.assertEqual(prepare.call_count, 2)
            self.assertIn('timed out', entry['replay']['reason'])

    def test_replay_tries_each_matching_test_before_rejecting_a_pair(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            failure = c.event_record(dict(kind='ci', repo='example/project', command='verify', error='FAIL: test_value',
                agent='unknown', source_ref='https://example.invalid/run/1', occurred='2026-11-05T00:00:00Z'), 'failure-ingest')
            c.save(root, 'failures', failure)
            entry = dict(verdict='PASS', evidence=[failure['id']], headline='A value failed.',
                         draft=dict(kind='test', title='Keep values correct', rule='Keep values correct.'))
            ledger = dict(drafts={'candidate': entry}, runs=[])
            (root / 'learn-drafts.json').write_text(json.dumps(ledger))
            pairs = root / 'pairs.json'
            pairs.write_text(json.dumps(dict(results=[])))
            pair = dict(before='a' * 40, after='b' * 40)
            def choose(failure, pair, projects, timeout, *, skip):
                path = 'tests/test_b.py' if ('tests/test_a.py', 'FAIL: test_value') in skip else 'tests/test_a.py'
                return root, pair['before'], pair['after'], path, 'FAIL: test_value'
            proof = dict(id='c' * 32, lesson_id='d' * 32, receipt_id='e' * 32, status='PASS', verification='proven')
            with patch.object(learn_replay, 'replay_candidates', return_value=iter([(failure, pair)])), \
                    patch.object(learn_replay, 'prepare', side_effect=choose), \
                    patch.object(learn_replay.c, 'privacy_git', return_value=b'Fix the value'), \
                    patch.object(learn_replay.incident_admission, 'admit', side_effect=[c.ColdfixError('wrong test'), proof]) as admit:
                result = learn_replay.run(root, ledger, pairs_path=pairs)
            self.assertEqual(result, dict(proven=1, rejected=1))
            self.assertEqual([call.args[0].test_path for call in admit.call_args_list], ['tests/test_a.py', 'tests/test_b.py'])

    def test_timed_out_replay_is_recorded_as_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before, after = 'a' * 40, 'b' * 40
            failure = c.event_record(dict(kind='ci', repo='example/project', command='verify',
                error=json.dumps(dict(head_sha=before, log_tail='FAIL: test_value')), agent='unknown',
                source_ref='https://example.invalid/run/1', occurred='2026-11-05T00:00:00Z'), 'failure-ingest')
            c.save(root, 'failures', failure)
            entry = dict(day=datetime.now(timezone.utc).date().isoformat(), verdict='PASS', evidence=[failure['id']])
            ledger = dict(drafts={'candidate': entry}, runs=[])
            (root / 'learn-drafts.json').write_text(json.dumps(ledger))
            pairs = root / 'pairs.json'
            pairs.write_text(json.dumps(dict(results=[dict(repo='example/project', before=before, after=after)])))
            with patch.object(learn_replay, 'prepare', side_effect=subprocess.TimeoutExpired('git', 1)):
                result = learn_replay.run(root, ledger, pairs_path=pairs)
            self.assertEqual(result['rejected'], 1)
            recorded = json.loads((root / 'learn-drafts.json').read_text())['drafts']['candidate']
            self.assertEqual(recorded['replay']['status'], 'rejected')
            self.assertIn('timed out', recorded['replay_attempts'][0]['reason'])

    def test_finds_a_recorded_failure_in_a_descendant_regression_test(self):
        with tempfile.TemporaryDirectory() as directory:
            projects = Path(directory)
            repo = projects / 'project'
            repo.mkdir()
            def git(*args):
                return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()
            git('init', '-q')
            git('config', 'user.name', 'Fixture')
            git('config', 'user.email', 'fixture@example.invalid')
            git('remote', 'add', 'origin', 'https://github.com/example/project.git')
            (repo / 'value.py').write_text('value = 1\n')
            git('add', '.')
            git('commit', '-qm', 'broken')
            before = git('rev-parse', 'HEAD')
            (repo / 'value.py').write_text('value = 2\n')
            (repo / 'tests').mkdir()
            (repo / 'tests/test_value.py').write_text('import unittest\nclass Tests(unittest.TestCase):\n'
                                                      '    def test_value(self):\n        self.assertTrue(True)\n')
            (repo / 'tests/test_aaa.py').write_text('import unittest\nclass Tests(unittest.TestCase):\n'
                                                    '    def test_value(self):\n        self.assertTrue(True)\n'
                                                    '    def test_value_extra(self):\n        self.assertTrue(True)\n')
            git('add', '.')
            git('commit', '-qm', 'fixed')
            after = git('rev-parse', 'HEAD')
            failure = dict(repo='example/project', error=json.dumps(dict(head_sha=before, log_tail='FAIL: test_value')))
            pair = dict(before=before, after=after)
            first = learn_replay.prepare(failure, pair, projects, 30)
            second = learn_replay.prepare(failure, pair, projects, 30, skip={(first[-2], first[-1])})
            self.assertEqual({first[-2], second[-2]}, {'tests/test_aaa.py', 'tests/test_value.py'})
            extended = dict(failure, error='FAIL: test_value_extra')
            self.assertEqual(learn_replay.prepare(extended, pair, projects, 30)[-1], 'FAIL: test_value_extra')
            self.assertEqual(list(learn_replay.history_candidates(failure, projects, 30)),
                             [dict(repo='example/project', before=before, after=after)])
            for kind in ('ci', 'correction'):
                with self.subTest(history_kind=kind):
                    record = dict(failure, id='f1', kind=kind)
                    reasons = []
                    self.assertEqual(list(learn_replay.replay_candidates(
                        dict(failures=dict(f1=record)), dict(evidence=['f1']), [], projects, 30, reasons)),
                        [(record, dict(repo='example/project', before=before, after=after))])
                    self.assertEqual(reasons, [])
            failure['error'] = json.dumps(dict(head_sha=before, log_tail='ERROR: test_value'))
            self.assertEqual(learn_replay.prepare(failure, pair, projects, 30)[-1], 'ERROR: test_value')
            with self.assertRaisesRegex(c.ColdfixError, 'not a descendant'):
                learn_replay.prepare(failure, dict(before=after, after=before), projects, 30)
            (repo / 'value.py').write_text('value = 3\n')
            git('add', 'value.py')
            git('commit', '-qm', 'another fix')
            latest = git('rev-parse', 'HEAD')
            self.assertEqual(learn_replay.prepare(failure, dict(before=after, after=latest), projects, 30)[-1],
                             'ERROR: test_value')

    def test_repeat_replay_of_a_rejected_pair_adds_no_lesson_or_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = root / 'store'
            store.mkdir(mode=0o700)
            repo = root / 'project'
            repo.mkdir()
            def git(*args):
                return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()
            git('init', '-q')
            git('config', 'user.name', 'Fixture')
            git('config', 'user.email', 'fixture@example.invalid')
            (repo / 'tests').mkdir()
            (repo / 'tests/test_value.py').write_text('import unittest\nclass ValueTests(unittest.TestCase):\n'
                                                      '    def test_value(self):\n        self.assertTrue(True)\n')
            (repo / 'value').write_text('bad')
            git('add', '.')
            git('commit', '-qm', 'before')
            before = git('rev-parse', 'HEAD')
            (repo / 'value').write_text('good')
            git('commit', '-qam', 'Correct the value')
            after = git('rev-parse', 'HEAD')
            now = datetime.now(timezone.utc)
            failure = c.event_record(dict(kind='ci', repo='example/project', command='verify', error='FAIL: test_value',
                agent='unknown', source_ref='https://example.invalid/run/1', occurred='2026-11-05T00:00:00Z'), 'failure-ingest')
            c.save(store, 'failures', failure)
            entry = dict(day=now.date().isoformat(), verdict='PASS', evidence=[failure['id']], headline='A value failed.',
                         draft=dict(kind='test', title='Keep values correct', rule='Keep values correct.'))
            ledger = dict(drafts={'candidate': entry}, runs=[])
            (store / 'learn-drafts.json').write_text(json.dumps(ledger))
            pairs = root / 'pairs.json'
            pairs.write_text(json.dumps(dict(results=[])))
            pair = dict(before=before, after=after)

            def rerun(checkout, day):
                prepared = (checkout, before, after, 'tests/test_value.py', 'FAIL: test_value')
                with patch.object(learn_replay, 'replay_candidates', side_effect=lambda *args: iter([(failure, pair)])), \
                        patch.object(learn_replay, 'prepare', side_effect=[prepared, c.ColdfixError(
                            'no changed unittest matches the recorded failure')]), \
                        patch.object(learn_replay, 'utc_day', return_value=day), \
                        patch.object(replay_isolation, 'replay_tree', c.replay_tree):
                    result = learn_replay.run(store, ledger, projects=root, pairs_path=pairs)
                files = {path.relative_to(store): path.read_bytes()
                         for collection in ('failures', 'lessons', 'receipts') for path in (store / collection).glob('*.json')}
                return result, files

            first, files = rerun(repo, '2026-11-05')
            self.assertEqual(first, dict(proven=0, rejected=1))
            self.assertEqual(entry['replay']['status'], 'rejected')
            self.assertIn('incident not admitted', entry['replay']['reason'])
            clone = root / 'workspace' / 'project'
            subprocess.run(['git', 'clone', '-q', str(repo), str(clone)], check=True)
            next_day, files_next_day = rerun(clone, '2026-11-06')
            self.assertEqual(next_day, dict(proven=0, rejected=0))
            self.assertEqual(files_next_day, files)
            self.assertEqual(entry['replay']['status'], 'rejected')
            second, files_again = rerun(clone, '2026-11-13')
            self.assertEqual(second, dict(proven=0, rejected=1))
            self.assertEqual(files_again, files)
            self.assertEqual([row['day'] for row in entry['replay_attempts']], ['2026-11-05', '2026-11-13'])

    def test_unmatched_collected_failure_is_recorded_and_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime.now(timezone.utc)
            failure = c.event_record(dict(kind='ci', repo='example/project', command='verify',
                error='FAIL: test_value', agent='unknown', source_ref='https://example.invalid/run/1',
                occurred='2026-11-05T00:00:00Z'), 'failure-ingest')
            c.save(root, 'failures', failure)
            entry = dict(day=now.date().isoformat(), verdict='PASS', evidence=[failure['id']])
            ledger = dict(drafts={'candidate': entry}, runs=[])
            (root / 'learn-drafts.json').write_text(json.dumps(ledger))
            pairs = root / 'pairs.json'
            pairs.write_text(json.dumps(dict(results=[])))
            result = learn_replay.run(root, ledger, pairs_path=pairs)
            self.assertEqual(result, dict(proven=0, rejected=1))
            reason = failure['id'] + ': source repository checkout unavailable'
            self.assertEqual(json.loads((root / 'learn-drafts.json').read_text())['drafts']['candidate']['replay'],
                             dict(status='rejected', reason=reason))
            report = learn_report.report(root, now)
            self.assertEqual(report['lessons_rejected'], {'ours': {'source repository checkout unavailable': 1}})


class LearnReplayNoRuleFileTests(unittest.TestCase):
    setUp = test_learn.LearnTests.setUp
    run_cli = test_learn.LearnTests.run_cli
    event = test_learn.LearnTests.event
    git = test_learn.LearnTests.git
    public_inputs = test_learn.LearnTests.public_inputs

    def test_a_proven_draft_stays_proven_and_writes_no_rule_file(self):
        hub = self.cwd / 'hub.py'
        hub.write_text('raise SystemExit(1)\n')
        self.delivery = ['--agentlocker', str(hub), '--target', str(self.repo),
                         '--exports', str(self.cwd / 'exports'), *self.public_inputs()[1:]]
        self.git('remote', 'add', 'origin', 'https://github.com/example/repo.git')
        (self.repo / 'tests').mkdir()
        (self.repo / 'tests/test_value.py').write_text(
            'import unittest\nfrom pathlib import Path\nclass ValueTests(unittest.TestCase):\n'
            '    def test_value(self):\n        self.assertEqual(Path("value").read_text(), "good")\n')
        self.git('add', 'tests')
        self.git('commit', '-qm', 'Retain the value regression test')
        store = self.cwd / 'learn-loop' / '.coldfix'
        store.mkdir(parents=True, mode=0o700)
        error = json.dumps(dict(head_sha=self.before, log_tail='FAIL: test_value'))
        failure = c.event_record(dict(kind='ci', repo='example/repo', command='verify', error=error,
            agent='unknown', source_ref='https://github.com/example/repo/actions/runs/1',
            occurred='2026-11-05T00:00:00Z'), 'failure-ingest')
        c.save(store, 'failures', failure)
        entry = dict(day=datetime.now(timezone.utc).date().isoformat(), verdict='PASS', evidence=[failure['id']],
                     headline='The value was incorrect.', draft=dict(kind='test', title='Correct the value',
                     rule='Keep the value correct.'))
        ledger = dict(drafts={'candidate': entry}, runs=[])
        (store / 'learn-drafts.json').write_text(json.dumps(ledger))
        pairs = self.cwd / 'pairs.json'
        pairs.write_text(json.dumps(dict(results=[dict(repo='example/repo', before=self.before,
                                                        after=self.git('rev-parse', 'HEAD'))])))
        config = {self.delivery[index][2:].replace('-', '_'): self.delivery[index + 1]
                  for index in range(0, len(self.delivery), 2)}
        (store / 'config.json').write_text(json.dumps(config))
        gate = self.cwd / 'gate.py'
        gate.write_text(gate.read_text().replace("'example' in a.private_repo", "'example/repo' in a.private_repo"))
        commands = []
        original = c.process
        def record(command, *args, **kwargs):
            commands.append(command)
            return original(command, *args, **kwargs)
        with patch.object(c, 'process', side_effect=record):
            first = learn_replay.run(store, ledger, projects=self.cwd, pairs_path=pairs)
            second = learn_replay.run(store, ledger, projects=self.cwd, pairs_path=pairs)
        self.assertEqual((first, second), (dict(proven=1, rejected=0), dict(proven=0, rejected=0)), entry.get('replay'))
        state = json.loads((store / 'learn-drafts.json').read_text())['drafts']['candidate']['replay']
        self.assertEqual(state['status'], 'proven')
        receipt = c.load_store(store)[1]['receipts'][state['proof']['receipt_id']]
        self.assertEqual([receipt[side]['status'] for side in ('before', 'after', 'reversed')], ['FAIL', 'PASS', 'FAIL'])
        self.assertFalse([command for command in commands if {'import-rule', 'deliver-rules'} & set(map(str, command))])
        self.assertEqual(self.git('status', '--porcelain', '--untracked-files=all'), '')
        self.assertFalse((store / 'deliveries.json').exists())


class PairOnlyReplayTests(unittest.TestCase):
    def test_rejected_explicit_review_pair_never_falls_back_to_history(self):
        failure = dict(id='f1', kind='review', repo='o/a', source_ref='finding', error='A review finding')
        pair = dict(repo='o/a', finding_url='finding', before='a' * 40, after='b' * 40)
        store, entry = dict(failures=dict(f1=failure)), dict(evidence=['f1'])
        for pairs in ([pair], []):
            with self.subTest(explicit=bool(pairs)), \
                    patch.object(learn_replay, 'history_candidates', side_effect=AssertionError('review history invoked')) as history:
                reasons = []
                selected = learn_replay.replay_candidates(store, entry, pairs, None, 30, reasons)
                if pairs:
                    self.assertEqual(next(selected), (failure, pair))
                    with self.assertRaisesRegex(c.ColdfixError, 'proven only with its flagged lines'):
                        incident_admission.flagged_revert(learn_replay.argparse.Namespace(), None)
                self.assertEqual(list(selected), [])
                history.assert_not_called()
                self.assertEqual(reasons, [])

    def test_a_pair_selected_for_another_run_at_the_same_commit_is_not_used(self):
        failure = dict(id='f1', repo='o/a', source_ref='https://github.com/o/a/actions/runs/11#attempt-1')
        mine = dict(repo='o/a', before='a' * 40, after='b' * 40, red_run=11)
        other = dict(repo='o/a', before='a' * 40, after='c' * 40, red_run=111)
        legacy = dict(repo='o/a', before='a' * 40, after='d' * 40)
        with patch.object(learn_replay.flakiness, 'metadata', return_value=dict(head_sha='a' * 40)):
            found = [pair for _, pair in learn_replay.candidates(dict(failures=dict(f1=failure)),
                                                                 dict(evidence=['f1']), [mine, other, legacy])]
        self.assertEqual(found, [mine, legacy])

    def test_pair_only_replay_never_tries_descendant_commits(self):
        failure, pair = dict(id='f1'), dict(repo='o/a', before='a' * 40, after='b' * 40)
        store, entry = dict(failures=dict(f1=failure)), dict(evidence=['f1'])
        descendant = dict(repo='o/a', before='a' * 40, after='c' * 40)
        with patch.object(learn_replay, 'candidates', return_value=[(failure, pair)]), \
                patch.object(learn_replay, 'history_candidates', return_value=iter([descendant])):
            both = list(learn_replay.replay_candidates(store, entry, [], None, 1, []))
            pairs_only = list(learn_replay.replay_candidates(store, entry, [], None, 1, [], history=False))
        self.assertEqual(both, [(failure, pair), (failure, descendant)])
        self.assertEqual(pairs_only, [(failure, pair)])



class ReviewPairReplayTests(unittest.TestCase):
    def test_a_review_finding_is_proven_only_by_a_changed_test_that_exercises_its_flagged_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = root / 'store'
            store.mkdir(mode=0o700)
            repo = root / 'project'
            repo.mkdir()
            def git(*args):
                return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()
            git('init', '-q')
            git('config', 'user.name', 'Fixture')
            git('config', 'user.email', 'fixture@localhost')
            git('remote', 'add', 'origin', 'https://github.com/example/project.git')
            (repo / 'value.py').write_text("VALUE = -1\nA = 1\nLABEL = ''\n")
            git('add', '.')
            git('commit', '-qm', 'Add the value and label')
            before = git('rev-parse', 'HEAD')
            (repo / 'value.py').write_text("VALUE = 1\nA = 1\nLABEL = 'total'\n")
            (repo / 'tests').mkdir()
            (repo / 'tests/test_label.py').write_text('import unittest\nfrom value import LABEL\n'
                                                      'class LabelTests(unittest.TestCase):\n'
                                                      '    def test_a_label_is_named(self):\n'
                                                      '        self.assertTrue(LABEL)\n')
            (repo / 'tests/test_value.py').write_text('import unittest\nfrom value import VALUE\n'
                                                      'class ValueTests(unittest.TestCase):\n'
                                                      '    def test_value_is_positive(self):\n'
                                                      '        self.assertGreater(VALUE, 0)\n')
            git('add', '.')
            git('commit', '-qm', 'Keep the value positive and name the label')
            after = git('rev-parse', 'HEAD')
            ref = 'https://github.com/example/project/pull/1#discussion_r1'
            failure = c.event_record(dict(kind='review', command='PR review finding', repo='example/project',
                error='**Keep the value positive**\n\nA negative value breaks the total.', agent='codex-review',
                source_ref=ref, occurred='2026-11-05T00:00:00Z'), 'github')
            c.save(store, 'failures', failure)
            entry = dict(day=datetime.now(timezone.utc).date().isoformat(), verdict='PASS', evidence=[failure['id']],
                         headline='Keep the value positive',
                         draft=dict(kind='test', title='Keep values positive', rule='Keep every value positive.'))
            ledger = dict(drafts={'candidate': entry}, runs=[])
            (store / 'learn-drafts.json').write_text(json.dumps(ledger))
            pairs = root / 'pairs.json'
            pairs.write_text(json.dumps(dict(results=[dict(repo='example/project', finding_url=ref, before=before,
                                                           after=after, path='value.py', start_line=1, end_line=1)])))
            with patch.object(replay_isolation, 'replay_tree', c.replay_tree), \
                    patch.object(learn_replay, 'REPLAY_SECONDS', 400), \
                    patch.object(learn_replay.incident_admission, 'admit', wraps=learn_replay.incident_admission.admit) as admit:
                result = learn_replay.run(store, ledger, projects=root, pairs_path=pairs)
            self.assertEqual(result['proven'], 1, str(entry.get('replay')))
            self.assertTrue(admit.call_args_list)
            self.assertTrue(all(call.args[0].timeout <= 100 for call in admit.call_args_list))
            [lesson] = c.latest_lessons(c.load_store(store)[1]).values()
            self.assertEqual(lesson['admission']['diagnostic'], 'FAIL: test_value_is_positive')
            self.assertIn('incident not admitted; flagged lines reverted PASS; expected FAIL',
                          [attempt['reason'] for attempt in entry['replay_attempts']])

    def proven_review_lesson(self, root):
        store = root / 'store'
        store.mkdir(mode=0o700)
        repo = root / 'project'
        repo.mkdir()
        def git(*args):
            return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()
        git('init', '-q')
        git('config', 'user.name', 'Fixture')
        git('config', 'user.email', 'fixture@localhost')
        git('remote', 'add', 'origin', 'https://github.com/example/project.git')
        (repo / 'value.py').write_text("VALUE = -1\nA = 1\nLABEL = ''\n")
        git('add', '.')
        git('commit', '-qm', 'Add the value and label')
        before = git('rev-parse', 'HEAD')
        (repo / 'value.py').write_text("VALUE = 1\nA = 1\nLABEL = 'total'\n")
        (repo / 'tests').mkdir()
        (repo / 'tests/test_value.py').write_text('import unittest\nfrom value import VALUE\n'
                                                  'class ValueTests(unittest.TestCase):\n'
                                                  '    def test_value_is_positive(self):\n'
                                                  '        self.assertGreater(VALUE, 0)\n')
        git('add', '.')
        git('commit', '-qm', 'Keep the value positive and name the label')
        after = git('rev-parse', 'HEAD')
        ref = 'https://github.com/example/project/pull/1#discussion_r1'
        failure = c.event_record(dict(kind='review', command='PR review finding', repo='example/project',
            error='**Keep the value positive**\n\nA negative value breaks the total.', agent='codex-review',
            source_ref=ref, occurred='2026-11-05T00:00:00Z'), 'github')
        c.save(store, 'failures', failure)
        entry = dict(day=datetime.now(timezone.utc).date().isoformat(), verdict='PASS', evidence=[failure['id']],
                     headline='Keep the value positive',
                     draft=dict(kind='test', title='Keep values positive', rule='Keep every value positive.'))
        ledger = dict(drafts={'candidate': entry}, runs=[])
        (store / 'learn-drafts.json').write_text(json.dumps(ledger))
        pairs = root / 'pairs.json'
        pairs.write_text(json.dumps(dict(results=[dict(repo='example/project', finding_url=ref, before=before,
                                                       after=after, path='value.py', start_line=1, end_line=1)])))
        self.collect(store, ledger, root, pairs)
        self.assertEqual(entry['replay']['status'], 'proven', str(entry.get('replay')))
        [lesson] = c.latest_lessons(c.load_store(store)[1]).values()
        self.assertEqual(entry['replay'].pop('flagged_lines', None), lesson['id'])
        return store, ledger, entry, lesson, pairs

    def collect(self, store, ledger, root, pairs, seconds=400):
        with patch.object(replay_isolation, 'replay_tree', c.replay_tree), \
                patch.object(learn_replay, 'REPLAY_SECONDS', seconds):
            return learn_replay.run(store, ledger, projects=root, pairs_path=pairs)

    def test_a_proven_review_lesson_that_exercises_its_flagged_lines_is_marked_revalidated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, ledger, entry, lesson, pairs = self.proven_review_lesson(root)
            rows = json.loads(pairs.read_text())
            rows['results'].insert(0, dict(rows['results'][0], end_line=None))
            pairs.write_text(json.dumps(rows))
            result = self.collect(store, ledger, root, pairs)
            self.assertEqual(result['revalidation'], dict(revalidated=1, invalidated=0, deferred=0, cannot_rerun=0,
                                                          reasons={}))
            self.assertEqual(json.loads((store / 'learn-drafts.json').read_text())['drafts']['candidate']['replay']
                             ['flagged_lines'], lesson['id'])
            self.assertEqual(c.read_invalidations(store), {})
            self.assertNotIn('revalidation', self.collect(store, ledger, root, pairs))

    def test_a_proven_review_lesson_whose_flagged_lines_are_not_exercised_is_invalidated_with_its_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, ledger, entry, lesson, pairs = self.proven_review_lesson(root)
            rows = json.loads(pairs.read_text())
            rows['results'][0].update(start_line=3, end_line=3)
            pairs.write_text(json.dumps(rows))
            result = self.collect(store, ledger, root, pairs)
            reason = 'incident not admitted; flagged lines reverted PASS; expected FAIL'
            self.assertEqual(result['revalidation'], dict(revalidated=0, invalidated=1, deferred=0, cannot_rerun=0,
                                                          reasons={reason: 1}))
            self.assertEqual(c.read_invalidations(store), {lesson['lesson_id']: lesson['id']})
            self.assertEqual(entry['replay'], dict(status='rejected', reason='flagged-lines re-validation: ' + reason))
            self.assertEqual(entry['flagged_lines_invalidated']['reason'], reason)
            entry.pop('replay_settled')
            entry.pop('replay_attempts', None)
            self.collect(store, ledger, root, pairs)
            self.assertEqual(c.read_invalidations(store), {lesson['lesson_id']: lesson['id']})
            self.assertEqual(entry['replay'], dict(status='rejected', reason=
                'an invalidated review lesson is re-proven only under the flagged-lines rule'))
            self.assertEqual(list(c.latest_lessons(c.load_store(store)[1])), [lesson['lesson_id']])
            self.assertEqual(c.latest_lessons(c.load_store(store)[1])[lesson['lesson_id']]['id'], lesson['id'])

    def test_a_proven_review_lesson_that_cannot_be_rerun_is_counted_and_kept_without_the_mark(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, ledger, entry, lesson, pairs = self.proven_review_lesson(root)
            recorded = pairs.read_text()
            rows = json.loads(recorded)
            rows['results'][0]['end_line'] = None
            pairs.write_text(json.dumps(rows))
            def blocked():
                result = self.collect(store, ledger, root, pairs)
                reason = entry['replay'].pop('flagged_lines_blocked')['reason']
                self.assertEqual(result['revalidation'], dict(revalidated=0, invalidated=0, deferred=0, cannot_rerun=1,
                                                              reasons={reason: 1}))
                self.assertEqual(entry['replay']['status'], 'proven')
                self.assertNotIn('flagged_lines', entry['replay'])
                self.assertEqual(c.read_invalidations(store), {})
                return reason
            self.assertEqual(blocked(), "no reviewed pair records the finding's flagged lines")
            pairs.write_text(recorded)
            with patch.object(learn_replay.incident_admission, 'require_flagged_lines_exercised',
                              side_effect=c.ColdfixError('sandbox unavailable: fixture')):
                self.assertEqual(blocked(), 'sandbox unavailable: fixture')
            for change, reason in ((dict(path='missing.py'), 'local privacy Git operation failed'),
                                   (dict(path='tests/test_value.py'), 'local privacy Git operation failed'),
                                   (dict(start_line=2, end_line=2), "the fix does not change the finding's flagged lines")):
                rows = json.loads(recorded)
                rows['results'][0].update(change)
                pairs.write_text(json.dumps(rows))
                self.assertEqual(blocked(), reason)
            pairs.write_text(recorded)
            git = c.privacy_git
            def diff_fails(repo, *args, **kwargs):
                if args[0] == 'diff':
                    raise c.ColdfixError('git diff failed: fixture')
                return git(repo, *args, **kwargs)
            with patch.object(c, 'privacy_git', side_effect=diff_fails):
                self.assertEqual(blocked(), 'git diff failed: fixture')
            project = ['git', '-C', str(root / 'project'), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost']
            for args in (('checkout', '-q', '--orphan', 'rewritten'), ('commit', '-qm', 'Rewritten history'),
                         ('for-each-ref', '--format=delete %(refname)', 'refs/heads/', 'refs/tags/')):
                output = subprocess.run([*project, *args], check=True, capture_output=True, text=True).stdout
            subprocess.run([*project, 'update-ref', '--stdin'], check=True, text=True,
                           input=''.join(line + '\n' for line in output.splitlines() if not line.endswith('/rewritten')))
            subprocess.run([*project, 'reflog', 'expire', '--expire=now', '--all'], check=True)
            subprocess.run([*project, 'gc', '-q', '--prune=now'], check=True)
            self.assertEqual(blocked(), 'source repository checkout unavailable')
            shutil.rmtree(root / 'project')
            self.assertEqual(blocked(), 'source repository checkout unavailable')
            entry['replay']['flagged_lines_blocked'] = dict(day=learn_replay.utc_day(), reason='fixture')
            self.assertNotIn('revalidation', self.collect(store, ledger, root, pairs))

    def test_a_proven_review_lesson_that_does_not_fit_the_deadline_is_deferred_not_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, ledger, entry, lesson, pairs = self.proven_review_lesson(root)
            result = self.collect(store, ledger, root, pairs, seconds=0)
            self.assertEqual(result['revalidation']['deferred'], 1)
            def cut_off(*args, **kwargs):
                time.sleep(1.2)
                raise c.ColdfixError('replay budget exhausted')
            with patch.object(learn_replay.incident_admission, 'require_flagged_lines_exercised', side_effect=cut_off):
                result = self.collect(store, ledger, root, pairs, seconds=2)
            self.assertEqual(result['revalidation'], dict(revalidated=0, invalidated=0, deferred=1, cannot_rerun=0,
                                                          reasons={}))
            self.assertIn('flagged_lines_tried', entry['replay'])
            self.assertEqual(entry['replay']['flagged_lines_preflight'], lesson['id'])
            self.assertEqual(entry['replay']['status'], 'proven')
            self.assertNotIn('flagged_lines', entry['replay'])
            self.assertEqual(c.read_invalidations(store), {})
            self.assertEqual(self.collect(store, ledger, root, pairs)['revalidation']['revalidated'], 1)

    def test_a_preflight_that_spends_the_deadline_does_not_starve_a_checked_lesson_tried_earlier(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, ledger, entry, lesson, pairs = self.proven_review_lesson(root)
            entry['replay'].update(flagged_lines_preflight=lesson['id'], flagged_lines_tried=1)
            other = dict(replay=dict(status='proven', flagged_lines_tried=2))
            targets = learn_replay.revalidation_targets
            def with_other(*args):
                for row in targets(*args):
                    yield row
                    yield other, dict(row[1], id='other-revision'), dict(row[2], other=True)
            source = learn_replay.source_repo
            def spend(failure, projects, **kwargs):
                if failure.get('other'):
                    time.sleep(max(0, projects.deadline - time.monotonic()))
                    raise c.ColdfixError('replay budget exhausted')
                return source(failure, projects, **kwargs)
            with patch.object(learn_replay, 'revalidation_targets', with_other), \
                    patch.object(learn_replay, 'source_repo', side_effect=spend):
                result = self.collect(store, ledger, root, pairs, seconds=20)
            self.assertEqual(result['revalidation'], dict(revalidated=1, invalidated=0, deferred=1, cannot_rerun=0,
                                                          reasons={}))
            self.assertEqual(entry['replay']['flagged_lines'], lesson['id'])

    def test_a_review_finding_is_proven_by_the_test_its_fix_added(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = root / 'store'
            store.mkdir(mode=0o700)
            repo = root / 'project'
            repo.mkdir()
            def git(*args):
                return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()
            git('init', '-q')
            git('config', 'user.name', 'Fixture')
            git('config', 'user.email', 'fixture@example.invalid')
            git('remote', 'add', 'origin', 'https://github.com/example/project.git')
            (repo / 'value.py').write_text('VALUE = -1\n')
            git('add', '.')
            git('commit', '-qm', 'Add the value')
            before = git('rev-parse', 'HEAD')
            (repo / 'value.py').write_text('VALUE = 1\n')
            (repo / 'tests').mkdir()
            (repo / 'tests/test_value.py').write_text('import unittest\nfrom value import VALUE\n'
                                                      'class ValueTests(unittest.TestCase):\n'
                                                      '    def test_value_is_positive(self):\n'
                                                      '        self.assertGreater(VALUE, 0)\n')
            git('add', '.')
            git('commit', '-qm', 'Keep the value positive')
            after = git('rev-parse', 'HEAD')
            ref = 'https://github.com/example/project/pull/1#discussion_r1'
            failure = c.event_record(dict(kind='review', command='PR review finding', repo='example/project',
                error='**Keep the value positive**\n\nA negative value breaks the total.', agent='codex-review',
                source_ref=ref, occurred='2026-11-05T00:00:00Z'), 'github')
            c.save(store, 'failures', failure)
            entry = dict(day=datetime.now(timezone.utc).date().isoformat(), verdict='PASS', evidence=[failure['id']],
                         headline='Keep the value positive',
                         draft=dict(kind='test', title='Keep values positive', rule='Keep every value positive.'))
            ledger = dict(drafts={'candidate': entry}, runs=[])
            (store / 'learn-drafts.json').write_text(json.dumps(ledger))
            pairs = root / 'pairs.json'
            pairs.write_text(json.dumps(dict(results=[dict(repo='example/project', finding_url=ref,
                                                           before=before, after=after, path='value.py',
                                                           start_line=1, end_line=1)])))
            with patch.object(replay_isolation, 'replay_tree', c.replay_tree):
                result = learn_replay.run(store, ledger, projects=root, pairs_path=pairs)
            self.assertEqual(result, dict(proven=1, rejected=0), str(entry.get('replay')))
            [lesson] = c.latest_lessons(c.load_store(store)[1]).values()
            self.assertEqual((lesson['evidence'], lesson['admission']['diagnostic']),
                             ([failure['id']], 'FAIL: test_value_is_positive'))

    def review_without_pair(self, root, commits=None, merged_at='2026-11-07T00:00:00Z'):
        store = root / 'store'
        store.mkdir(mode=0o700)
        repo = root / 'project'
        repo.mkdir()
        def git(*args):
            return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()
        git('init', '-q')
        git('config', 'user.name', 'Fixture')
        git('config', 'user.email', 'fixture@example.invalid')
        git('remote', 'add', 'origin', 'https://github.com/example/project.git')
        (repo / 'value.py').write_text('VALUE = -1\n')
        git('add', '.')
        git('commit', '-qm', 'Add the value')
        before = git('rev-parse', 'HEAD')
        (repo / 'value.py').write_text('VALUE = 1\n')
        (repo / 'tests').mkdir()
        (repo / 'tests/test_value.py').write_text('import unittest\nfrom value import VALUE\n'
                                                  'class ValueTests(unittest.TestCase):\n'
                                                  '    def test_value_is_positive(self):\n'
                                                  '        self.assertGreater(VALUE, 0)\n')
        git('add', '.')
        git('commit', '-qm', 'Keep the value positive')
        after = git('rev-parse', 'HEAD')
        ref = 'https://github.com/example/project/pull/1#discussion_r1'
        failure = c.event_record(dict(kind='review', command='PR review finding', repo='example/project',
            error='**Keep the value positive**\n\nA negative value breaks the total.', agent='codex-review',
            source_ref=ref, occurred='2026-11-05T00:00:00Z'), 'github')
        c.save(store, 'failures', failure)
        entry = dict(day=datetime.now(timezone.utc).date().isoformat(), verdict='PASS', evidence=[failure['id']],
                     headline='Keep the value positive',
                     draft=dict(kind='test', title='Keep values positive', rule='Keep every value positive.'))
        ledger = dict(drafts={'candidate': entry}, runs=[])
        (store / 'learn-drafts.json').write_text(json.dumps(ledger))
        pairs = root / 'pairs.json'
        pairs.write_text(json.dumps(dict(results=[])))
        patch_text = '@@ -1 +1 @@\n-VALUE = -1\n+VALUE = 1'
        responses = {
            'pulls/comments/1': dict(id=1, user=dict(login='chatgpt-codex-connector[bot]'), html_url=ref,
                                     body='<!-- codex-code-review-finding:v1 -->\n![P1 Badge](https://img.shields.io/badge/P1-orange)',
                                     original_commit_id=before, pull_request_review_id=5, side='RIGHT',
                                     original_line=1, path='value.py', created_at='2026-11-05T00:00:00Z'),
            'pulls/1': dict(state='closed', merged_at=merged_at),
            'pulls/1/reviews/5': dict(user=dict(login='chatgpt-codex-connector[bot]'), commit_id=before,
                                      body=f'Reviewed commit: `{before[:10]}`'),
            'pulls/1/commits?per_page=100&page=1': [dict(sha=sha) for sha in
                                                    (commits(before, after) if commits else (before, after))],
            f'commits/{after}?per_page=100&page=1': dict(sha=after, parents=[dict(sha=before)],
                commit=dict(committer=dict(date='2026-11-06T00:00:00Z')),
                files=[dict(filename='value.py', status='modified', patch=patch_text)]),
            f'compare/{before}...{after}': dict(files=[dict(filename='value.py', patch=patch_text)]),
        }
        calls = []
        class GitHub(ci_fix_learning.GitHub):
            def api(self, endpoint):
                self.requests += 1
                calls.append(endpoint)
                value = responses[endpoint]
                if isinstance(value, Exception):
                    raise value
                return value
        return store, ledger, entry, failure, pairs, responses, calls, GitHub

    def replay_review(self, root, store, ledger, pairs, github):
        with patch.object(replay_isolation, 'replay_tree', c.replay_tree), \
                patch.object(learn_replay.ci_fix_learning, 'GitHub', github):
            return learn_replay.run(store, ledger, projects=root, pairs_path=pairs)

    def test_a_review_finding_missing_from_the_pair_report_is_paired_from_github_and_proven(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, ledger, entry, failure, pairs, responses, calls, github = self.review_without_pair(root)
            result = self.replay_review(root, store, ledger, pairs, github)
            self.assertEqual(result, dict(proven=1, rejected=0, github_calls=6), str(entry.get('replay')))
            stored = json.loads((store / learn_replay.REVIEW_PAIRS).read_text())
            self.assertEqual((stored[failure['id']]['finding_url'], stored[failure['id']]['after'] is not None),
                             (failure['source_ref'], True))

    def test_a_review_finding_never_fixed_is_retired_as_no_fix_and_not_asked_again(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, ledger, entry, failure, pairs, responses, calls, github = self.review_without_pair(
                root, commits=lambda before, after: (before,))
            self.assertEqual(self.replay_review(root, store, ledger, pairs, github),
                             dict(proven=0, rejected=0, no_fix=1, github_calls=4))
            self.assertEqual(entry['replay'], dict(status='rejected', reason='no fix exists: NO_LATER_COMMIT'))
            calls.clear()
            entry.pop('replay_settled', None)
            self.assertEqual(self.replay_review(root, store, ledger, pairs, github),
                             dict(proven=0, rejected=0, no_fix=1))
            self.assertEqual(calls, [])

    def replay_proposed_fix(self, root, fixed_source, screen=None):
        import learn_daily
        import contextlib
        store, ledger, entry, failure, pairs, responses, calls, github = self.review_without_pair(
            root, commits=lambda before, after: (before,))
        test = ('import unittest\nfrom value import VALUE\nclass ProofTests(unittest.TestCase):\n'
                '    def test_value_is_positive(self):\n        self.assertGreater(VALUE, 0)\n')
        with patch.object(learn_daily, 'primaries', return_value=[dict(model='claude-haiku-5-5')]), \
                patch.object(learn_daily, 'ask', return_value=dict(source=fixed_source)) as ask, \
                patch.object(learn_replay, 'write_test', return_value=(test, 'claude-haiku-5-5')), \
                (patch.object(learn_replay.c, 'privacy_counts', return_value=screen) if screen
                 else contextlib.nullcontext()):
            first = self.replay_review(root, store, ledger, pairs, github)
            entry.pop('replay_settled', None)
            second = self.replay_review(root, store, ledger, pairs, github)
        return store, entry, first, second, ask

    def test_a_never_fixed_review_finding_gets_a_proposed_fix_admitted_only_by_red_green_reversed(self):
        with tempfile.TemporaryDirectory() as directory:
            store, entry, first, second, ask = self.replay_proposed_fix(Path(directory), 'VALUE = 1\n')
            self.assertEqual(first, dict(proven=1, rejected=0, github_calls=4), str(entry.get('replay')))
            self.assertEqual((entry['replay']['status'], entry['proposed_fix']['status']), ('proven', 'proven'))
            self.assertIn('VALUE = -1', ask.call_args.args[1])
            _, saved = c.load_store(store)
            [lesson] = c.latest_lessons(saved).values()
            receipt = saved['receipts'][lesson['receipt']]
            self.assertEqual([receipt[side]['status'] for side in ('before', 'after', 'reversed')], ['FAIL', 'PASS', 'FAIL'])
            self.assertEqual(lesson['verification'], 'proven')
            after = receipt['after']['commit']
            pinned = subprocess.check_output(['git', '-C', str(Path(directory) / 'project'), 'rev-parse',
                                              'refs/coldfix/' + after], text=True).strip()
            self.assertEqual(pinned, after)
            self.assertEqual((second, ask.call_count), (dict(proven=0, rejected=0), 1))

    def test_a_wrong_proposed_fix_is_rejected_by_admission_and_not_asked_again_on_the_same_code(self):
        with tempfile.TemporaryDirectory() as directory:
            store, entry, first, second, ask = self.replay_proposed_fix(Path(directory), 'VALUE = -2\n')
            self.assertEqual(first, dict(proven=0, rejected=0, no_fix=1, github_calls=4))
            self.assertEqual(entry['replay'], dict(status='rejected', reason='no fix exists: NO_LATER_COMMIT'))
            self.assertEqual(entry['proposed_fix']['status'], 'rejected')
            self.assertIn('green FAIL', entry['proposed_fix']['reason'])
            self.assertNotIn('proven', [lesson['verification']
                                        for lesson in c.latest_lessons(c.load_store(store)[1]).values()])
            self.assertEqual((second, ask.call_count), (dict(proven=0, rejected=0, no_fix=1), 1))

    def test_a_proposal_cut_off_by_the_deadline_resumes_from_its_stored_patch_without_asking_again(self):
        import learn_daily
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, ledger, entry, failure, pairs, responses, calls, github = self.review_without_pair(
                root, commits=lambda before, after: (before,))
            late = []
            def reply(*args):
                late.append(True)
                return dict(source='VALUE = 1\n')
            test = ('import unittest\nfrom value import VALUE\nclass ProofTests(unittest.TestCase):\n'
                    '    def test_value_is_positive(self):\n        self.assertGreater(VALUE, 0)\n')
            with patch.object(learn_daily, 'primaries', return_value=[dict(model='claude-haiku-5-5')]), \
                    patch.object(learn_daily, 'ask', side_effect=reply) as ask, \
                    patch.object(learn_replay, 'write_test', return_value=(test, 'claude-haiku-5-5')):
                with patch.object(learn_replay, 'expired', side_effect=lambda deadline: bool(late)):
                    self.replay_review(root, store, ledger, pairs, github)
                self.assertEqual(entry['proposed_fix']['status'], 'deferred')
                entry.pop('replay_settled', None)
                result = self.replay_review(root, store, ledger, pairs, github)
            self.assertEqual((result['proven'], ask.call_count, entry['proposed_fix']['status']), (1, 1, 'proven'))

    def test_a_proposal_model_call_bounded_and_cut_off_by_the_deadline_is_not_rejected_and_is_asked_again(self):
        import learn_daily
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, ledger, entry, failure, pairs, responses, calls, github = self.review_without_pair(
                root, commits=lambda before, after: (before,))
            cut = []
            def reply(model, prompt, shape, work, deadline=None):
                if not cut:
                    cut.append(deadline)
                    raise subprocess.TimeoutExpired('model', 1)
                return dict(source='VALUE = 1\n')
            test = ('import unittest\nfrom value import VALUE\nclass ProofTests(unittest.TestCase):\n'
                    '    def test_value_is_positive(self):\n        self.assertGreater(VALUE, 0)\n')
            with patch.object(learn_daily, 'primaries', return_value=[dict(model='claude-haiku-5-5')]), \
                    patch.object(learn_daily, 'ask', side_effect=reply) as ask, \
                    patch.object(learn_replay, 'write_test', return_value=(test, 'claude-haiku-5-5')):
                with patch.object(learn_replay, 'expired', side_effect=lambda deadline: bool(cut)):
                    self.replay_review(root, store, ledger, pairs, github)
                self.assertIsNotNone(cut[0])
                self.assertNotIn('proposed_fix', entry)
                entry.pop('replay_settled', None)
                result = self.replay_review(root, store, ledger, pairs, github)
            self.assertEqual((result['proven'], ask.call_count, entry['proposed_fix']['status']), (1, 2, 'proven'))

    def replay_spilled_fix(self, root, proposed_source, previous=None):
        import learn_daily
        import oss_replay
        store, ledger, entry, failure, pairs, responses, calls, github = self.review_without_pair(root)
        repo = root / 'project'
        def git(*args):
            return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()
        test_file = (repo / 'tests/test_value.py').read_text()
        git('rm', '-rq', 'tests')
        (repo / 'value.py').write_text('VALUE = -1\nA = 1\n')
        git('commit', '-qam', 'Add a neighbour')
        before = git('rev-parse', 'HEAD')
        (repo / 'tests').mkdir()
        (repo / 'tests/test_value.py').write_text(test_file)
        (repo / 'value.py').write_text('VALUE = 1\nA = 2\n')
        git('add', '.')
        git('commit', '-qm', 'Keep the value positive and bump the neighbour')
        after = git('rev-parse', 'HEAD')
        pairs.write_text(json.dumps(dict(results=[dict(repo='example/project', finding_url=failure['source_ref'],
            before=before, after=after, path='value.py', start_line=1, end_line=1)])))
        if previous:
            entry['proposed_fix'] = dict(previous, failure=failure['id'], finding=[before, 'value.py', 1, 1])
            (store / 'learn-drafts.json').write_text(json.dumps(ledger))
        test = ('import unittest\nfrom value import VALUE\nclass ProofTests(unittest.TestCase):\n'
                '    def test_value_is_positive(self):\n        self.assertGreater(VALUE, 0)\n')
        with patch.object(learn_daily, 'primaries', return_value=[dict(model='claude-haiku-5-5')]), \
                patch.object(learn_daily, 'ask', return_value=dict(source=proposed_source)), \
                patch.object(learn_replay, 'write_test', return_value=(test, 'claude-haiku-5-5')), \
                patch.object(oss_replay, 'commit_fix', wraps=oss_replay.commit_fix) as committed:
            result = self.collect(store, ledger, root, pairs)
        return store, entry, result, committed

    def test_a_real_fix_that_spills_past_the_flagged_lines_is_proven_by_a_proposed_fix_confined_to_them(self):
        with tempfile.TemporaryDirectory() as directory:
            store, entry, result, committed = self.replay_spilled_fix(Path(directory), 'VALUE = 1\nA = 1\n')
            self.assertIn(SPILLED, [attempt['reason'] for attempt in entry['replay_attempts']])
            self.assertEqual((result['proven'], entry['replay']['status'], entry.get('proposed_fix', {}).get('status')),
                             (1, 'proven', 'proven'), str(entry.get('proposed_fix')))
            [lesson] = c.latest_lessons(c.load_store(store)[1]).values()
            self.assertEqual(lesson['verification'], 'proven')

    def test_a_proposed_fix_that_edits_outside_the_flagged_lines_is_refused_before_any_commit_or_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            store, entry, result, committed = self.replay_spilled_fix(Path(directory), 'VALUE = 1\nA = 2\n')
            self.assertEqual((entry['replay']['status'], set(entry['replay']['reason'].split('; '))), ('rejected', {SPILLED}))
            self.assertEqual(entry.get('proposed_fix', {}).get('reason'),
                             "the proposed fix edits lines outside the finding's flagged lines")
            committed.assert_not_called()
            self.assertEqual(c.latest_lessons(c.load_store(store)[1]), {})

    def test_a_resumed_deferred_fix_that_edits_outside_the_flagged_lines_is_refused_before_any_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            spilled = learn_replay.proposed_patch('value.py', 'VALUE = -1\nA = 1\n', 'VALUE = 1\nA = 2\n')
            store, entry, result, committed = self.replay_spilled_fix(
                Path(directory), 'VALUE = 1\nA = 1\n',
                dict(status='deferred', patch=spilled, revision=learn_replay.code_revision()))
            self.assertEqual((entry['replay']['status'], entry['proposed_fix'].get('reason')),
                             ('rejected', "the proposed fix edits lines outside the finding's flagged lines"))
            committed.assert_not_called()

    def test_the_proposed_fix_check_measures_hunks_the_way_admission_does(self):
        original = 'A = 1\nB = 2\nC = 3\n'
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(hasattr(learn_replay, 'require_flagged_only'))
            root = Path(directory)
            repo = root / 'repo'
            (repo / 'pkg').mkdir(parents=True)
            (repo / 'pkg/value.py').write_text(original)
            (repo / 'pkg/opaque.py').write_text(original)
            (repo / '.gitattributes').write_text('pkg/opaque.py -diff\n')
            (repo / '.gitmodules').write_text('[submodule "sub"]\n\tpath = sub\n\turl = ./sub\n\tignore = all\n')
            def git(*args):
                return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()
            git('init', '-q')
            git('add', '.')
            git('update-index', '--add', '--cacheinfo', '160000,' + 'a' * 40 + ',sub')
            git('-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '-qm', 'base')
            before = git('rev-parse', 'HEAD')
            def check(source, start, end, path='pkg/value.py'):
                learn_replay.require_flagged_only(root, repo, before, path,
                                                  learn_replay.proposed_patch(path, original, source), start, end, 30)
            check('A = 1\nB = 9\nC = 3\n', 2, 2)
            check('A = 1\nC = 3\n', 2, 2)
            for source, start, end, path in (('A = 1\nB = 9\nC = 4\n', 2, 2, 'pkg/value.py'),
                                             ('A = 1\nB = 9\nX = 0\nC = 3\n', 2, 2, 'pkg/value.py'),
                                             ('A = 1\nX = 0\nB = 2\nC = 3\n', 2, 2, 'pkg/value.py'),
                                             ('A = 0\nB = 2\nC = 3\n', 2, 3, 'pkg/value.py'),
                                             ('A = 1\nB = 9\nC = 3\n', 2, 2, 'pkg/opaque.py')):
                with self.subTest(source=source, path=path), self.assertRaisesRegex(c.ColdfixError, 'outside the finding'):
                    check(source, start, end, path)
            two = learn_replay.proposed_patch('pkg/value.py', original, 'A = 1\nB = 9\nC = 3\n') + \
                learn_replay.proposed_patch('pkg/opaque.py', original, 'A = 0\nB = 2\nC = 3\n')
            with self.assertRaisesRegex(c.ColdfixError, 'outside the finding'):
                learn_replay.require_flagged_only(root, repo, before, 'pkg/value.py', two, 2, 2, 30)
            submodule = (learn_replay.proposed_patch('pkg/value.py', original, 'A = 1\nB = 9\nC = 3\n') +
                         'diff --git a/sub b/sub\nindex ' + 'a' * 40 + '..' + 'b' * 40 + ' 160000\n--- a/sub\n+++ b/sub\n'
                         '@@ -1 +1 @@\n-Subproject commit ' + 'a' * 40 + '\n+Subproject commit ' + 'b' * 40 + '\n')
            with self.assertRaisesRegex(c.ColdfixError, 'outside the finding'):
                learn_replay.require_flagged_only(root, repo, before, 'pkg/value.py', submodule, 2, 2, 30)
            self.assertEqual(git('rev-list', '--all', '--count'), '1')

    def test_only_spill_rejections_of_the_real_or_generated_test_seek_a_proposed_fix(self):
        self.assertTrue(hasattr(learn_replay, 'spill_only'))
        self.assertTrue(learn_replay.spill_only([SPILLED, SPILLED]))
        self.assertTrue(learn_replay.spill_only(['no changed unittest matches the recorded failure',
                                                 'generated test: ' + SPILLED]))
        self.assertFalse(learn_replay.spill_only(['no changed unittest matches the recorded failure']))
        self.assertFalse(learn_replay.spill_only([SPILLED, 'a review finding is proven only with its flagged lines']))

    def test_a_long_file_sends_only_the_flagged_lines_and_their_context_and_splices_the_reply_back(self):
        original = ''.join(f'LINE_{number} = {number}\n' for number in range(1, 501))
        head, excerpt, tail = learn_replay.flagged_window(original, 250, 252)
        self.assertEqual(head + excerpt + tail, original)
        self.assertTrue(excerpt.startswith('LINE_190 = 190\n') and excerpt.endswith('LINE_312 = 312\n'))
        self.assertNotIn('LINE_189 =', excerpt)
        self.assertEqual(learn_replay.flagged_window(original, 1, 1)[0], '')
        self.assertEqual(learn_replay.flagged_window(original, 500, 500)[2], '')
        for start, end in ((0, 1), (3, 2), (900, 900), (500, 501), (None, None)):
            with self.subTest(start=start, end=end), self.assertRaises(c.ColdfixError):
                learn_replay.flagged_window(original, start, end)

    def test_a_proposed_diff_that_fails_the_privacy_screen_never_reaches_the_test_model_even_when_resumed(self):
        import learn_daily
        real = learn_replay.c.privacy_counts
        def screen(value, *args, **kwargs):
            return {'credential': 1} if '--- a/' in value else real(value, *args, **kwargs)
        for deferred in (False, True):
            with self.subTest(deferred=deferred), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                store, ledger, entry, failure, pairs, responses, calls, github = self.review_without_pair(
                    root, commits=lambda before, after: (before,))
                with patch.object(learn_daily, 'primaries', return_value=[dict(model='claude-haiku-5-5')]), \
                        patch.object(learn_daily, 'ask', return_value=dict(source='VALUE = 1\n')) as ask, \
                        patch.object(learn_replay, 'write_test') as write:
                    if deferred:
                        with patch.object(learn_replay, 'expired', side_effect=lambda deadline: ask.called):
                            self.replay_review(root, store, ledger, pairs, github)
                        self.assertEqual(entry['proposed_fix']['status'], 'deferred')
                        entry.pop('replay_settled', None)
                    with patch.object(learn_replay.c, 'privacy_counts', side_effect=screen):
                        self.replay_review(root, store, ledger, pairs, github)
                self.assertEqual(entry['proposed_fix']['reason'], 'flagged file failed the privacy screen')
                self.assertEqual((write.call_count, ask.call_count), (0, 1))

    def test_a_flagged_file_that_fails_the_privacy_screen_is_never_sent_to_the_model(self):
        with tempfile.TemporaryDirectory() as directory:
            store, entry, first, second, ask = self.replay_proposed_fix(Path(directory), 'VALUE = 1\n',
                                                                        screen={'home_path': 1})
            self.assertEqual(first, dict(proven=0, rejected=0, no_fix=1, github_calls=4))
            self.assertEqual(entry['proposed_fix']['reason'], 'flagged file failed the privacy screen')
            self.assertEqual(ask.call_count, 0)

    def test_a_review_finding_whose_reviewed_commit_left_the_pr_is_retired_as_unpairable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, ledger, entry, failure, pairs, responses, calls, github = self.review_without_pair(
                root, commits=lambda before, after: ('0' * 40, after))
            self.assertEqual(self.replay_review(root, store, ledger, pairs, github),
                             dict(proven=0, rejected=0, unpairable=1, github_calls=4))
            self.assertEqual(entry['replay'], dict(status='rejected', reason='no exact pair: REVIEWED_COMMIT_NOT_IN_PR'))

    def test_a_review_finding_is_never_retired_on_a_failed_call_an_unmerged_pr_or_a_spent_call_budget(self):
        cases = dict(failed=dict(fail='pulls/1/commits?per_page=100&page=1'), unmerged=dict(merged_at=None),
                     budget=dict(calls=3))
        for name, case in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                store, ledger, entry, failure, pairs, responses, calls, github = self.review_without_pair(
                    root, commits=lambda before, after: (before,), merged_at=case.get('merged_at', '2026-11-07T00:00:00Z'))
                if 'fail' in case:
                    responses[case['fail']] = ci_fix_learning.Skipped('read operation unavailable')
                with patch.object(learn_replay, 'PAIR_CALLS', case.get('calls', learn_replay.PAIR_CALLS)):
                    result = self.replay_review(root, store, ledger, pairs, github)
                self.assertNotIn('no_fix', result)
                self.assertNotIn('unpairable', result)
                self.assertFalse((store / learn_replay.REVIEW_PAIRS).exists())
                if name == 'budget':
                    self.assertEqual(len(calls), 3)

    def test_a_review_outcome_stored_by_another_run_before_the_lock_is_kept(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, ledger, entry, failure, pairs, responses, calls, github = self.review_without_pair(root)
            lock = learn_replay.c.lesson_lock
            stored = {failure['id']: dict(after=None, reason='NO_LATER_COMMIT')}
            def other_run_first(path):
                (store / learn_replay.REVIEW_PAIRS).write_text(json.dumps(stored))
                return lock(path)
            with patch.object(learn_replay.c, 'lesson_lock', other_run_first):
                result = self.replay_review(root, store, ledger, pairs, github)
            self.assertEqual((result, calls), (dict(proven=0, rejected=0, no_fix=1), []))
            self.assertEqual(json.loads((store / learn_replay.REVIEW_PAIRS).read_text()), stored)

    def cold_review(self, root, cited):
        store = root / 'store'
        store.mkdir(mode=0o700)
        repo = root / 'project'
        repo.mkdir()
        def git(*args):
            return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()
        git('init', '-q')
        git('config', 'user.name', 'Fixture')
        git('config', 'user.email', 'fixture@example.invalid')
        git('remote', 'add', 'origin', 'https://github.com/example/project.git')
        (repo / 'value.py').write_text("LABEL = 'a'\nOTHER = 0\nVALUE = -1\n")
        git('add', '.')
        git('commit', '-qm', 'Add the value')
        before = git('rev-parse', 'HEAD')
        (repo / 'value.py').write_text("LABEL = 'b'\nOTHER = 0\nVALUE = 1\n")
        (repo / 'tests').mkdir()
        (repo / 'tests/test_value.py').write_text('import unittest\nfrom value import VALUE\n'
                                                  'class ValueTests(unittest.TestCase):\n'
                                                  '    def test_value_is_positive(self):\n'
                                                  '        self.assertGreater(VALUE, 0)\n')
        git('add', '.')
        git('commit', '-qm', 'Keep the value positive')
        after = git('rev-parse', 'HEAD')
        failure = c.event_record(dict(kind='review', command='review', repo='example/project', agent='reviewer',
            error=c.encoded(dict(finding=f'[P1] value.py:{cited}: a negative value breaks the total.', head=before)).strip(),
            source_ref=f'https://github.com/example/project/pull/1#cold-review-{before}-{"a" * 64}',
            occurred='2026-11-05T00:00:00Z'), 'cold-review')
        c.save(store, 'failures', failure)
        entry = dict(day=datetime.now(timezone.utc).date().isoformat(), verdict='PASS', evidence=[failure['id']],
                     headline='Keep the value positive',
                     draft=dict(kind='test', title='Keep values positive', rule='Keep every value positive.'))
        ledger = dict(drafts={'candidate': entry}, runs=[])
        (store / 'learn-drafts.json').write_text(json.dumps(ledger))
        pairs = root / 'pairs.json'
        pairs.write_text(json.dumps(dict(results=[])))
        patch_text = "@@ -1 +1 @@\n-LABEL = 'a'\n+LABEL = 'b'\n@@ -3 +3 @@\n-VALUE = -1\n+VALUE = 1"
        responses = {
            'pulls/1': dict(state='closed', merged_at='2026-11-07T00:00:00Z'),
            'pulls/1/files?per_page=100&page=1': [dict(sha='1' * 40, filename='value.py'),
                                                  dict(sha='2' * 40, filename='tests/test_value.py')],
            'pulls/1/commits?per_page=100&page=1': [dict(sha=before), dict(sha=after)],
            f'commits/{after}?per_page=100&page=1': dict(sha=after, parents=[dict(sha=before)],
                commit=dict(committer=dict(date='2026-11-06T00:00:00Z')),
                files=[dict(filename='value.py', status='modified', patch=patch_text)]),
            f'compare/{before}...{after}': dict(files=[dict(filename='value.py', patch=patch_text)]),
        }
        class GitHub(ci_fix_learning.GitHub):
            def api(self, endpoint):
                self.requests += 1
                return responses[endpoint]
        return store, ledger, entry, failure, pairs, GitHub

    def test_a_cold_review_finding_is_paired_from_the_pr_commits_after_its_reviewed_head_and_proven(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, ledger, entry, failure, pairs, github = self.cold_review(root, 3)
            result = self.replay_review(root, store, ledger, pairs, github)
            self.assertEqual(result['proven'], 1, str(entry.get('replay')))
            self.assertEqual(result['cold_review'], dict(paired=1, eligible=1, duplicate=0, no_pair=0, held=0, deferred=0))
            row = json.loads((store / learn_replay.REVIEW_PAIRS).read_text())[failure['id']]
            self.assertEqual((row['path'], row['start_line'], row['end_line'], row['flagged_only']), ('value.py', 3, 3, True))

    def test_a_cold_review_finding_citing_lines_the_fix_did_not_need_is_not_provable_and_never_a_lesson(self):
        test = ('import unittest\nfrom value import VALUE\nclass ProofTests(unittest.TestCase):\n'
                '    def test_value_is_positive(self):\n        self.assertGreater(VALUE, 0)\n')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, ledger, entry, failure, pairs, github = self.cold_review(root, 1)
            with patch.object(learn_replay, 'write_test', return_value=(test, 'claude-haiku-5-5')):
                result = self.replay_review(root, store, ledger, pairs, github)
            self.assertEqual((result['proven'], result['cold_review']['eligible']), (0, 1))
            self.assertEqual(entry['replay']['status'], 'rejected')
            self.assertIn('flagged lines reverted PASS', entry['replay']['reason'])
            self.assertIn('not_provable', dict(learn_replay.reason_buckets([entry['replay']['reason']])))
            self.assertEqual(c.latest_lessons(c.load_store(store)[1]), {})

    def test_cold_review_pairing_counts_duplicates_holds_unmapped_renames_and_defers_past_the_deadline(self):
        def finding(ident, head, repo='example/project', occurred='2026-11-05T00:00:00Z'):
            return dict(id=ident, repo=repo, occurred=occurred,
                        source_ref=f'https://github.com/{repo}/pull/1#cold-review-{head}-{"a" * 64}')
        store = dict(failures={row['id']: row for row in (
            finding('a' * 32, '1' * 40), finding('b' * 32, '2' * 40, occurred='2026-11-06T00:00:00Z'),
            finding('c' * 32, '3' * 40, repo='example/old'), finding('d' * 32, '4' * 40, repo='example/old'),
            finding('e' * 32, '5' * 40, occurred='2026-11-07T00:00:00Z'))})
        def paired(failure, *args):
            head = failure['source_ref'].split('-')[-2]
            return dict(repo='example/project', finding_url=failure['source_ref'], pr=1, before=head, after='9' * 40,
                        path='value.py', start_line=3, end_line=4, reason=None, flagged_only=True)
        clock = []
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(learn_replay.moved, {'c' * 32: 'example/new'}, clear=True), \
                patch.object(learn_replay, 'cited_pair', side_effect=paired) as pair, \
                patch.object(learn_replay, 'expired', side_effect=lambda deadline: clock.append(1) or len(clock) > 3):
            reviewed = {}
            tally = learn_replay.pair_cold_reviews(store, reviewed, Path(directory) / 'pairs.json', {}, 1.0)
        self.assertEqual(tally, dict(paired=1, eligible=1, duplicate=2, no_pair=0, held=1, deferred=1))
        self.assertEqual(pair.call_count, 3)
        self.assertEqual((reviewed['b' * 32]['reason'], reviewed['b' * 32]['after']), ('DUPLICATE_FINDING', None))
        self.assertNotIn('d' * 32, reviewed)
        self.assertNotIn('e' * 32, reviewed)

    def test_a_cold_review_pair_whose_flagged_lines_check_cannot_be_read_is_deferred_not_stored_ineligible(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, ledger, entry, failure, pairs, github = self.cold_review(root, 3)
            compares = []
            class Flaky(github):
                def api(self, endpoint):
                    if endpoint.startswith('compare/'):
                        compares.append(endpoint)
                        if len(compares) > 1:
                            raise OSError('timed out')
                    return super().api(endpoint)
            result = self.replay_review(root, store, ledger, pairs, Flaky)
            self.assertEqual(len(compares), 2)
            self.assertEqual(result['cold_review'], dict(paired=0, eligible=0, duplicate=0, no_pair=0, held=0, deferred=1))
            self.assertFalse((store / learn_replay.REVIEW_PAIRS).exists())

    def test_a_cold_review_finding_deferred_past_the_pairing_slice_is_not_paired_by_the_draft_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, ledger, entry, failure, pairs, github = self.cold_review(root, 3)
            with patch.object(learn_replay, 'PAIR_SECONDS', 0):
                result = self.replay_review(root, store, ledger, pairs, github)
            self.assertEqual((result['proven'], result['cold_review']['deferred']), (0, 1))
            self.assertFalse((store / learn_replay.REVIEW_PAIRS).exists())

    def test_a_cold_review_finding_whose_slice_runs_out_between_its_github_calls_is_deferred(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, ledger, entry, failure, pairs, github = self.cold_review(root, 3)
            calls = []
            class Counted(github):
                def api(self, endpoint):
                    calls.append(endpoint)
                    return super().api(endpoint)
            with patch.object(learn_replay, 'expired', side_effect=lambda deadline: deadline is not None and bool(calls)):
                result = self.replay_review(root, store, ledger, pairs, Counted)
            self.assertEqual(calls, ['pulls/1'])
            self.assertEqual(result['cold_review'], dict(paired=0, eligible=0, duplicate=0, no_pair=0, held=0, deferred=1))
            self.assertFalse((store / learn_replay.REVIEW_PAIRS).exists())

    def test_a_draft_of_only_duplicate_cold_review_findings_is_retired_without_proposing_a_fix(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, ledger, entry, failure, pairs, github = self.cold_review(root, 3)
            duplicate = dict(repo='example/project', finding_url=failure['source_ref'], pr=1,
                             before=json.loads(failure['error'])['head'], after=None, reason='DUPLICATE_FINDING',
                             path='value.py', start_line=3, end_line=3, flagged_only=False,
                             duplicate_of='https://github.com/example/project/pull/1#cold-review-original')
            (store / learn_replay.REVIEW_PAIRS).write_text(json.dumps({failure['id']: duplicate}))
            with patch.object(learn_replay, 'proposed_proof') as proposed:
                result = self.replay_review(root, store, ledger, pairs, github)
            proposed.assert_not_called()
            self.assertEqual((result['proven'], result.get('unpairable')), (0, 1))
            self.assertEqual(entry['replay'], dict(status='rejected', reason='no exact pair: DUPLICATE_FINDING'))

    def test_a_cold_review_finding_without_a_fix_pair_closes_unpairable_and_no_fix_is_ever_written(self):
        import learn_daily
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, ledger, entry, failure, pairs, github = self.cold_review(root, 1)
            unfixed = dict(repo='example/project', finding_url=failure['source_ref'], pr=1,
                           before=json.loads(failure['error'])['head'], after=None, reason='FLAGGED_HUNK_UNCHANGED',
                           path='value.py', start_line=1, end_line=1)
            (store / learn_replay.REVIEW_PAIRS).write_text(json.dumps({failure['id']: unfixed}))
            with patch.object(learn_daily, 'ask') as ask:
                result = self.replay_review(root, store, ledger, pairs, github)
            ask.assert_not_called()
            self.assertNotIn('proposed_fix', entry)
            self.assertEqual((result['proven'], result.get('no_fix'), result.get('unpairable')), (0, None, 1))
            self.assertEqual(entry['replay'], dict(status='rejected', reason='no exact pair: FLAGGED_HUNK_UNCHANGED'))
            self.assertEqual(c.latest_lessons(c.load_store(store)[1]), {})

    def test_the_write_a_fix_step_refuses_a_cold_review_finding_before_asking_any_model(self):
        import learn_daily
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, ledger, entry, failure, pairs, github = self.cold_review(root, 1)
            row = dict(repo='example/project', finding_url=failure['source_ref'], pr=1,
                       before=json.loads(failure['error'])['head'], after=None, reason='FLAGGED_HUNK_UNCHANGED',
                       path='value.py', start_line=1, end_line=1)
            with patch.object(learn_daily, 'ask') as ask, \
                    self.assertRaisesRegex(c.ColdfixError, 'a review finding is never given a written fix'):
                learn_replay.proposed_proof(store, c.load_store(store)[1], entry, failure, row, root, 60, None)
            ask.assert_not_called()

    def test_a_draft_whose_cold_review_finding_is_not_yet_paired_is_deferred_not_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, ledger, entry, failure, pairs, github = self.cold_review(root, 3)
            with patch.object(learn_replay, 'PAIR_SECONDS', 0):
                result = self.replay_review(root, store, ledger, pairs, github)
            self.assertEqual((result['rejected'], result.get('deferred')), (0, 1))
            self.assertNotIn('replay', entry)
            self.assertNotIn('replay_attempts', entry)

    def test_a_failed_cold_review_pairing_pass_is_counted_and_the_replay_continues(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, ledger, entry, failure, pairs, github = self.cold_review(root, 3)
            with patch.object(learn_replay, 'pair_cold_reviews', side_effect=OSError('disk full')):
                result = self.replay_review(root, store, ledger, pairs, github)
            self.assertEqual(result['cold_review'], dict(error='disk full'))
            self.assertEqual(result['proven'], 0)

    def test_a_pair_matches_a_failure_moved_to_its_repository_new_name(self):
        failure = dict(id='f' * 32, kind='review', repo='example/old',
                       source_ref='https://github.com/example/old/pull/1#discussion_r1', error='finding')
        pair = dict(repo='example/new', finding_url=failure['source_ref'], before='a' * 40, after='b' * 40)
        store = dict(failures={failure['id']: failure})
        with patch.dict(learn_replay.moved, {failure['id']: 'example/new'}, clear=True):
            self.assertEqual(list(learn_replay.candidates(store, dict(evidence=[failure['id']]), [pair])),
                             [(failure, pair)])

    def untested_review_fix(self, root, base_test=None, fix_test=None, ci=False, also_failed=''):
        store = root / 'store'
        store.mkdir(mode=0o700)
        repo = root / 'project'
        (repo / 'tests').mkdir(parents=True)
        def git(*args):
            return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()
        git('init', '-q')
        git('config', 'user.name', 'Fixture')
        git('config', 'user.email', 'fixture@example.invalid')
        git('remote', 'add', 'origin', 'https://github.com/example/project.git')
        (repo / 'value.py').write_text('VALUE = -1\n')
        if base_test:
            (repo / 'tests/test_value.py').write_text(base_test)
        git('add', '.')
        git('commit', '-qm', 'Add the value')
        before = git('rev-parse', 'HEAD')
        (repo / 'value.py').write_text('VALUE = 1\n')
        if fix_test:
            (repo / 'tests/test_value.py').write_text(fix_test)
        git('add', '.')
        git('commit', '-qm', 'Keep the value positive')
        after = git('rev-parse', 'HEAD')
        if ci:
            failure = c.event_record(failure_ingest.ci_event(dict(
                type='ci', repo='example/project', run_id=7, workflow='verify', job='verify', runner='fixture',
                pr=None, duration_seconds=1, attempt=1, conclusion='failure', head_sha=before,
                occurred='2026-11-05T00:00:00Z',
                log_tail=also_failed + 'FAIL: test_value_is_positive (test_value.ValueTests)\nAssertionError')),
                'failure-ingest')
            pair = dict(repo='example/project', red_run=7, before=before, after=after)
        else:
            ref = 'https://github.com/example/project/pull/1#discussion_r1'
            failure = c.event_record(dict(kind='review', command='PR review finding', repo='example/project',
                error='**Keep the value positive**\n\nA negative value breaks the total.', agent='codex-review',
                source_ref=ref, occurred='2026-11-05T00:00:00Z'), 'github')
            pair = dict(repo='example/project', finding_url=ref, before=before, after=after, path='value.py',
                        start_line=1, end_line=1)
        c.save(store, 'failures', failure)
        entry = dict(day=datetime.now(timezone.utc).date().isoformat(), verdict='PASS', evidence=[failure['id']],
                     headline='Keep the value positive',
                     draft=dict(kind='test', title='Keep values positive', rule='Keep every value positive.'))
        ledger = dict(drafts={'candidate': entry}, runs=[])
        (store / 'learn-drafts.json').write_text(json.dumps(ledger))
        pairs = root / 'pairs.json'
        pairs.write_text(json.dumps(dict(results=[pair])))
        return store, ledger, entry, pairs

    def replay_with_generated(self, source, **fixture):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, ledger, entry, pairs = self.untested_review_fix(root, **fixture)
            with patch.object(replay_isolation, 'replay_tree', c.replay_tree), \
                    patch.object(learn_replay, 'write_test', return_value=(source, 'claude-haiku-5-5')) as write:
                result = learn_replay.run(store, ledger, projects=root, pairs_path=pairs)
            lessons = [lesson for lesson in c.latest_lessons(c.load_store(store)[1]).values()
                       if lesson['verification'] == 'proven']
            return result, entry, write, lessons

    GENERATED = ('import unittest\nfrom value import VALUE\n'
                 'class ProofTests(unittest.TestCase):\n'
                 '    def test_value_is_positive(self):\n'
                 '        self.assertGreater(VALUE, 0)\n')
    VACUOUS = ('import unittest\nclass ValueTests(unittest.TestCase):\n'
               '    def test_value_is_positive(self):\n        pass\n')

    def test_pinned_rejection_environment_survives_live_and_oss_readback(self):
        missing = self.GENERATED.replace('from value import VALUE', 'import coldfix_missing_module')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stores = {}
            for scope in ('live', 'oss'):
                for case in ('reconstructed', 'host-only', 'unknown', 'proven'):
                    with self.subTest(scope=scope, case=case):
                        project = root / scope / case
                        project.mkdir(parents=True)
                        test = self.GENERATED if case in ('unknown', 'proven') else missing
                        if case in ('reconstructed', 'proven'):
                            package = project / 'project/packages/kit'
                            package.mkdir(parents=True)
                            (package / 'pyproject.toml').write_text('[project]\nname = "coldfix-fixture-kit"\n')
                            (package / 'coldfix_fixture_kit.py').write_text('import unittest\n')
                            test = test.replace('import unittest', 'from coldfix_fixture_kit import unittest')
                        store, ledger, entry, pairs = self.untested_review_fix(project, base_test=test, ci=True)
                        pair = json.loads(pairs.read_text())['results'][0]
                        if case == 'unknown':
                            entry['draft']['rule'] = 'Make test_value_is_positive pass.'
                        with patch.dict(os.environ, COLDFIX_REPLAY_CACHE=str(project / 'cache')):
                            os.environ.pop(replay_isolation.RUNNER, None)
                            with patch.object(replay_env, 'install', wraps=replay_env.install) as install:
                                if case == 'host-only':
                                    install.return_value = 'fixture setup failed'
                                if scope == 'live':
                                    learn_replay.run(store, ledger, projects=project, pairs_path=pairs)
                                else:
                                    learn_replay.run_in_workspace(store, ledger, project, pairs, 600, history=False,
                                                                  unbounded=True)
                        saved = ci_batch.learn_daily_ledger(store)['drafts']['candidate']
                        stores[scope, case] = store
                        if case == 'proven':
                            self.assertEqual(saved['replay']['status'], 'proven')
                            self.assertNotIn('replay_attempts', saved)
                            receipt = c.load_store(store)[1]['receipts'][saved['replay']['proof']['receipt_id']]
                            self.assertEqual([(receipt[side]['status'], receipt[side]['env'])
                                              for side in ('before', 'after', 'reversed')],
                                             [('FAIL', 'reconstructed'), ('PASS', 'reconstructed'),
                                              ('FAIL', 'reconstructed')])
                            continue
                        self.assertEqual(saved['replay']['status'], 'rejected')
                        [attempt] = saved['replay_attempts']
                        self.assertEqual((attempt['before'], attempt['after']), (pair['before'], pair['after']))
                        if case == 'unknown':
                            self.assertNotIn('env', attempt)
                            self.assertFalse(install.called)
                        else:
                            self.assertIn("No module named 'coldfix_missing_module'", attempt['reason'])
                            self.assertEqual(attempt.get('env'), case)
                            self.assertNotIn('env_error', attempt)
                            self.assertTrue(install.called)
                        if scope == 'oss':
                            counts = ci_batch.measure(store, set(saved['evidence'])).get('rejection_environments')
                            self.assertEqual(counts, {case: 1})
            for case in ('reconstructed', 'host-only', 'unknown'):
                if ('live', case) in stores and ('oss', case) in stores:
                    report = learn_report.report(stores['live', case], datetime.now(timezone.utc),
                                                 oss_store=stores['oss', case])
                    self.assertEqual(report.get('replay_rejection_environments'), {case: 1})
                    self.assertEqual(report['outside_repos'].get('replay_rejection_environments'), {case: 1})

    def test_a_ci_fix_whose_recorded_test_passes_before_it_is_proven_by_a_generated_test_of_that_name(self):
        source = "from pathlib import Path\n" + self.GENERATED.replace(
            "        self.assertGreater", '        self.assertTrue((Path(__file__).resolve().parents[1] / "value.py").is_file())\n'
            "        self.assertGreater")
        result, entry, write, lessons = self.replay_with_generated(source, base_test=self.VACUOUS, ci=True)
        self.assertEqual(result, dict(proven=1, rejected=1), str(entry.get('replay')))
        self.assertIn('red PASS', entry['replay_attempts'][0]['reason'])
        self.assertEqual(write.call_args.args[3], ['test_value_is_positive'])
        [lesson] = lessons
        self.assertEqual(lesson['admission']['diagnostic'], 'FAIL: test_value_is_positive')
        self.assertRegex(lesson['admission']['test_paths'][0], r'^tests/test_coldfix_proof_[0-9a-f]{12}\.py$')

    def test_a_generated_ci_test_may_reproduce_any_recorded_failed_test(self):
        result, entry, write, lessons = self.replay_with_generated(
            self.GENERATED, base_test=self.VACUOUS, ci=True, also_failed='FAIL: test_another_case (test_value.ValueTests)\n')
        self.assertEqual(result['proven'], 1, str(entry.get('replay')))
        self.assertEqual(write.call_args.args[3], ['test_another_case', 'test_value_is_positive'])
        self.assertEqual(lessons[0]['admission']['diagnostic'], 'FAIL: test_value_is_positive')

    def test_a_ci_failure_matched_only_by_a_pair_without_its_red_run_gets_no_generated_test(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, ledger, entry, pairs = self.untested_review_fix(root, base_test=self.VACUOUS, ci=True)
            [pair] = json.loads(pairs.read_text())['results']
            del pair['red_run']
            pairs.write_text(json.dumps(dict(results=[pair])))
            with patch.object(replay_isolation, 'replay_tree', c.replay_tree), \
                    patch.object(learn_replay, 'write_test', return_value=(self.GENERATED, 'claude-haiku-5-5')) as write:
                result = learn_replay.run(store, ledger, projects=root, pairs_path=pairs)
            self.assertEqual((result['proven'], write.call_count), (0, 0))
            self.assertIn('red PASS', entry['replay']['reason'])

    def test_a_pair_ruled_out_before_generated_tests_covered_it_is_replayed_once_more(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, ledger, entry, pairs = self.untested_review_fix(root, base_test=self.VACUOUS, ci=True)
            [pair] = json.loads(pairs.read_text())['results']
            key = ' '.join((entry['evidence'][0], pair['before'], pair['after']))
            entry['replay_terminal'] = {key: dict(day=learn_replay.utc_day(), reason=(
                'incident not admitted; red PASS, green PASS, reversed-patch PASS; expected FAIL, PASS, FAIL'))}
            with patch.object(replay_isolation, 'replay_tree', c.replay_tree), \
                    patch.object(learn_replay, 'write_test', return_value=(self.GENERATED, 'claude-haiku-5-5')):
                result = learn_replay.run(store, ledger, projects=root, pairs_path=pairs)
            self.assertEqual(result['proven'], 1, str(entry.get('replay')))

    def test_a_ci_reply_that_does_not_define_the_recorded_test_is_unusable(self):
        renamed = self.GENERATED.replace('test_value_is_positive', 'test_something_else')
        result, entry, _, lessons = self.replay_with_generated(renamed, base_test=self.VACUOUS, ci=True)
        self.assertEqual((result['proven'], lessons), (0, []))
        self.assertIn('generated test: model wrote no usable unittest', entry['replay']['reason'])

    def test_a_fix_whose_own_test_the_privacy_screen_blocks_is_proven_by_a_generated_test(self):
        private = self.GENERATED.replace('ProofTests', 'ValueTests') + '    HOST = "build.internal.example.com"\n'
        result, entry, write, lessons = self.replay_with_generated(self.GENERATED, fix_test=private)
        self.assertEqual((result['proven'], write.call_count), (1, 1), str(entry.get('replay')))
        self.assertIn('privacy screen blocked', entry['replay_attempts'][0]['reason'])
        [lesson] = lessons
        self.assertRegex(lesson['admission']['test_paths'][0], r'^tests/test_coldfix_proof_')

    def test_a_generated_test_with_a_private_hostname_in_a_string_is_blocked(self):
        private = self.GENERATED + '        self.assertTrue("build.internal.example.com")\n'
        result, entry, _, lessons = self.replay_with_generated(private)
        self.assertEqual((result['proven'], lessons), (0, []))
        self.assertIn('generated test: incident cannot be exported; privacy screen blocked', entry['replay']['reason'])

    def test_an_unusable_first_reply_is_replaced_by_the_second_bulk_route(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, ledger, entry, pairs = self.untested_review_fix(root)
            replies = [('placeholder', 'claude-haiku-5-5'), (self.GENERATED, 'gpt-6-luna')]
            with patch.object(replay_isolation, 'replay_tree', c.replay_tree), \
                    patch.object(learn_replay, 'write_test', side_effect=replies) as write:
                result = learn_replay.run(store, ledger, projects=root, pairs_path=pairs)
            self.assertEqual((result['proven'], [call.args[4] for call in write.call_args_list]), (1, [0, 1]),
                             str(entry.get('replay')))
            [stored] = json.loads((store / 'proof-tests.json').read_text()).values()
            self.assertEqual((stored['model'], len(stored['asked']), stored['usable']), ('gpt-6-luna', 2, True))

    def test_a_model_call_waits_no_longer_than_the_collect_deadline_on_every_route(self):
        import learn_daily
        deadline = __import__('time').monotonic() + 5
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            def run(command, **kwargs):
                (work / 'reply.json').write_text('{}')
                return subprocess.CompletedProcess(command, 0, stdout=json.dumps(
                    dict(is_error=False, structured_output={})))
            with patch.object(learn_daily.subprocess, 'run', side_effect=run) as cli, \
                    patch.object(c, 'process', return_value=subprocess.CompletedProcess([], 0, stdout=b'{}')) as command, \
                    patch.dict(os.environ, COLDFIX_MODEL_CMD='model-command'):
                for model in (dict(provider='claudeAgent', model='claude-haiku-5-5'),
                              dict(provider='codex', model='gpt-6-luna'), dict(provider='command', role='drafter')):
                    learn_daily.ask(model, 'prompt', learn_replay.GENERATED_TEST, work, deadline)
            timeouts = [call.kwargs['timeout'] for call in [*cli.call_args_list, *command.call_args_list]]
            self.assertEqual(len(timeouts), 3)
            self.assertTrue(all(0 < timeout <= 5 for timeout in timeouts), timeouts)
            with self.assertRaisesRegex(c.ColdfixError, 'replay budget exhausted'):
                learn_daily.ask(dict(provider='command', role='drafter'), 'prompt', learn_replay.GENERATED_TEST,
                                work, deadline - 10)

    def test_a_generated_test_cut_off_by_the_deadline_is_deferred_and_asked_again_next_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, ledger, entry, pairs = self.untested_review_fix(root)
            cut = []
            def write(*args, **kwargs):
                if not cut:
                    cut.append(kwargs['deadline'])
                    raise subprocess.TimeoutExpired('model', 1)
                return self.GENERATED, 'claude-haiku-5-5'
            with patch.object(replay_isolation, 'replay_tree', c.replay_tree), \
                    patch.object(learn_replay, 'write_test', side_effect=write) as written:
                with patch.object(learn_replay, 'expired', side_effect=lambda deadline: bool(cut)):
                    first = learn_replay.run(store, ledger, projects=root, pairs_path=pairs)
                self.assertIsNotNone(cut[0])
                self.assertEqual(first.get('deferred'), 1, str(entry.get('replay')))
                self.assertNotIn('generated test:', json.dumps(entry))
                second = learn_replay.run(store, ledger, projects=root, pairs_path=pairs)
            self.assertEqual((second['proven'], written.call_count), (1, 2), str(entry.get('replay')))

    def second_route_after(self, first_run_replies, edit_stored=lambda row: None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, ledger, entry, pairs = self.untested_review_fix(root)
            tests = store / 'proof-tests.json'
            later = [(self.GENERATED, 'gpt-6-luna')]
            with patch.object(replay_isolation, 'replay_tree', c.replay_tree), \
                    patch.object(learn_replay, 'write_test', side_effect=first_run_replies + later) as write:
                learn_replay.run(store, ledger, projects=root, pairs_path=pairs)
                [(key, stored)] = json.loads(tests.read_text()).items()
                first = (stored['usable'], len(stored['asked']), entry['replay']['status'])
                edit_stored(stored)
                tests.write_text(json.dumps({key: stored}))
                for attempt in [*entry['replay_attempts'], entry['replay_settled']]:
                    attempt['day'] = '2026-01-01'
                second = learn_replay.run(store, ledger, projects=root, pairs_path=pairs)
            [stored] = json.loads(tests.read_text()).values()
            return first, second['proven'], [call.args[4] for call in write.call_args_list], stored

    def test_a_generated_test_that_ends_incomplete_is_asked_again_once_then_counts_as_not_generated(self):
        missing = self.GENERATED.replace('from value import VALUE', 'import coldfix_missing_module')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, ledger, entry, pairs = self.untested_review_fix(root)
            calls, reasons, settled = [], [], []
            with patch.object(replay_isolation, 'replay_tree', c.replay_tree), \
                    patch.object(learn_replay, 'write_test', return_value=(missing, 'claude-haiku-5-5')) as write:
                for rules in (0, 0, 0, 1):
                    with patch.object(learn_replay, 'REPLAY_RULES', learn_replay.REPLAY_RULES + rules):
                        learn_replay.run(store, ledger, projects=root, pairs_path=pairs)
                    calls.append(write.call_count)
                    reasons.append(entry['replay']['reason'])
                    settled.append(entry['replay_settled']['confirmed'])
                    entry.pop('replay_terminal', None)
                    for attempt in [*entry['replay_attempts'], entry.get('replay_settled') or {}]:
                        attempt['day'] = '2026-01-01'
                    if rules == 0 and len(calls) == 3:
                        [stored] = json.loads((store / 'proof-tests.json').read_text()).values()
        self.assertEqual(calls, [1, 2, 2, 3])
        self.assertIn("generated test: incident not admitted; flagged lines reverted INCOMPLETE; expected FAIL; "
                      "ModuleNotFoundError: No module named 'coldfix_missing_module'", reasons[0])
        self.assertEqual(settled, [False, True, True, True])
        self.assertIn("No module named 'coldfix_missing_module' outside this repo", reasons[2])
        self.assertEqual((stored['usable'], stored['incomplete'], stored['reasked']),
                         (False, True, learn_replay.REPLAY_RULES))
        self.assertEqual(write.call_args.kwargs['path'], stored['path'])

    def test_a_usable_reasked_test_settles_its_terminal_outcome_after_the_rules_version_changes(self):
        missing = self.GENERATED.replace('from value import VALUE', 'import coldfix_missing_module')
        replies = [(missing, 'claude-haiku-5-5'), (self.VACUOUS, 'claude-haiku-5-5')]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, ledger, entry, pairs = self.untested_review_fix(root)
            settled = []
            with patch.object(replay_isolation, 'replay_tree', c.replay_tree), \
                    patch.object(learn_replay, 'write_test', side_effect=replies) as write:
                for rules in (0, 0, 1):
                    with patch.object(learn_replay, 'REPLAY_RULES', learn_replay.REPLAY_RULES + rules):
                        learn_replay.run(store, ledger, projects=root, pairs_path=pairs)
                    settled.append(entry['replay_settled']['confirmed'])
                    entry.pop('replay_terminal', None)
                    for attempt in [*entry['replay_attempts'], entry['replay_settled']]:
                        attempt['day'] = '2026-01-01'
        self.assertEqual(write.call_count, 2)
        self.assertIn('generated test: incident not admitted; flagged lines reverted PASS; expected FAIL',
                      entry['replay']['reason'])
        self.assertEqual(settled, [False, True, True])

    def test_a_second_route_that_errors_keeps_the_first_reply_and_is_asked_again_alone(self):
        replies = [('placeholder', 'claude-haiku-5-5'), c.ColdfixError('route unavailable')]
        first, proven, routes, stored = self.second_route_after(replies)
        self.assertEqual(first, (False, 1, 'rejected'))
        self.assertEqual((proven, routes), (1, [0, 1, 1]))
        self.assertEqual((stored['model'], len(stored['asked']), stored['usable']), ('gpt-6-luna', 2, True))

    def test_a_generated_reask_with_no_usable_unittest_settles_the_draft(self):
        missing = self.GENERATED.replace('from value import VALUE', 'import coldfix_missing_module')
        replies = [(missing, 'fixture'), ('placeholder', 'fixture')]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, ledger, entry, pairs = self.untested_review_fix(root)
            with patch.object(replay_isolation, 'replay_tree', c.replay_tree), \
                    patch.object(learn_replay, 'write_test', side_effect=replies):
                learn_replay.run(store, ledger, projects=root, pairs_path=pairs)
                self.assertFalse(entry['replay_settled']['confirmed'])
                for attempt in [*entry['replay_attempts'], entry['replay_settled']]:
                    attempt['day'] = '2026-01-01'
                learn_replay.run(store, ledger, projects=root, pairs_path=pairs)
            self.assertEqual(entry['replay']['status'], 'rejected')
            self.assertIn('generated test: model wrote no usable unittest', entry['replay']['reason'])
            self.assertTrue(entry['replay_settled']['confirmed'])

    def test_an_unusable_reply_stored_before_the_second_route_existed_is_asked_of_it_once(self):
        replies = [('placeholder', 'claude-haiku-5-5'), ('placeholder', 'gpt-6-luna')]
        first, proven, routes, stored = self.second_route_after(replies, lambda row: row.pop('asked'))
        self.assertEqual(first, (False, 2, 'rejected'))
        self.assertEqual((proven, routes), (1, [0, 1, 1]))
        self.assertEqual((len(stored['asked']), stored['usable']), (2, True))

    def test_the_ci_prompt_names_the_recorded_test_and_quotes_its_log(self):
        import learn_daily
        failure = dict(kind='ci', error=json.dumps(dict(
            type='ci', log_tail='FAIL: test_value_is_positive (t.V)\nFAIL: test_other (t.V)\nFAIL: test_other (t.V)')))
        with patch.object(learn_daily, 'primaries', return_value=[dict(model='claude-haiku-5-5')]), \
                patch.object(learn_daily, 'ask', return_value=dict(source='x')) as ask:
            reply = learn_replay.write_test(failure, 'the diff', Path('.'), learn_replay.failed_tests(failure))
        model, prompt = ask.call_args.args[:2]
        self.assertEqual((reply, model), (('x', 'claude-haiku-5-5'), dict(model='claude-haiku-5-5')))
        self.assertIn('reproduces the CI failure below', prompt)
        self.assertIn('named after the recorded failed test it reproduces: test_value_is_positive or test_other,', prompt)
        self.assertIn('FAIL: test_value_is_positive (t.V)', prompt)

    def test_the_prompt_locates_the_repository_root_from_the_test_directory(self):
        import learn_daily
        with patch.object(learn_daily, 'primaries', return_value=[dict(model='claude-haiku-5-5')]), \
                patch.object(learn_daily, 'ask', return_value=dict(source='x')) as ask:
            learn_replay.write_test(dict(kind='review', error='finding'), 'the diff', Path('.'),
                                    path='tests/test_coldfix_proof_0123456789ab.py')
        self.assertIn('The file is saved as tests/test_coldfix_proof_0123456789ab.py, so the '
                      'repository root is Path(__file__).resolve().parents[1]', ask.call_args.args[1])

    def test_a_review_test_writer_gets_the_flagged_files_real_names_and_parameters(self):
        _, entry, write, _ = self.replay_with_generated(self.GENERATED)
        self.assertEqual(write.call_args.kwargs.get('api'), 'VALUE = ...', str(entry.get('replay')))
        with patch.object(c, 'privacy_counts', return_value={'secret': 1}):
            _, _, screened, _ = self.replay_with_generated(self.GENERATED)
        self.assertEqual(screened.call_args.kwargs.get('api'), '')
        source = ('from dataclasses import dataclass\n'
                  '@dataclass(frozen=True)\nclass Config:\n    mode: str\n    retries: int = 3\n'
                  'class Source(Base):\n    def __init__(self, config, *, interval=None):\n        self._token = None\n'
                  '    async def _fetch(self, now: float) -> str:\n        return ""\n')
        api = learn_replay.flagged_api(source)
        self.assertEqual(api.splitlines(), ['@dataclass(frozen=True)', 'class Config():', '    mode: str',
                                            '    retries: int = ...', 'class Source(Base):',
                                            '    def __init__(self, config, *, interval=None)',
                                            '    async def _fetch(self, now: float) -> str'])
        import learn_daily
        with patch.object(learn_daily, 'primaries', return_value=[dict(model='claude-haiku-5-5')]), \
                patch.object(learn_daily, 'ask', return_value=dict(source='x')) as ask:
            learn_replay.write_test(dict(kind='review', error='finding'), 'the diff', Path('.'), api=api)
        self.assertIn('use only those names and call them with exactly those parameters', ask.call_args.args[1])
        self.assertIn('def __init__(self, config, *, interval=None)', ask.call_args.args[1])

    def test_the_review_prompt_screens_each_local_import_independently(self):
        import learn_daily
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / 'project'
            package = repo / 'src/example'
            package.mkdir(parents=True)
            files = {
                'source.py': ('from .config import Config\nfrom . import blocked\n'
                              'import example.clock as clock\nimport unavailable\n'
                              'def fetch(password="unsafe"): pass\n'),
                'config.py': 'def Config(mode, client_id, api_endpoint): pass\n',
                'blocked.py': 'def blocked(password="unsafe"): pass\n',
                'clock/__init__.py': 'def now(): pass\n',
            }
            for name, source in files.items():
                path = package / name
                path.parent.mkdir(exist_ok=True)
                path.write_text(source)
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            git = ['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid']
            subprocess.run([*git, 'add', '.'], check=True)
            subprocess.run([*git, 'commit', '-qm', 'Before'], check=True)
            before = c.privacy_git(repo, 'rev-parse', 'HEAD').decode().strip()
            (package / 'config.py').write_text('def Config(): pass\n')
            subprocess.run([*git, 'commit', '-qam', 'After'], check=True)
            after = c.privacy_git(repo, 'rev-parse', 'HEAD').decode().strip()
            failure = dict(id='fixture', repo='example/project', kind='review', command='review', error='finding')
            with patch.object(learn_daily, 'primaries', return_value=[dict(model='fixture')]), \
                    patch.object(learn_daily, 'ask', return_value=dict(source=self.GENERATED)) as ask:
                learn_replay.stored_test(root, failure, repo, before, after, 10, flagged='src/example/source.py')
            prompt = ask.call_args.args[1]
            payload = json.loads(prompt.split('\n', 1)[1])
            self.assertNotIn('flagged_file_api', payload)
            self.assertEqual(payload.get('imported_module_apis'), {
                'src/example/config.py': 'def Config(mode, client_id, api_endpoint)',
                'src/example/clock/__init__.py': 'def now()',
            })

    def test_the_changed_test_pattern_still_reads_stored_patches_the_way_their_lessons_pinned_them(self):
        patch_text = '+    def test_new(self):\n+        pass\n+\n     def test_old(self):\n'
        self.assertEqual(regression_adapter.TEST_NAME.findall(patch_text), ['test_new', 'test_old'],
                         'check_pins recomputes names from each stored patch; a narrower pattern makes load_store '
                         'reject existing lessons with "lesson test digest changed"')

    def test_a_review_fix_without_a_test_is_proven_by_a_stored_generated_test(self):
        source = ('import unittest\nfrom value import VALUE\n'
                  'class ValueTests(unittest.TestCase):\n'
                  '    def test_value_is_positive(self):\n'
                  '        self.assertGreater(VALUE, 0)\n')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, ledger, entry, pairs = self.untested_review_fix(root)
            with patch.object(replay_isolation, 'replay_tree', c.replay_tree), \
                    patch.object(learn_replay, 'write_test', create=True,
                                 return_value=(source, 'claude-haiku-5-5')) as write:
                result = learn_replay.run(store, ledger, projects=root, pairs_path=pairs)
            self.assertEqual(result, dict(proven=1, rejected=1), str(entry.get('replay')))
            self.assertEqual(write.call_count, 1)
            [lesson] = c.latest_lessons(c.load_store(store)[1]).values()
            self.assertEqual((lesson['verification'], lesson['admission']['diagnostic']),
                             ('proven', 'FAIL: test_value_is_positive'))
            tests = store / 'proof-tests.json'
            self.assertTrue(tests.exists())
            [stored] = json.loads(tests.read_text()).values()
            self.assertEqual((stored['source'], stored['model']), (source, 'claude-haiku-5-5'))
            self.assertEqual(lesson['admission']['test_paths'], [stored['path']])

    def test_a_reply_that_is_not_one_unittest_is_asked_of_each_bulk_route_once_then_kept_unusable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, ledger, entry, pairs = self.untested_review_fix(root)
            with patch.object(replay_isolation, 'replay_tree', c.replay_tree), \
                    patch.object(learn_replay, 'write_test', create=True,
                                 return_value=('placeholder', 'claude-haiku-5-5')) as write:
                learn_replay.run(store, ledger, projects=root, pairs_path=pairs)
                for attempt in entry['replay_attempts']:
                    attempt['day'] = '2026-01-01'
                second = learn_replay.run(store, ledger, projects=root, pairs_path=pairs)
            self.assertEqual((second['proven'], [call.args[4] for call in write.call_args_list]), (0, [0, 1]))
            self.assertTrue((store / 'proof-tests.json').exists())
            stored = list(json.loads((store / 'proof-tests.json').read_text()).values())
            self.assertEqual([(test['source'], test['usable'], len(test['asked'])) for test in stored],
                             [('placeholder', False, 2)])
            self.assertIn('generated test: model wrote no usable unittest', entry['replay']['reason'])

    def test_a_stored_generated_test_is_reused_and_never_rewritten(self):
        source = ('import unittest\nfrom value import VALUE\n'
                  'class ValueTests(unittest.TestCase):\n'
                  '    def test_value_is_small(self):\n'
                  '        self.assertLess(VALUE, 5)\n')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, ledger, entry, pairs = self.untested_review_fix(root)
            with patch.object(replay_isolation, 'replay_tree', c.replay_tree), \
                    patch.object(learn_replay, 'write_test', create=True,
                                 return_value=(source, 'claude-haiku-5-5')) as write:
                learn_replay.run(store, ledger, projects=root, pairs_path=pairs)
                tests = store / 'proof-tests.json'
                self.assertTrue(tests.exists())
                saved = tests.read_bytes()
                for attempt in entry['replay_attempts']:
                    attempt['day'] = '2026-01-01'
                second = learn_replay.run(store, ledger, projects=root, pairs_path=pairs)
            self.assertEqual((second['proven'], write.call_count), (0, 1))
            self.assertEqual(tests.read_bytes(), saved)
            self.assertIn('generated test: incident not admitted; flagged lines reverted PASS', entry['replay']['reason'])


class TranscriptCodeLeadReplayTests(unittest.TestCase):
    GENERATED = ReviewPairReplayTests.GENERATED

    def lead(self, root, after_mistake, thread='', named_repo='example/project', merged=False, repo_name='example/project',
             session=None, folder='/srv/projects/project'):
        store = root / 'store'
        store.mkdir(mode=0o700)
        repo = root / 'project'
        repo.mkdir()
        def git(*args):
            return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()
        git('init', '-q')
        git('config', 'user.name', 'Fixture')
        git('config', 'user.email', 'fixture@example.invalid')
        git('remote', 'add', 'origin', 'https://github.com/example/project.git')
        (repo / 'value.py').write_text('VALUE = -1\n')
        git('add', '.')
        git('commit', '-qm', 'Add the value')
        before = git('rev-parse', 'HEAD')
        git('update-ref', 'refs/remotes/origin/main', before)
        git('symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/main')
        (repo / 'value.py').write_text('VALUE = 1\n')
        git('commit', '-qam', 'Keep the value positive')
        after = git('rev-parse', 'HEAD')
        git('update-ref', 'refs/remotes/origin/pull/7', after)
        if merged:
            git('checkout', '-q', '-b', 'main', before)
            (repo / 'notes.txt').write_text('notes\n')
            git('add', '.')
            git('commit', '-qm', 'Add notes')
            git('merge', '-q', '--no-ff', '-m', 'Merge pull request #7', after)
            git('update-ref', 'refs/remotes/origin/main', git('rev-parse', 'HEAD'))
        named = after_mistake.format(repo=named_repo, after=after)
        evidence = ('user [u1]: Keep VALUE positive.\n\nassistant [a1]: I set VALUE to -1.\n\n'
                    'user [u2]: Wrong, it must stay positive.\n\nassistant [a2]: ' + named)
        transcript = root / 'transcript.md'
        turns = [('user', 'u1', 'Keep VALUE positive.'), ('assistant', 'a1', 'I set VALUE to -1.'),
                 ('user', 'u2', 'Wrong, it must stay positive.'), ('assistant', 'a2', named),
                 *[('assistant', 'a3', text.format(repo=named_repo, after=after)) for text in [session] if session]]
        transcript.write_text((f'---\nproject_hint: "{folder}"\n---\n\n' if folder else '') + ''.join(f'## {role} · 2026-11-05 00:00:0{number}Z · {ident}\n\n{text}\n\n'
                                      for number, (role, ident, text) in enumerate(turns)))
        failure = c.event_record(dict(kind='correction', command='transcript correction', repo=repo_name,
            agent='claude', error=json.dumps(dict(type='transcript_correction', score=0.9, evidence=evidence)),
            source_ref=f'transcript:{transcript}#messages=u1,a1,u2,a2', occurred='2026-11-05T00:00:00Z',
            thread=thread), 'stdin')
        c.save(store, 'failures', failure)
        lesson = dict(id=failure['id'], verdict='lesson', source='owner', kind='code', title='Keep values positive',
                      what_went_wrong='The agent set a value negative.', rule='Keep every value positive.',
                      how_to_check='Assert the value is positive.', duplicate_of='', wrong_message='a1',
                      fixed_message='a2')
        (store / 'transcript-lessons.json').write_text(json.dumps(dict(batches=[], leads={failure['id']: dict(
            status='unprovable', reason='code mistake needs repository replay', lesson=lesson,
            source_ref=failure['source_ref'])})))
        return store, failure, before, after

    def replay(self, root, store, source=None):
        with patch.object(replay_isolation, 'replay_tree', c.replay_tree), \
                patch.object(learn_replay, 'write_test', return_value=(source or self.GENERATED, 'claude-haiku-5-5')) as write:
            counts = learn_replay.transcript_run(store, projects=root)
        routes = store / 'transcript-replay.json'
        return counts, write, json.loads(routes.read_text()) if routes.exists() else {}

    def test_a_code_lead_that_names_its_pull_request_is_proven_by_a_generated_test(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, failure, before, after = self.lead(root, 'Fixed in https://github.com/{repo}/pull/7')
            counts, write, routes = self.replay(root, store)
            self.assertEqual(counts, dict(routed=1, proven=1, rejected=0))
            route = routes[failure['id']]
            self.assertEqual((route['status'], route['fix'], route['before'], route['after']),
                             ('proven', 'https://github.com/example/project/pull/7', before, after))
            self.assertIn('Keep every value positive.', write.call_args.kwargs['finding'])
            [lesson] = c.latest_lessons(c.load_store(store)[1]).values()
            self.assertEqual((lesson['verification'], lesson['evidence'], lesson['admission']['diagnostic'], lesson['rule']),
                             ('proven', [failure['id']], 'FAIL: test_value_is_positive', 'Keep every value positive.'))
            self.assertEqual(route['lesson_id'], lesson['lesson_id'])

    def test_incomplete_transcript_check_retries_its_original_assertion_in_tests(self):
        source = ("from pathlib import Path\nimport unittest\n"
                  "class ValueTests(unittest.TestCase):\n"
                  "    def test_value_is_positive(self):\n"
                  "        value = Path(__file__).resolve().parents[1] / 'value.py'\n"
                  "        self.assertEqual(value.read_text().strip(), 'VALUE = 1')\n")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, failure, _, _ = self.lead(root, 'Fixed in https://github.com/{repo}/pull/7')
            original = Path(learn_replay.test_path(failure)).name
            with patch.object(learn_replay, 'test_path', return_value=original):
                _, _, routes = self.replay(root, store, source)
            self.assertIn("FileNotFoundError", routes[failure['id']]['reason'])
            with patch.object(learn_replay, 'recent', return_value=False):
                counts, write, routes = self.replay(root, store, 'invalid replacement')
            self.assertEqual(counts, dict(routed=1, proven=1, rejected=0))
            write.assert_not_called()
            receipt = c.load_store(store)[1]['receipts'][routes[failure['id']]['receipt_id']]
            self.assertEqual([receipt[s]['status'] for s in ('before', 'after', 'reversed')],
                             ['FAIL', 'PASS', 'FAIL'])

    def test_a_code_lead_that_names_its_fix_commit_replays_that_commit_against_its_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, failure, before, after = self.lead(root, 'Fixed in https://github.com/{repo}/commit/{after}')
            counts, _, routes = self.replay(root, store)
            self.assertEqual(counts['proven'], 1)
            self.assertEqual((routes[failure['id']]['before'], routes[failure['id']]['after']), (before, after))

    def test_a_pull_request_from_the_thread_routes_a_lead_whose_text_names_none(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, failure, _, _ = self.lead(root, 'Fixed.', thread='https://github.com/example/project/pull/7')
            self.assertEqual(self.replay(root, store)[0], dict(routed=1, proven=1, rejected=0))

    def test_a_pull_request_in_another_repository_is_not_routed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, _, _, _ = self.lead(root, 'Fixed in https://github.com/{repo}/pull/7', named_repo='example/other')
            counts, write, routes = self.replay(root, store)
            self.assertEqual((counts, routes, write.call_count), (dict(routed=0, proven=0, rejected=0), {}, 0))

    def test_a_pull_request_the_session_opened_after_the_excerpt_routes_the_lead(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, failure, before, after = self.lead(root, 'Fixed.', session='Opened https://github.com/{repo}/pull/7')
            counts, _, routes = self.replay(root, store)
            self.assertEqual(counts, dict(routed=1, proven=1, rejected=0))
            self.assertEqual((routes[failure['id']]['fix'], routes[failure['id']]['before'], routes[failure['id']]['after']),
                             ('https://github.com/example/project/pull/7', before, after))

    def test_a_lead_without_a_repository_takes_the_one_its_session_pull_request_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, failure, _, _ = self.lead(root, 'Fixed.', repo_name='unknown',
                                             session='Opened https://github.com/{repo}/pull/7')
            self.assertEqual(self.replay(root, store)[0], dict(routed=1, proven=1, rejected=0))
            self.assertEqual(replay_repos.load(store), {failure['id']: 'example/project'})
            [(_, _, repo)] = check_delivery.transcript_candidates(store, c.load_store(store)[1], replay_repos.load(store))
            self.assertEqual(repo, 'example/project')

    def test_a_lead_without_a_repository_whose_session_names_two_repositories_is_not_routed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, _, _, _ = self.lead(root, 'Fixed.', repo_name='unknown', session=(
                'Opened https://github.com/{repo}/pull/7 and https://github.com/example/other/pull/8'))
            counts, write, routes = self.replay(root, store)
            self.assertEqual((counts, routes, write.call_count, replay_repos.load(store)),
                             (dict(routed=0, proven=0, rejected=0), {}, 0, {}))

    def test_a_lead_without_a_repository_ignores_a_pull_request_outside_its_session_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, _, _, _ = self.lead(root, 'Fixed.', repo_name='unknown', folder='/srv/projects/other',
                                       session='See https://github.com/{repo}/pull/7 for an example')
            counts, write, routes = self.replay(root, store)
            self.assertEqual((counts, routes, write.call_count), (dict(routed=0, proven=0, rejected=0), {}, 0))

    def test_a_lead_without_a_repository_ignores_a_folder_quoted_in_the_conversation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, _, _, _ = self.lead(root, 'Fixed.', repo_name='unknown', folder=None, session=(
                'The log shows {{"cwd": "/srv/projects/project"}}. Opened https://github.com/{repo}/pull/7'))
            self.assertEqual(self.replay(root, store)[0], dict(routed=0, proven=0, rejected=0))

    def test_a_lead_without_a_repository_ignores_a_subdirectory_named_like_the_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, _, _, _ = self.lead(root, 'Fixed.', repo_name='unknown', folder='/srv/projects/app/project',
                                       session='Opened https://github.com/{repo}/pull/7')
            self.assertEqual(self.replay(root, store)[0], dict(routed=0, proven=0, rejected=0))

    def test_a_lead_without_a_repository_takes_only_the_first_checkout_in_its_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, _, _, _ = self.lead(root, 'Fixed.', repo_name='unknown',
                                       folder='/srv/projects/app/tests/worktrees/project',
                                       session='Opened https://github.com/{repo}/pull/7')
            self.assertEqual(self.replay(root, store)[0], dict(routed=0, proven=0, rejected=0))

    def test_a_lead_without_a_repository_ignores_an_unclosed_front_matter_block(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, failure, _, _ = self.lead(root, 'Fixed.', repo_name='unknown', folder=None,
                                             session='Opened https://github.com/{repo}/pull/7')
            transcript = root / 'transcript.md'
            transcript.write_text('---\ntitle: "x"\n\n' + transcript.read_text().replace(
                'Opened', 'project_hint: "/srv/projects/project"\nOpened'))
            self.assertEqual(self.replay(root, store)[0], dict(routed=0, proven=0, rejected=0))

    def test_a_proof_from_another_checkout_does_not_bind_a_lead_without_a_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, failure, _, _ = self.lead(root, 'Fixed.', repo_name='unknown',
                                             session='Opened https://github.com/{repo}/pull/7')
            proof = dict(id='r', lesson_id='L1', receipt_id='x', status='PASS', verification='proven')
            with patch.object(learn_replay, 'prior_proof', return_value=(root / 'other', proof)):
                counts, _, routes = self.replay(root, store)
            self.assertEqual(counts, dict(routed=1, proven=0, rejected=1))
            self.assertEqual(routes[failure['id']]['reason'], 'prior proof is in another repository')
            self.assertEqual(replay_repos.load(store), {})

    def test_a_proof_from_a_same_named_repository_of_another_owner_does_not_bind_the_lead(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, _, _, _ = self.lead(root, 'Fixed.', repo_name='unknown',
                                       session='Opened https://github.com/{repo}/pull/7')
            other = root / 'elsewhere' / 'project'
            other.mkdir(parents=True)
            subprocess.check_call(['git', '-C', str(other), 'init', '-q'])
            subprocess.check_call(['git', '-C', str(other), 'remote', 'add', 'origin',
                                   'https://github.com/elsewhere/project.git'])
            proof = dict(id='r', lesson_id='L1', receipt_id='x', status='PASS', verification='proven')
            with patch.object(learn_replay, 'prior_proof', return_value=(other, proof)):
                counts, _, _ = self.replay(root, store)
            self.assertEqual(counts, dict(routed=1, proven=0, rejected=1))
            self.assertEqual(replay_repos.load(store), {})

    def test_a_rejected_guess_does_not_bind_a_lead_to_that_repository(self):
        vacuous = ('import unittest\nclass ValueTests(unittest.TestCase):\n'
                   '    def test_value_is_positive(self):\n        pass\n')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, _, _, _ = self.lead(root, 'Fixed.', repo_name='unknown',
                                       session='Opened https://github.com/{repo}/pull/7')
            self.assertEqual(self.replay(root, store, vacuous)[0], dict(routed=1, proven=0, rejected=1))
            self.assertEqual(replay_repos.load(store), {})

    def test_a_transcript_path_replaced_by_a_fifo_is_not_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, _, _, _ = self.lead(root, 'Fixed.', session='Opened https://github.com/{repo}/pull/7')
            (root / 'transcript.md').unlink()
            os.mkfifo(root / 'transcript.md')
            self.assertEqual(self.replay(root, store)[0], dict(routed=0, proven=0, rejected=0))

    def test_a_session_pull_request_in_another_repository_is_not_routed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, _, _, _ = self.lead(root, 'Fixed.', session='Opened https://github.com/example/other/pull/7')
            self.assertEqual(self.replay(root, store)[0], dict(routed=0, proven=0, rejected=0))

    def test_a_generated_test_that_passes_before_the_fix_is_rejected_and_not_retried_that_week(self):
        vacuous = ('import unittest\nclass ValueTests(unittest.TestCase):\n'
                   '    def test_value_is_positive(self):\n        pass\n')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, failure, _, _ = self.lead(root, 'Fixed in https://github.com/{repo}/pull/7')
            counts, _, routes = self.replay(root, store, vacuous)
            self.assertEqual(counts, dict(routed=1, proven=0, rejected=1))
            self.assertIn('incident not admitted; red PASS', routes[failure['id']]['reason'])
            counts, write, _ = self.replay(root, store, vacuous)
            self.assertEqual((counts, write.call_count), (dict(routed=1, proven=0, rejected=0), 0))

    def test_a_pull_request_merged_by_merge_commit_replays_against_its_fork_point(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, failure, before, after = self.lead(root, 'Fixed in https://github.com/{repo}/pull/7', merged=True)
            counts, _, routes = self.replay(root, store)
            self.assertEqual(counts, dict(routed=1, proven=1, rejected=0))
            self.assertEqual((routes[failure['id']]['before'], routes[failure['id']]['after']), (before, after))

    def test_a_lead_proven_before_its_route_was_saved_reuses_that_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, failure, _, _ = self.lead(root, 'Fixed in https://github.com/{repo}/pull/7')
            _, _, first = self.replay(root, store)
            (store / 'transcript-replay.json').unlink()
            counts, write, routes = self.replay(root, store)
            self.assertEqual((counts, write.call_count), (dict(routed=1, proven=1, rejected=0), 0))
            self.assertEqual(routes[failure['id']]['lesson_id'], first[failure['id']]['lesson_id'])
            self.assertEqual(len(c.latest_lessons(c.load_store(store)[1])), 1)

    def test_source_and_admission_timeouts_are_bounded_by_the_replay_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, _, _, _ = self.lead(root, 'Fixed in https://github.com/{repo}/pull/7')
            with patch.object(learn_replay, 'TRANSCRIPT_SECONDS', 30), \
                    patch.object(learn_replay, 'fix_pair', wraps=learn_replay.fix_pair) as pair, \
                    patch.object(learn_replay, 'source_repo', wraps=learn_replay.source_repo) as source, \
                    patch.object(incident_admission, 'admit', wraps=incident_admission.admit) as admit:
                self.assertEqual(self.replay(root, store)[0]['proven'], 1)
            self.assertLessEqual(pair.call_args.args[3], 10)
            self.assertTrue(all(call.args[1].deadline is not None for call in source.call_args_list))
            self.assertLessEqual(admit.call_args.args[0].timeout, 10)

    def test_a_lead_that_runs_out_of_replay_time_is_not_recorded_as_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, _, _, _ = self.lead(root, 'Fixed in https://github.com/{repo}/pull/7')
            with patch.object(learn_replay, 'fix_pair', side_effect=c.ColdfixError('replay budget exhausted')), \
                    patch.object(learn_replay, 'expired', side_effect=[False, True]):
                counts, _, routes = self.replay(root, store)
            self.assertEqual((counts, routes), (dict(routed=1, proven=0, rejected=0), {}))

if __name__ == '__main__':
    unittest.main()
