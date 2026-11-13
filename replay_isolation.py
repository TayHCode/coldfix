"""Run automatic local checks without host filesystem, environment, or network access."""
from contextlib import contextmanager
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile

import coldfix as c

TOOLS = '/opt/coldfix-replay'
NAMESPACE_FIX = ('on Ubuntu 23.10 or later, let bwrap create user namespaces with the AppArmor profile '
                 'in the README section "Replay sandbox"')
INSTALL_FIX = 'install bubblewrap (sudo apt install bubblewrap); ' + NAMESPACE_FIX


def cache():
    value = os.environ.get('COLDFIX_REPLAY_CACHE')
    return Path(value) if value else None


def cached_tools():
    tools = cache() / 'tools' if cache() else None
    return tools if tools is not None and tools.is_dir() else None


@contextmanager
def source_workspace(root, prefix):
    if cache() is not None:
        repos = cache() / 'repos'
        repos.mkdir(parents=True, exist_ok=True)
        yield repos
        return
    with tempfile.TemporaryDirectory(prefix=prefix, dir=root) as temporary:
        yield Path(temporary)


def sandbox(repo, argv, check=None):
    command = ['bwrap', '--die-with-parent', '--unshare-all', '--ro-bind', '/usr', '/usr',
               '--ro-bind', '/bin', '/bin', '--ro-bind', '/lib', '/lib', '--ro-bind', '/lib64', '/lib64',
               '--bind', str(repo), '/work', '--tmpfs', '/tmp', '--proc', '/proc', '--dev', '/dev',
               '--clearenv', '--setenv', 'PATH', '/usr/bin:/bin', '--setenv', 'HOME', '/tmp',
               '--setenv', 'PYTHONDONTWRITEBYTECODE', '1', '--chdir', '/work']
    if check is not None:
        command += ['--ro-bind', str(check), '/retained-check']
    if cached_tools() is not None:
        command += ['--ro-bind', str(cached_tools()), TOOLS]
    return [*command, '--', *argv]


def unavailable():
    if shutil.which('bwrap') is None:
        return 'bwrap is not installed; ' + INSTALL_FIX
    with tempfile.TemporaryDirectory(prefix='coldfix-sandbox-') as temporary:
        try:
            result = c.process(sandbox(Path(temporary), ['true']), timeout=30)
        except (OSError, subprocess.TimeoutExpired) as error:
            return f'bwrap could not start ({type(error).__name__}); ' + NAMESPACE_FIX
    if result.returncode == 0:
        return None
    lines = result.stderr.decode('utf-8', errors='replace').strip().splitlines()
    detail = lines[-1] if lines else f'exit {result.returncode}'
    return f'bwrap cannot create the sandbox ({detail}); ' + NAMESPACE_FIX


def require_sandbox():
    reason = unavailable()
    c.require(reason is None, f'sandbox unavailable: {reason}')
    return dict(sandbox='available')


def check_command(command):
    argv = shlex.split(command) if isinstance(command, str) else list(command)
    c.require(len(argv) >= 2 and Path(argv[0]).name in ('python', 'python3')
              and all(value in ('-I', '-S') for value in argv[1:-1]), 'unsupported isolated replay command')
    check = c.ordinary_path(argv[-1])
    c.require(check.is_file() and not check.is_symlink(), 'isolated replay check unavailable')
    return ['python3', *argv[1:-1], '/retained-check'], check


def replay_tree(repo, revision, scratch, command, expected_failure, timeout, reverse_patch=None):
    require_sandbox()
    return c.replay_tree(repo, revision, scratch, command, expected_failure, timeout, reverse_patch,
                         isolated=True)


def witness_process(command, cwd=None, timeout=None):
    require_sandbox()
    with tempfile.TemporaryDirectory(prefix='coldfix-witness-') as temporary:
        copy = Path(temporary) / 'tree'
        shutil.copytree(cwd, copy, symlinks=True)
        return c.process(sandbox(copy, command), timeout=timeout)
