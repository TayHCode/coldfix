import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

import coldfix as c

MOUNT = '/opt/coldfix-env'
PYTHON = shutil.which('python3', path=os.defpath) or 'python3'
BUILD_SECONDS = 900
RETRY_SECONDS = 86400
PROJECT_FILES = ('pyproject.toml', 'setup.py', 'setup.cfg')
TEST_DIRS = {'test', 'tests'}
SKIPPED_DIRS = TEST_DIRS | {'doc', 'docs', 'example', 'examples', 'fixtures', 'e2e-tests'}
REQUIREMENTS = re.compile(r'requirements[^/]*\.txt')
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*')
ERRORS = (c.ColdfixError, AttributeError, ImportError, OSError, UnicodeError, ValueError, TypeError, subprocess.SubprocessError)


def normalized(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def project_dirs(paths):
    dirs = set()
    for path in paths:
        parts = path.split('/')
        ends = [len(parts) - 1] if parts[-1] in PROJECT_FILES or REQUIREMENTS.fullmatch(parts[-1]) else []
        ends += [index for index, part in enumerate(parts[:-1]) if part in TEST_DIRS]
        for end in ends:
            if end <= 2 and not any(part in SKIPPED_DIRS or part.startswith('.') for part in parts[:end]):
                dirs.add('/'.join(parts[:end]))
    return sorted(dirs)


def requirement_lines(text):
    for line in text.splitlines():
        line = re.sub(r'(^|\s)#.*', '', line).strip()
        if NAME.match(line) and not re.search(r'[@/\\:%]|\s-|\{root', line):
            yield line


def pyproject_requirements(text):
    import tomllib
    data = tomllib.loads(text)
    project = data.get('project') or {}
    groups = [project.get('dependencies'), *(project.get('optional-dependencies') or {}).values(),
              *(data.get('dependency-groups') or {}).values()]
    found = [value for group in groups if isinstance(group, list) for value in group if isinstance(value, str)]
    name = project.get('name')
    return (normalized(name) if isinstance(name, str) else None), found


def spec(repo, revision, deadline):
    git = lambda *args: c.privacy_git(repo, *args, timeout=c.remaining_timeout(60, deadline))
    paths = set(git('ls-tree', '-r', '--name-only', '-z', revision).decode('utf-8', 'replace').split('\0')) - {''}
    show = lambda path: git('show', f'{revision}:{path}').decode('utf-8')
    local, wanted, roots = set(), set(), []
    for directory in project_dirs(paths):
        prefix = directory + '/' if directory else ''
        if prefix + 'pyproject.toml' in paths:
            name, found = pyproject_requirements(show(prefix + 'pyproject.toml'))
            local.add(name)
            wanted.update(requirement_lines('\n'.join(found)))
        for path in sorted(paths):
            if path.startswith(prefix) and REQUIREMENTS.fullmatch(path[len(prefix):]):
                wanted.update(requirement_lines(show(path)))
        base = '/work/' + directory if directory else '/work'
        roots.append(base + '/src' if any(path.startswith(prefix + 'src/') for path in paths) else base)
    requirements = sorted(line for line in wanted if (match := NAME.match(line)) and normalized(match[0]) not in local)
    return requirements, roots


VERSION = []


def python_version(deadline):
    if not VERSION:
        VERSION.append(subprocess.run([PYTHON, '-I', '-c', 'import sys; print(sys.version)'], capture_output=True, check=True,
                                      timeout=c.remaining_timeout(30, deadline)).stdout.decode('ascii', 'replace').strip())
    return VERSION[0]


def mount_scripts(env):
    staged = str(env).encode()
    for script in (env / 'bin').iterdir():
        if script.is_file() and not script.is_symlink() and (text := script.read_bytes()).startswith(b'#!') and staged in text:
            script.write_bytes(text.replace(staged + b'/bin/', MOUNT.encode() + b'/bin/'))


def install(env, requirements, roots, deadline):
    environment = dict(PATH=os.defpath, HOME=os.environ.get('HOME', '/tmp'), PIP_CONFIG_FILE=os.devnull,
                       PIP_DISABLE_PIP_VERSION_CHECK='1', PIP_NO_INPUT='1')
    result = c.process([PYTHON, '-I', '-m', 'venv', '--system-site-packages', str(env)], cwd=env.parent,
                       timeout=c.remaining_timeout(60, deadline), env=environment)
    if result.returncode == 0 and requirements:
        listed = env.parent / 'requirements.txt'
        listed.write_text('\n'.join(requirements) + '\n', encoding='utf-8')
        result = c.process([str(env / 'bin' / 'python3'), '-I', '-m', 'pip', 'install', '--quiet', '--only-binary=:all:',
                            '-r', str(listed)], cwd=env.parent, timeout=c.remaining_timeout(BUILD_SECONDS, deadline), env=environment)
    if result.returncode:
        lines = result.stderr.decode('utf-8', 'replace').strip().splitlines()[-5:]
        return ('\n'.join(lines) or f'exit {result.returncode}')[-1000:]
    mount_scripts(env)
    site = next(env.glob('lib/python*/site-packages'))
    (site / 'coldfix-work.pth').write_text(''.join(root + '\n' for root in roots), encoding='utf-8')
    return None


def build(cache, repo, revision, deadline=None):
    if cache is None:
        return None, 'no COLDFIX_REPLAY_CACHE to hold the replay environment'
    try:
        requirements, roots = spec(repo, revision, deadline)
        key = hashlib.sha256(json.dumps(['system-site', python_version(deadline), requirements, roots]).encode()).hexdigest()[:32]
        envs = cache / 'envs'
        env, failed = envs / key, envs / (key + '.error')
        if (env / 'pyvenv.cfg').is_file():
            return env, None
        if failed.is_file() and time.time() - failed.stat().st_mtime < RETRY_SECONDS:
            return None, failed.read_text(encoding='utf-8')
        envs.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.build-', dir=envs) as temporary:
            staged = Path(temporary) / 'env'
            error = install(staged, requirements, roots, deadline)
            if error is not None:
                if (env / 'pyvenv.cfg').is_file():
                    return env, None
                c.atomic_create(failed, error, replace=failed.exists())
                return None, error
            try:
                staged.rename(env)
            except OSError:
                if not (env / 'pyvenv.cfg').is_file():
                    raise
        return env, None
    except ERRORS as error:
        return None, f'{type(error).__name__}: {error}'[:1000]
