import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest import mock

import replay_env
import replay_isolation

PYPROJECT = '''[project]
name = "Demo_App"
dependencies = ["requests>=2", "demo-kit"]
[project.optional-dependencies]
xlsx = ["openpyxl"]
[dependency-groups]
test = ["pytest", {include-group = "other"}]
'''
KIT = '[project]\nname = "demo.kit"\ndependencies = ["flask"]\n'
REQUIREMENTS = ("# pinned\nattrs==23.1  # inline\n-e .\n-r other.txt\n./local\nfile:///x\ndemo_app\n"
                "evil @ https://pkgs/evil.tar.gz\nhashed==1 --hash=sha256:00\ngit+https://pkgs/x\nvendor/pkg\nfile:%2Fsrv%2Fcheckout\nsrc:pkg\n")
CHECK = ('import json, os, socket, sys\n'
         'import kit\n'
         'try:\n'
         '    open(os.path.join(sys.prefix, "probe"), "w")\n'
         '    writable = True\n'
         'except OSError:\n'
         '    writable = False\n'
         'online = [name for _, name in socket.if_nameindex() if name != "lo"] != []\n'
         'shared = "include-system-site-packages = true" in open(sys.prefix + "/pyvenv.cfg").read()\n'
         'ok = shared and kit.VALUE == 7 and sys.prefix == "/opt/coldfix-env" and not writable and not online\n'
         'print(json.dumps({"status": "PASS"} if ok else {"status": "FAIL", "failure": [sys.prefix, writable, online]}))\n'
         'sys.exit(0 if ok else 1)\n')


def commit(root, files):
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
    git = lambda *args: subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True,
                                       text=True).stdout.strip()
    git('init', '-q')
    git('add', '-A')
    git('-c', 'user.name=t', '-c', 'user.email=', 'commit', '-q', '-m', 'init')
    return git('rev-parse', 'HEAD')


def fake_install(calls, error=None):
    def install(env, requirements, roots, deadline):
        calls.append(requirements)
        if error is None:
            env.mkdir(parents=True)
            (env / 'pyvenv.cfg').write_text('home = /usr/bin\n', encoding='utf-8')
        return error
    return install


class ReplayEnvTests(unittest.TestCase):
    def test_spec_installs_declared_dependencies_and_mounts_local_packages_from_the_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            revision = commit(root, {'pyproject.toml': PYPROJECT, 'requirements-dev.txt': REQUIREMENTS,
                                     'packages/kit/pyproject.toml': KIT, 'packages/kit/src/kit/__init__.py': '',
                                     'tests/pyproject.toml': '[project]\ndependencies = ["skipped"]\n',
                                     'tools/vault-kit/vault_kit.py': '', 'tools/vault-kit/tests/test_kit.py': '',
                                     'docs/pyproject.toml': '[project]\ndependencies = ["sphinx"]\n',
                                     'a/b/c/pyproject.toml': '[project]\ndependencies = ["too-deep"]\n'})
            requirements, roots = replay_env.spec(root, revision, None)
        self.assertEqual(requirements, ['attrs==23.1', 'flask', 'openpyxl', 'pytest', 'requests>=2'])
        self.assertEqual(roots, ['/work', '/work/packages/kit/src', '/work/tools/vault-kit'])

    def test_build_reuses_the_cached_env_for_the_same_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            revision = commit(root / 'repo', {'requirements.txt': 'attrs\n'})
            calls = []
            with mock.patch.object(replay_env, 'install', side_effect=fake_install(calls)):
                first = replay_env.build(root / 'cache', root / 'repo', revision)
                second = replay_env.build(root / 'cache', root / 'repo', revision)
            self.assertEqual(first, second)
            self.assertIsNone(first[1])
            self.assertTrue((first[0] / 'pyvenv.cfg').is_file())
            self.assertEqual(calls, [['attrs']])

    def test_failed_install_is_host_only_with_the_error_and_not_retried_the_same_day(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            revision = commit(root / 'repo', {'requirements.txt': 'missing-package\n'})
            calls = []
            with mock.patch.object(replay_env, 'install', side_effect=fake_install(calls, 'No matching distribution')):
                self.assertEqual(replay_env.build(root / 'cache', root / 'repo', revision),
                                 (None, 'No matching distribution'))
                self.assertEqual(replay_env.build(root / 'cache', root / 'repo', revision),
                                 (None, 'No matching distribution'))
            self.assertEqual(len(calls), 1)
            self.assertEqual([path.name for path in (root / 'cache' / 'envs').iterdir() if path.is_dir()], [])

    def test_an_exhausted_replay_budget_skips_the_install(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            revision = commit(root / 'repo', {'requirements.txt': 'attrs\n'})
            calls = []
            with mock.patch.object(replay_env, 'install', side_effect=fake_install(calls)):
                env, error = replay_env.build(root / 'cache', root / 'repo', revision, deadline=time.monotonic() - 1)
        self.assertEqual((env, calls), (None, []))
        self.assertIn('replay budget exhausted', error)

    def test_installed_scripts_point_at_the_mounted_env(self):
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / 'env'
            (env / 'bin').mkdir(parents=True)
            (env / 'bin' / 'pytest').write_text(f'#!{env}/bin/python3\nimport pytest\n', encoding='utf-8')
            replay_env.mount_scripts(env)
            self.assertEqual((env / 'bin' / 'pytest').read_text(encoding='utf-8'),
                             '#!/opt/coldfix-env/bin/python3\nimport pytest\n')

    def test_no_cache_is_host_only(self):
        env, error = replay_env.build(None, Path('.'), 'HEAD')
        self.assertIsNone(env)
        self.assertIn('COLDFIX_REPLAY_CACHE', error)

    def test_env_only_adds_one_read_only_mount_and_path_entry_to_the_sandbox(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('COLDFIX_REPLAY_CACHE', None)
            plain = replay_isolation.sandbox(Path(directory), ['python3', 'check'])
            mounted = replay_isolation.sandbox(Path(directory), ['python3', 'check'], env=Path(directory) / 'env')
        path = plain.index('PATH') + 1
        self.assertEqual(mounted[path], '/opt/coldfix-env/bin:/usr/bin:/bin')
        expected = plain[:path] + [mounted[path]] + plain[path + 1:plain.index('--')]
        expected += ['--ro-bind', str(Path(directory) / 'env'), '/opt/coldfix-env', '--', 'python3', 'check']
        self.assertEqual(mounted, expected)
        self.assertIn('--unshare-all', mounted)
        self.assertIn('--clearenv', mounted)

    @unittest.skipIf(replay_isolation.unavailable() is not None, 'bwrap sandbox unavailable')
    def test_replay_imports_a_local_package_from_a_read_only_offline_env(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            revision = commit(root / 'repo', {'packages/kit/pyproject.toml': '[project]\nname = "kit"\n',
                                              'packages/kit/src/kit/__init__.py': 'VALUE = 7\n'})
            check = root / 'check.py'
            check.write_text(CHECK, encoding='utf-8')
            with mock.patch.dict(os.environ, COLDFIX_REPLAY_CACHE=str(root / 'cache')):
                os.environ.pop(replay_isolation.RUNNER, None)
                row = replay_isolation.replay_tree(root / 'repo', revision, root / 'tree', ['python3', '-I', str(check)],
                                                   '', 120)
        self.assertEqual((row['env'], row['status']), ('reconstructed', 'PASS'), json.dumps(row))
        self.assertNotIn('env_error', row)


if __name__ == '__main__':
    unittest.main()
