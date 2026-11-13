import hashlib
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import ci_fix_learning as ci
import coldfix as c
import ci_vm_executor as vm


class VMExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        subprocess.run(['git', '-C', str(self.repo), '-c', 'user.name=Fixture',
                        '-c', 'user.email=fixture@example.invalid', 'commit', '--allow-empty', '-qm', 'fixture'], check=True)
        self.sha = subprocess.check_output(['git', '-C', str(self.repo), 'rev-parse', 'HEAD'], text=True).strip()
        self.check = self.root / 'retained.py'
        self.check.write_text('print(1)\n')
        self.calls = []
        self.receipt = dict(name='', owner='coldfix', cleanup='gone', verdict='PASS', guest_exit=0)
        self.row = dict(commit=self.sha, tree='b' * 40, status='PASS', exit=0,
                        stdout='{"status":"PASS"}\n', stderr='')

    def transport(self, args, **kwargs):
        self.calls.append(args)
        if '--name' in args:
            self.receipt['name'] = args[args.index('--name') + 1]
            return subprocess.CompletedProcess(args, 0, b'', b'Evidence: /var/lib/vm02/jobs/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa.json\n')
        data = (json.dumps(self.receipt).encode() if args[-1].endswith('.json') else
                b'COLDFIX_VM_LOG_SHA256 ' + hashlib.sha256(b'').hexdigest().encode() + b'\n' +
                b'COLDFIX_VM_RESULT ' + json.dumps(self.row).encode() + b'\n')
        return subprocess.CompletedProcess(args, 0, data, b'')

    def invoke(self):
        return vm.replay_tree(self.repo, self.sha, self.root / 'red',
                              'python3 -I -S ' + str(self.check), ci.EXPECTED, 10)

    def test_guest_execution_uses_host_lifecycle_and_confirms_cleanup(self):
        with patch.object(vm, 'remote', side_effect=self.transport), patch.object(c, 'replay_tree', side_effect=AssertionError('host execution')):
            row = self.invoke()
        self.assertEqual(row['status'], 'PASS')
        self.assertEqual(self.calls[0][:6], ['sudo', '-n', 'python3', '-B',
                                             '/usr/local/lib/vm02/lifecycle.py', 'run'])
        self.assertEqual(self.calls[0][self.calls[0].index('--owner') + 1], 'coldfix')
        self.assertIn('--no-same-owner', self.calls[0][-1])
        self.assertIn('python3', self.calls[0][-1])
        self.assertEqual(self.calls[1][:3], ['sudo', '-n', 'cat'])
        self.assertTrue(self.calls[1][-1].endswith('.json'))

    def test_configured_host_and_launcher_are_used(self):
        with patch.dict('os.environ', {'COLDFIX_VM_HOST': 'guest-host',
                                       'COLDFIX_VM_RUN_COMMAND': '/opt/vm/run-command.sh'}):
            with patch.object(vm.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
                vm.remote(['true'], timeout=10)
            self.assertEqual(run.call_args.args[0][3], 'guest-host')
            with patch.object(vm, 'remote', side_effect=self.transport):
                self.assertEqual(self.invoke()['status'], 'PASS')
            self.assertIn('/opt/vm/run-command.sh', self.calls[0])

    def test_local_host_invokes_the_lifecycle_directly(self):
        with patch.dict('os.environ', {'COLDFIX_VM_HOST': 'local'}):
            with patch.object(vm.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
                vm.remote(['sudo', '-n', 'cat', '/receipt'], timeout=10)
        self.assertEqual(run.call_args.args[0], ['sudo', '-n', 'cat', '/receipt'])

    def test_lifecycle_job_records_the_replayed_repo_and_exact_commit(self):
        subprocess.run(['git', '-C', str(self.repo), 'remote', 'add', 'origin',
                        'https://github.com/example/app.git'], check=True)
        with patch.object(vm, 'remote', side_effect=self.transport):
            self.assertEqual(vm.replay_tree(self.repo, 'HEAD', self.root / 'red',
                                            'python3 -I -S ' + str(self.check), ci.EXPECTED, 10)['status'], 'PASS')
        job = self.calls[0][:self.calls[0].index('--')]
        self.assertEqual(job[job.index('--repo') + 1], 'example/app')
        self.assertEqual(job[job.index('--commit') + 1], self.sha)

    def test_lifecycle_job_omits_repo_without_a_github_origin(self):
        with patch.object(vm, 'remote', side_effect=self.transport):
            self.invoke()
        job = self.calls[0][:self.calls[0].index('--')]
        self.assertNotIn('--repo', job)
        self.assertEqual(job[job.index('--commit') + 1], self.sha)

    def test_unparseable_origin_omits_repo_instead_of_aborting(self):
        subprocess.run(['git', '-C', str(self.repo), 'remote', 'add', 'origin', 'https://['], check=True)
        with patch.object(vm, 'remote', side_effect=self.transport):
            self.assertEqual(self.invoke()['status'], 'PASS')
        job = self.calls[0][:self.calls[0].index('--')]
        self.assertNotIn('--repo', job)
        self.assertEqual(job[job.index('--commit') + 1], self.sha)

    def test_inherited_git_configuration_cannot_break_the_payload_fetch(self):
        inherited = dict(GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='protocol.file.allow', GIT_CONFIG_VALUE_0='never')
        with patch.dict('os.environ', inherited), patch.object(vm, 'remote', side_effect=self.transport):
            row = self.invoke()
        self.assertEqual(row['status'], 'PASS')

    def test_requested_python_is_resolved_on_the_guest_not_the_caller(self):
        version = '3.99'
        setup = f'      - uses: actions/setup-python@v5\n        with:\n          python-version: "{version}"\n'
        from test_ci_fix_learning import WORKFLOW, job
        content = WORKFLOW.replace('      - name: Test', setup + '      - name: Test')
        with patch.object(ci.shutil, 'which', return_value=None):
            plan = ci.workflow_plan(content, job())
        self.assertEqual(plan['runtime_name'], 'python' + version)
        self.assertIn("'python3.99'", ci.check_script(plan))

    def test_timeout_and_transport_errors_stop_replay_admission(self):
        for error in (OSError('offline'), subprocess.TimeoutExpired('ssh', 10)):
            with self.subTest(error=type(error).__name__):
                with patch.object(vm, 'remote', side_effect=error):
                    with self.assertRaises(vm.CleanupFailed):
                        self.invoke()

    def test_command_path_selects_VM_for_probe_and_red_and_green(self):
        plan = dict(command=['python3', '-m', 'unittest'], kind='test')
        with patch.object(ci, 'vm_replay_tree', return_value=self.row) as execute, patch.object(c, 'replay_tree', side_effect=AssertionError('local execution')):
            proof = ci.replay_pair(self.repo, self.sha, 'c' * 40, plan, self.root, 10)
        self.assertEqual(proof['before']['status'], 'PASS')
        self.assertEqual(execute.call_count, 2)

    def test_cleanup_failure_cannot_return_proof(self):
        self.receipt['cleanup'] = 'pending'
        with patch.object(vm, 'remote', side_effect=self.transport):
            with self.assertRaises(vm.CleanupFailed):
                self.invoke()

    def test_truncated_guest_log_cannot_return_proof(self):
        def truncated(args, **kwargs):
            result = self.transport(args, **kwargs)
            if args[-1].endswith('.log'):
                result.stdout = b'COLDFIX_GUEST_LOG truncated\n' + result.stdout
            return result
        with patch.object(vm, 'remote', side_effect=truncated):
            with self.assertRaisesRegex(vm.CleanupFailed, 'VM output incomplete'):
                self.invoke()

    def test_guest_failure_with_confirmed_cleanup_is_incomplete(self):
        self.receipt.update(verdict='INCOMPLETE', guest_exit=None)
        with patch.object(vm, 'remote', side_effect=self.transport):
            self.assertEqual(self.invoke()['status'], 'INCOMPLETE')
        self.assertEqual(len(self.calls), 2)

    def test_guest_launcher_does_not_use_the_caller_interpreter_name(self):
        requests = []
        def transport(args, stdin=None, **kwargs):
            if stdin is not None:
                with tarfile.open(fileobj=stdin) as archive:
                    requests.append(json.load(archive.extractfile('request.json')))
            return self.transport(args, **kwargs)
        with patch.object(vm, 'remote', side_effect=transport):
            vm.replay_tree(self.repo, self.sha, self.root / 'red',
                           '/opt/caller/bin/python3.11 -I -S ' + str(self.check), ci.EXPECTED, 10)
        self.assertEqual(requests[0]['argv'], ['python3', '-I', '-S'])

    def test_non_object_lifecycle_receipt_stops_replay_admission(self):
        for value in (b'null', b'[]', b'"gone"'):
            def transport(args, **kwargs):
                if '--name' in args:
                    return subprocess.CompletedProcess(args, 0, b'', b'Evidence: /var/lib/vm02/jobs/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa.json\n')
                return subprocess.CompletedProcess(args, 0, value, b'')
            with self.subTest(receipt=value), patch.object(vm, 'remote', side_effect=transport):
                with self.assertRaises(vm.CleanupFailed):
                    self.invoke()

    def test_missing_receipt_stops_replay_admission(self):
        with patch.object(vm, 'remote', return_value=subprocess.CompletedProcess([], 125, b'', b'offline')):
            with self.assertRaises(vm.CleanupFailed):
                self.invoke()

    def test_promotion_uses_the_same_VM_executor_as_discovery(self):
        from tests import test_ci_fix_learning as fixtures
        fixture = fixtures.LocalProofTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        local = ci.vm_replay_tree.side_effect
        with patch.object(ci, 'vm_replay_tree', side_effect=local) as execute:
            result = fixture.attempt()
        self.assertEqual(result['lesson']['verification'], 'proven')
        self.assertEqual(execute.call_count, 6)

    def test_cleanup_failure_stops_the_command_instead_of_advancing_to_another_repo(self):
        with patch.object(ci, 'execute', side_effect=vm.CleanupFailed('cleanup unconfirmed')) as execute:
            with patch('sys.stderr'):
                self.assertEqual(ci.main(['--repo', 'example/project']), 2)
        execute.assert_called_once()

    def test_shallow_source_transfers_a_fetchable_selected_commit(self):
        subprocess.run(['git', '-C', str(self.repo), '-c', 'user.name=Fixture',
                        '-c', 'user.email=fixture@example.invalid', 'commit', '--allow-empty', '-qm', 'green'], check=True)
        shallow = self.root / 'shallow'
        subprocess.run(['git', 'clone', '-q', '--depth=1', '--no-checkout', self.repo.as_uri(), str(shallow)], check=True)
        self.repo = shallow
        self.sha = subprocess.check_output(['git', '-C', str(shallow), 'rev-parse', 'HEAD'], text=True).strip()
        def transfer(args, **kwargs):
            if '--name' in args:
                import tarfile
                extracted = self.root / 'transferred'
                extracted.mkdir()
                with tarfile.open(fileobj=kwargs['stdin']) as archive:
                    archive.extractall(extracted, filter='data')
                checkout = self.root / 'guest-checkout'
                subprocess.run(['git', 'init', '-q', str(checkout)], check=True)
                result = subprocess.run(['git', '-C', str(checkout), 'fetch', '-q', str(extracted / 'repo'), self.sha], capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
            return self.transport(args, **kwargs)
        with patch.object(vm, 'remote', side_effect=transfer):
            self.invoke()

    def test_reversed_patch_runs_against_the_transferred_green_tree(self):
        target = self.repo / 'value'
        target.write_text('before\n')
        git = ['git', '-C', str(self.repo)]
        subprocess.run(git + ['add', 'value'], check=True)
        subprocess.run(git + ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                              'commit', '-qm', 'before'], check=True)
        target.write_text('after\n')
        reverse = self.root / 'change.patch'
        reverse.write_bytes(subprocess.check_output(git + ['diff']))
        subprocess.run(git + ['add', 'value'], check=True)
        subprocess.run(git + ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                              'commit', '-qm', 'after'], check=True)
        self.sha = subprocess.check_output(git + ['rev-parse', 'HEAD'], text=True).strip()
        self.check.write_text('import json, pathlib, sys\n'
                              'ok = pathlib.Path("value").read_text() == "after\\n"\n'
                              'print(json.dumps({"status": "PASS"} if ok else '
                              '{"status": "FAIL", "failure": "fixture"}))\n'
                              'sys.exit(0 if ok else 1)\n')

        def transport(args, stdin=None, **kwargs):
            if stdin is not None:
                guest = self.root / 'guest'
                guest.mkdir()
                with tarfile.open(fileobj=stdin) as archive:
                    archive.extractall(guest, filter='data')
                result = subprocess.run(['python3', str(guest / 'guest.py'), str(guest)],
                                        capture_output=True, check=True)
                self.row = json.loads(result.stdout.split(b'COLDFIX_VM_RESULT ')[1])
            return self.transport(args, **kwargs)

        with patch.object(vm, 'remote', side_effect=transport):
            row = vm.replay_tree(self.repo, self.sha, self.root / 'red',
                                 'python3 -I -S ' + str(self.check), 'fixture', 10, reverse)
        self.assertEqual(row['status'], 'FAIL')
        self.assertEqual(row['exit'], 1)
