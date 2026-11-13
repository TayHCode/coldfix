"""Execute retained CI checks in owned disposable Incus VMs on `COLDFIX_VM_HOST`."""

import hashlib
import json
import math
import os
import re
from pathlib import Path
import shlex
import subprocess
import tarfile
import tempfile
import uuid

import coldfix as c
from verify_pairs import github_slug


GUEST = r'''
import hashlib, json, os, shlex, subprocess, sys, time
from pathlib import Path
import coldfix as c

root = Path(sys.argv[1])
request = json.loads((root / "request.json").read_text())
check = root / "check"
script = check.read_text()
os.environ.clear()
os.environ.update(PATH="/usr/local/bin:/usr/bin:/bin", HOME=str(root), LANG="C.UTF-8",
                  LC_ALL="C.UTF-8", PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1", CI="1",
                  GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null", GIT_TERMINAL_PROMPT="0",
                  COLDFIX_GUEST_LOG=str(root / "command.log"))
command = shlex.join([*request["argv"], str(check)])
for step in request["provision"]:
    if subprocess.run(step, stdin=subprocess.DEVNULL).returncode:
        row = dict(commit='', tree='', status='INCOMPLETE', exit=None, stdout='', stderr='Guest provisioning failed')
        break
else:
    row = c.replay_tree(root / "repo", request["revision"], root / "work", command,
                        request["expected"], request["timeout"],
                        root / "reverse.patch" if request["reverse"] else None)
if not c.check_unchanged(check, script):
    row.update(status="INCOMPLETE", stdout='', stderr='Authored check changed')
log = root / "command.log"
digest = hashlib.sha256()
# Provisioning programs may leave stdout without a trailing newline.
print('', flush=True)
if log.is_file():
    with log.open('rb') as source:
        for line in source:
            data = ('COLDFIX_GUEST_LOG ' + line.decode('utf-8', 'backslashreplace').rstrip('\n') + '\n').encode('utf-8')
            digest.update(data)
            sys.stdout.buffer.write(data)
print('COLDFIX_VM_LOG_SHA256 ' + digest.hexdigest())
print('COLDFIX_VM_RESULT ' + json.dumps(row))
sys.stdout.flush()
# Give the Incus exec transport time to drain before the guest process exits.
# The host verifies the digest and fails closed if output is still lost.
time.sleep(3)
'''


class CleanupFailed(RuntimeError):
    pass


def remote(args, timeout, stdin=None):
    host = os.environ.get('COLDFIX_VM_HOST', 'local')
    command = args if host == 'local' else ['ssh', '-o', 'BatchMode=yes', host, shlex.join(args)]
    return subprocess.run(command,
                          stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)


def replay_tree(repo, revision, scratch, command, expected_failure, timeout, reverse_patch=None, *, receipts=None, provision=()):
    row = dict(commit='', tree='', status='INCOMPLETE', exit=None, stdout='', stderr='VM replay unavailable')
    argv = shlex.split(command)
    c.require(len(argv) == 4 and argv[1:3] == ['-I', '-S'], 'unsupported VM replay command')
    check = Path(argv[-1])
    instance = 'run-coldfix-' + uuid.uuid4().hex
    guest = '/tmp/' + instance
    try:
        with tempfile.TemporaryDirectory(prefix='.vm-', dir=Path(scratch).parent) as name:
            payload = Path(name)
            bare = payload / 'repo'
            env = dict(PATH=os.defpath, HOME=name, LANG='C.UTF-8', LC_ALL='C.UTF-8', GIT_CONFIG_NOSYSTEM='1',
                       GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT='0')
            for args in (['git', 'init', '--bare', '--quiet', str(bare)],
                         ['git', '-C', str(bare), 'fetch', '--quiet', '--depth=1', '--no-tags', '--', str(repo), revision]):
                result = c.process(args, timeout=timeout, env=env)
                if result.returncode:
                    return row
            identity = []
            origin = c.process(['git', '-C', str(repo), 'remote', 'get-url', 'origin'], timeout=timeout, env=env)
            try:
                slug = github_slug(origin.stdout.decode('utf-8', 'replace').strip()) if not origin.returncode else None
            except ValueError:
                slug = None
            if slug and re.fullmatch(r'[A-Za-z0-9-]+/[A-Za-z0-9._-]+', slug):
                identity += ['--repo', slug]
            fetched = c.process(['git', '-C', str(bare), 'rev-parse', '--verify', 'FETCH_HEAD^{commit}'],
                                timeout=timeout, env=env).stdout.decode('ascii', 'replace').strip()
            if re.fullmatch(r'[0-9a-f]{40}', fetched):
                identity += ['--commit', fetched]
            request = dict(revision=revision, argv=['python3', '-I', '-S'],
                           expected=expected_failure, timeout=timeout, reverse=reverse_patch is not None,
                           provision=provision)
            (payload / 'request.json').write_text(json.dumps(request))
            (payload / 'guest.py').write_text(GUEST)
            (payload / 'check').write_bytes(check.read_bytes())
            if reverse_patch is not None:
                (payload / 'reverse.patch').write_bytes(Path(reverse_patch).read_bytes())
            archive = payload / 'payload.tar'
            with tarfile.open(archive, 'w') as output:
                paths = [bare, payload / 'request.json', payload / 'guest.py', payload / 'check', Path(c.__file__)]
                if reverse_patch is not None:
                    paths.append(payload / 'reverse.patch')
                for path in paths:
                    output.add(path, arcname=path.name)
            ttl = max(1, math.ceil(timeout * 3))
            configured_launcher = os.environ.get('COLDFIX_VM_RUN_COMMAND')
            launcher = (['bash', configured_launcher] if configured_launcher is not None else
                        ['python3', '-B', '/usr/local/lib/vm02/lifecycle.py', 'run'])
            job = ['sudo', '-n', *launcher,
                   '--owner', 'coldfix', '--name', instance, '--ttl', str(ttl),
                   '--disk-request', '40GiB', '--disk-reserve', '20GiB',
                   '--ready-timeout', str(max(1, math.ceil(timeout))), *identity, '--', 'sh', '-ec',
                   shlex.join(['mkdir', '-m', '700', guest]) + '; '
                   + shlex.join(['tar', '--no-same-owner', '-xf', '-', '-C', guest]) + '; '
                   + shlex.join(['python3', guest + '/guest.py', guest])]
            try:
                with archive.open('rb') as retained:
                    result = remote(job, timeout=ttl + 60, stdin=retained)
                match = re.search(rb'^Evidence: (/var/lib/vm02/jobs/[0-9a-f-]{36}\.json)$', result.stderr, re.M)
                if not match:
                    raise CleanupFailed('VM lifecycle receipt unavailable for ' + instance + '; stop replay admission')
                receipt_path = match[1].decode('ascii')
                receipt_result = remote(['sudo', '-n', 'cat', receipt_path], timeout=timeout)
                receipt = json.loads(receipt_result.stdout)
                if (receipt_result.returncode or not isinstance(receipt, dict) or receipt.get('name') != instance
                        or receipt.get('owner') != 'coldfix' or receipt.get('cleanup') != 'gone'):
                    raise CleanupFailed('VM cleanup unconfirmed for ' + instance + '; stop replay admission')
                if receipts is not None:
                    receipts.append(receipt_path)
                if result.returncode or receipt.get('verdict') != 'PASS' or receipt.get('guest_exit') != 0:
                    return row
                logs = remote(['sudo', '-n', 'cat', receipt_path[:-5] + '.log'], timeout=timeout)
                if logs.returncode:
                    return row
                output_lines = logs.stdout.split(b'\n')
                log_lines = b''.join(line + b'\n' for line in output_lines if line.startswith(b'COLDFIX_GUEST_LOG '))
                digests = [line.removeprefix(b'COLDFIX_VM_LOG_SHA256 ') for line in output_lines
                           if line.startswith(b'COLDFIX_VM_LOG_SHA256 ')]
                if len(digests) != 1 or digests[0] != hashlib.sha256(log_lines).hexdigest().encode('ascii'):
                    raise CleanupFailed('VM output incomplete for ' + instance + '; stop replay admission')
                lines = [line.removeprefix(b'COLDFIX_VM_RESULT ') for line in output_lines
                         if line.startswith(b'COLDFIX_VM_RESULT ')]
                if len(lines) != 1:
                    raise CleanupFailed('VM output incomplete for ' + instance + '; stop replay admission')
                value = json.loads(lines[0])
                c.require(isinstance(value, dict) and set(value) == set(row), 'invalid VM replay response')
                c.require(value['status'] in ('PASS', 'FAIL', 'INCOMPLETE'), 'invalid VM replay status')
                c.require(value['status'] == c.check_status(value['exit'], value['stdout'], expected_failure)
                          or value['status'] == 'INCOMPLETE', 'invalid VM replay result')
                row = value
            except (OSError, subprocess.TimeoutExpired, ValueError):
                raise CleanupFailed('VM lifecycle interrupted for ' + instance + '; stop replay admission') from None
    except (OSError, subprocess.TimeoutExpired, ValueError, c.ColdfixError):
        return row
    return row
