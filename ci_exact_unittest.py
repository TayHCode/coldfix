"""Run an unchanged unittest command and retain matching test identities."""

import shlex

import ci_fix_learning as ci


CHECK = r'''
import hashlib, json, os, re, shutil, subprocess, sys, tempfile
from pathlib import Path

def finish(status, reason, diagnostic='', tests=None):
    print(json.dumps({'status': status, **({'failure': expected} if status == 'FAIL' else {})}))
    print(json.dumps({'reason': reason, 'diagnostic': diagnostic, 'tests': tests or []}), file=sys.stderr)
    sys.exit({'PASS': 0, 'FAIL': 1, 'INCOMPLETE': 2}[status])

def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()

runtime = shutil.which(runtime_name, path='/usr/local/bin:/usr/bin:/bin')
if not runtime:
    finish('INCOMPLETE', 'local tool unavailable')
try:
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], stderr=subprocess.DEVNULL)
    tree_changes = subprocess.check_output(['git', 'diff', '--binary', 'HEAD'], stderr=subprocess.DEVNULL)
    with tempfile.TemporaryDirectory(prefix='coldfix-exact-', dir=os.environ['HOME']) as home:
        bindir = Path(home) / 'bin'
        bindir.mkdir()
        for alias in ('python', 'python3'):
            (bindir / alias).symlink_to(runtime)
        env = dict(PATH=str(bindir) + ':/usr/local/bin:/usr/bin:/bin', HOME=home,
                   LANG='C.UTF-8', LC_ALL='C.UTF-8', CI='1', PYTHONNOUSERSITE='1',
                   PYTHONDONTWRITEBYTECODE='1', GIT_CONFIG_NOSYSTEM='1',
                   GIT_CONFIG_GLOBAL='/dev/null', GIT_TERMINAL_PROMPT='0')
        path = os.environ.get('COLDFIX_GUEST_LOG')
        output = open(path, 'w+b') if path else tempfile.TemporaryFile()
        with output:
            if setup_commands:
                venv = Path(home) / 'venv'
                if subprocess.run([runtime, '-m', 'venv', str(venv)], stdin=subprocess.DEVNULL,
                                  stdout=output, stderr=subprocess.STDOUT, env=env).returncode:
                    finish('INCOMPLETE', 'dependency, service, secret or infrastructure unavailable')
                env['PATH'] = str(venv / 'bin') + ':' + env['PATH']
                for setup in setup_commands:
                    if subprocess.run(setup, stdin=subprocess.DEVNULL, stdout=output,
                                      stderr=subprocess.STDOUT, env=env).returncode:
                        finish('INCOMPLETE', 'dependency, service, secret or infrastructure unavailable')
            start = output.tell()
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output,
                                       stderr=subprocess.STDOUT, env=env)
            code = process.wait()
            output.seek(start)
            text = output.read().decode('utf-8', 'replace')
    if (revision != subprocess.check_output(['git', 'rev-parse', 'HEAD'], stderr=subprocess.DEVNULL)
            or tree_changes != subprocess.check_output(['git', 'diff', '--binary', 'HEAD'], stderr=subprocess.DEVNULL)):
        finish('INCOMPLETE', 'dependency, service, secret or infrastructure unavailable')
except (OSError, subprocess.CalledProcessError):
    finish('INCOMPLETE', 'local tool unavailable')

match = re.search(r'(?m)^Ran (\d+) tests? in ', text)
if not match or int(match[1]) == 0 or re.search(r'(?m)^ERROR: |expected failures=|unexpected successes=', text):
    finish('INCOMPLETE', 'structured test results unavailable')
failures = sorted({digest(item) for item in re.findall(r'(?m)^FAIL: (.+)$', text)})
passed = {digest(item) for item in re.findall(r'(?m)^(\w+ \([\w.]+\))(?: \.\.\. ok|\n[^\n]* \.\.\. ok)\s*$', text)}
if code == 0:
    if failures or not re.search(r'(?m)^OK(?: \([^\n]*\))?\s*$', text):
        finish('INCOMPLETE', 'structured test results unavailable')
    if known_tests and not set(known_tests) <= passed:
        finish('INCOMPLETE', 'failed test did not pass after fix')
    finish('PASS', 'local command passed', tests=known_tests)
if code != 1 or not failures:
    finish('INCOMPLETE', 'unexpected local exit')
if known_tests and set(known_tests) != set(failures):
    finish('INCOMPLETE', 'failed test identity changed')
match = re.search(r'(?m)^\s*AssertionError[^\n]*', text)
if not match:
    finish('INCOMPLETE', 'unrecognized failure diagnostic')
diagnostic = digest(' '.join(match[0].split()))
if known and known != diagnostic:
    finish('INCOMPLETE', 'diagnostic differs from retained failure')
finish('FAIL', 'known local diagnostic', diagnostic, failures)
'''


def check_script(plan, known=None, known_tests=None):
    command = shlex.split(plan['source_command'])
    if (plan['kind'] != 'test' or command[:3] not in
            (['python', '-m', 'unittest'], ['python3', '-m', 'unittest'])
            or not any(arg in ('-v', '--verbose') for arg in command)):
        raise ci.Skipped('exact unittest workflow requires a verbose command')
    setup = plan.get('setup', [])
    setup_sources = plan.get('setup_commands', [])
    if len(setup) != len(setup_sources) or any(ci.pip_install(raw) != args for raw, args in zip(setup_sources, setup)):
        raise ci.Skipped('exact preparation commands unavailable')
    values = dict(command=command, runtime_name=plan.get('runtime_name', 'python3'),
                  setup_commands=[shlex.split(raw) for raw in setup_sources],
                  expected=ci.EXPECTED, known=known, known_tests=known_tests or [])
    return '\n'.join(key + ' = ' + repr(value) for key, value in values.items()) + '\n' + CHECK
