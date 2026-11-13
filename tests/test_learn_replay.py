import hashlib
import inspect
import json
import os
import shlex
import socket
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import ci_fix_learning
import coldfix as c
import coldrecall
import incident_admission
import learn_replay
import learn_report
import regression_adapter
import replay_isolation
import test_learn

from datetime import datetime, timezone

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

    def test_rejection_confirmed_at_one_code_revision_waits_for_new_code_or_a_week(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = dict(runs=[], drafts={'draft': dict(verdict='PASS', evidence=['draft'])})
            (root / 'learn-drafts.json').write_text(json.dumps(ledger))
            pairs = root / 'pairs.json'
            pairs.write_text(json.dumps(dict(results=[])))

            def replays(day, revision='first'):
                with patch.object(learn_replay, 'utc_day', return_value=day), \
                        patch.object(learn_replay, 'code_revision', return_value=revision, create=True), \
                        patch.object(learn_replay.c, 'load_store', return_value=(root, {})), \
                        patch.object(learn_replay, 'prior_proof', return_value=None), \
                        patch.object(learn_replay, 'replay_candidates', return_value=[]) as replayed:
                    learn_replay.run(root, ledger, projects=root, pairs_path=pairs)
                return replayed.call_count

            self.assertEqual([replays(day) for day in ('2026-11-01', '2026-11-02', '2026-11-03', '2026-11-04')],
                             [1, 1, 0, 0])
            self.assertEqual([replays(day, 'second') for day in ('2026-11-04', '2026-11-05', '2026-11-06')], [1, 1, 0])
            self.assertEqual([replays(day, 'second') for day in ('2026-11-11', '2026-11-12', '2026-11-13')], [0, 1, 0])
            self.assertEqual(ledger['drafts']['draft']['replay']['status'], 'rejected')

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

    def replay_one_pair_on_a_clock(self, kind, matching_tests, runs, cost=100, resume=None, prepare_costs=(),
                                   generated_results=()):
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
                raise c.ColdfixError('incident not admitted; red PASS, green PASS, reversed-patch PASS ' + test_path)

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
                        patch.object(learn_replay, 'admission_args', side_effect=lambda *args: args[6]), \
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
            result = learn_replay.run(root, ledger, projects=root, pairs_path=pairs)
            self.assertEqual(result, dict(proven=0, rejected=0))
            self.assertNotIn('replay_attempts', entry)

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
                                                           before=before, after=after)])))
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
            ref = 'https://github.com/example/project/actions/runs/7#attempt-1'
            failure = c.event_record(dict(kind='ci', command='verify', repo='example/project', agent='unknown',
                error=json.dumps(dict(type='ci', head_sha=before, run_id=7,
                                      log_tail=also_failed + 'FAIL: test_value_is_positive (test_value.ValueTests)\nAssertionError')),
                source_ref=ref, occurred='2026-11-05T00:00:00Z'), 'failure-ingest')
            pair = dict(repo='example/project', red_run=7, before=before, after=after)
        else:
            ref = 'https://github.com/example/project/pull/1#discussion_r1'
            failure = c.event_record(dict(kind='review', command='PR review finding', repo='example/project',
                error='**Keep the value positive**\n\nA negative value breaks the total.', agent='codex-review',
                source_ref=ref, occurred='2026-11-05T00:00:00Z'), 'github')
            pair = dict(repo='example/project', finding_url=ref, before=before, after=after)
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

    def test_a_ci_fix_whose_recorded_test_passes_before_it_is_proven_by_a_generated_test_of_that_name(self):
        result, entry, write, lessons = self.replay_with_generated(self.GENERATED, base_test=self.VACUOUS, ci=True)
        self.assertEqual(result, dict(proven=1, rejected=1), str(entry.get('replay')))
        self.assertIn('red PASS', entry['replay_attempts'][0]['reason'])
        self.assertEqual(write.call_args.args[3], ['test_value_is_positive'])
        [lesson] = lessons
        self.assertEqual(lesson['admission']['diagnostic'], 'FAIL: test_value_is_positive')
        self.assertRegex(lesson['admission']['test_paths'][0], r'^test_coldfix_proof_[0-9a-f]{12}\.py$')

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
        self.assertRegex(lesson['admission']['test_paths'][0], r'^test_coldfix_proof_')

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

    def test_a_second_route_that_errors_keeps_the_first_reply_and_is_asked_again_alone(self):
        replies = [('placeholder', 'claude-haiku-5-5'), c.ColdfixError('route unavailable')]
        first, proven, routes, stored = self.second_route_after(replies)
        self.assertEqual(first, (False, 1, 'rejected'))
        self.assertEqual((proven, routes), (1, [0, 1, 1]))
        self.assertEqual((stored['model'], len(stored['asked']), stored['usable']), ('gpt-6-luna', 2, True))

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
            self.assertIn('generated test: incident not admitted; red PASS', entry['replay']['reason'])


class TranscriptCodeLeadReplayTests(unittest.TestCase):
    GENERATED = ReviewPairReplayTests.GENERATED

    def lead(self, root, after_mistake, thread='', named_repo='example/project', merged=False):
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
        failure = c.event_record(dict(kind='correction', command='transcript correction', repo='example/project',
            agent='claude', error=json.dumps(dict(type='transcript_correction', score=0.9, evidence=evidence)),
            source_ref='transcript:/transcripts/t.md#messages=u1,a1,u2,a2', occurred='2026-11-05T00:00:00Z',
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
            with patch.object(learn_replay, 'REPLAY_SECONDS', 30), \
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
