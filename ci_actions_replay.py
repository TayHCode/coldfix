"""Replay a supported Actions job inside a disposable VM using act."""

import itertools
import json
import re
import shlex
from pathlib import Path

import ci_fix_learning as ci
import ci_workflow_replay as replay

ACT_VERSION = 'v0.2.97'
ACT_SHA256 = '67b4976a56f33ea7870b798912e738ec44eef38e7578ae7b532f2def1c73a5ad'
ACT_PROVISION = [['sh', '-ec',
    'curl -fsSL https://github.com/nektos/act/releases/download/' + ACT_VERSION +
    '/act_Linux_x86_64.tar.gz -o /tmp/coldfix-act.tar.gz; printf "%s  %s\\n" ' +
    ACT_SHA256 + ' /tmp/coldfix-act.tar.gz | sha256sum -c -; '
    'tar -xzf /tmp/coldfix-act.tar.gz -C /usr/local/bin act']]
MATRIX = re.compile(r'\$\{\{\s*matrix\.([A-Za-z_][\w-]*)\s*\}\}')
ACTION = re.compile(r'actions/(?:checkout|setup-python|setup-node|setup-go|setup-java)@(?:v\d+|[a-f0-9]{40})\Z')


def matrix_rows(job):
    strategy = job.get('strategy', {})
    if not isinstance(strategy, dict):
        raise ci.Skipped('unsupported job strategy')
    matrix = strategy.get('matrix', {})
    if not isinstance(matrix, dict) or 'exclude' in matrix:
        raise ci.Skipped('unsupported matrix')
    if 'include' in matrix:
        if len(matrix) != 1 or not isinstance(matrix['include'], list):
            raise ci.Skipped('unsupported matrix include')
        rows = matrix['include']
    else:
        if any(not isinstance(v, list) or not v for v in matrix.values()):
            raise ci.Skipped('unsupported matrix axes')
        rows = [dict(zip(matrix, values)) for values in itertools.product(*matrix.values())]
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise ci.Skipped('unsupported matrix entry')
        values = {}
        for key, value in row.items():
            if not re.fullmatch(r'[A-Za-z_][\w-]*', str(key)) or isinstance(value, (dict, list, bool)):
                raise ci.Skipped('unsupported matrix value')
            value = str(value)
            if not re.fullmatch(r'[\w.+-]+', value):
                raise ci.Skipped('unsupported matrix value')
            values[str(key)] = value
        result.append(values)
    return result


def expand(value, matrix):
    value = str(value)
    def replace(match):
        if match[1] not in matrix:
            raise ci.Skipped('matrix value unavailable')
        return matrix[match[1]]
    value = MATRIX.sub(replace, value)
    if '${{' in value:
        raise ci.Skipped('unsupported expression')
    return value


def select_plan(content, workflow, job, event='push'):
    try:
        import yaml
    except ImportError:
        raise ci.Skipped('PyYAML is required for Actions replay') from None
    if event != 'push' or not re.fullmatch(r'\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml', workflow):
        raise ci.Skipped('unsupported workflow event or path')
    try:
        class UniqueLoader(yaml.SafeLoader):
            pass
        def mapping(loader, node, deep=False):
            result = {}
            for key, value in loader.construct_pairs(node, deep=deep):
                if key in result:
                    raise ValueError('duplicate YAML key')
                result[key] = value
            return result
        UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
        data = yaml.load(content, Loader=UniqueLoader)
    except (yaml.YAMLError, TypeError, ValueError):
        raise ci.Skipped('invalid workflow YAML') from None
    if not isinstance(data, dict) or not isinstance(data.get('jobs'), dict):
        raise ci.Skipped('workflow jobs unavailable')
    if re.search(r'\$\{\{\s*(?:secrets|vars)\.', content):
        raise ci.Skipped('workflow requires secrets or variables')
    for expression in re.findall(r'\$\{\{(.*?)\}\}', content, re.S):
        if not re.fullmatch(r'\s*(?:matrix\.[A-Za-z_][\w-]*|github\.event_name)\s*', expression):
            raise ci.Skipped('workflow requires unsupported context')
    matches = []
    for job_id, item in data['jobs'].items():
        if not isinstance(job_id, str) or not re.fullmatch(r'[A-Za-z_][\w-]*', job_id) or not isinstance(item, dict):
            continue
        try:
            matrices = matrix_rows(item)
        except ci.Skipped:
            continue
        for matrix in matrices:
            try:
                display = expand(item.get('name', job_id), matrix)
            except ci.Skipped:
                continue
            names = {display}
            if matrix and 'name' not in item:
                names.add(display + ' (' + ', '.join(matrix.values()) + ')')
            if job['name'] in names:
                matches.append((job_id, item, matrix))
    if len(matches) != 1:
        raise ci.Skipped('workflow job identity unsupported or ambiguous')
    job_id, item, matrix = matches[0]
    if any(key in item for key in ('uses', 'needs', 'services', 'container', 'environment')):
        raise ci.Skipped('workflow needs unsupported job features')
    runners = item.get('runs-on', [])
    runners = runners if isinstance(runners, list) else [runners]
    runners = [expand(value, matrix) for value in runners]
    if not (any(re.fullmatch(r'ubuntu-(?:latest|\d{2}\.\d{2})', r) for r in runners)
            or 'linux' in runners and 'self-hosted' in runners):
        raise ci.Skipped('unsupported OS or runner')
    if any(re.search(r'windows|macos|arm', r, re.I) for r in runners):
        raise ci.Skipped('unsupported OS or runner')
    steps = item.get('steps')
    if not isinstance(steps, list):
        raise ci.Skipped('workflow steps unavailable')
    failed = [step['name'] for step in job.get('steps', []) if step.get('conclusion') in ci.FAILED]
    if len(failed) != 1:
        raise ci.Skipped('failed workflow step identity unavailable')
    target = []
    for step in steps:
        if not isinstance(step, dict) or ('uses' in step) == ('run' in step):
            raise ci.Skipped('unsupported workflow step')
        if 'uses' in step:
            action = str(step['uses'])
            if not ACTION.fullmatch(action):
                raise ci.Skipped('workflow requires an unsupported action')
            options = step.get('with', {})
            if not isinstance(options, dict):
                raise ci.Skipped('unsupported action inputs')
            allowed = {'python-version', 'node-version', 'go-version', 'java-version', 'distribution'}
            if action.startswith('actions/checkout@'):
                allowed = set()
            if set(options) - allowed:
                raise ci.Skipped('unsupported action inputs')
            continue
        command = step['run']
        if not isinstance(command, str) or not command.strip() or re.search(r'[\n\r;&|><`\\]', command):
            raise ci.Skipped('unsupported shell command')
        expanded = expand(command, matrix)
        args = shlex.split(expanded)
        if not args or args[0] not in ('python', 'python3', 'pytest', 'ruff', 'mypy', 'tsc', 'npm', 'pnpm', 'yarn', 'go', 'cargo', 'mvn', 'gradle', './gradlew'):
            raise ci.Skipped('unsupported workflow command')
        if any(arg in ('publish', 'deploy', '-c', '--eval') for arg in args[1:]):
            raise ci.Skipped('unsupported workflow command')
        if args[0] == 'tsc' and ('--noEmit' not in args or '--build' in args or '-b' in args
                                 or any(arg.startswith('--noEmit=') or arg == 'false' for arg in args)):
            raise ci.Skipped('type check must retain noEmit')
        if (args[0] == 'ruff' or len(args) > 2 and args[1:3] == ['-m', 'ruff']) and any(arg.startswith('--fix') for arg in args):
            raise ci.Skipped('lint must not modify the checkout')
        name = step.get('name') or 'Run ' + command
        if name == failed[0]:
            target.append((step.get('name') or command, args))
    if len(target) != 1:
        raise ci.Skipped('failed workflow step identity unsupported or ambiguous')
    name, args = target[0]
    kind = ('lint' if args[0] in ('ruff', 'mypy', 'tsc') or 'lint' in args
            or len(args) > 2 and args[0] in ('python', 'python3') and args[1] == '-m'
            and args[2] in ('ruff', 'mypy') else 'test')
    return dict(kind=kind, workflow=workflow, job_id=job_id, event=event, matrix=matrix,
                target_step=name, runners=runners)


CHECK = r'''
import hashlib, json, os, re, subprocess, sys, tempfile
from pathlib import Path

def finish(status, reason, diagnostic='', tests=None):
    print(json.dumps({'status': status, **({'failure': expected} if status == 'FAIL' else {})}))
    print(json.dumps({'reason': reason, 'diagnostic': diagnostic, 'tests': tests or []}), file=sys.stderr)
    sys.exit({'PASS': 0, 'FAIL': 1, 'INCOMPLETE': 2}[status])

def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()

if Path('.actrc').exists():
    finish('INCOMPLETE', 'dependency, service, secret or infrastructure unavailable')
command = ['unshare', '--mount', '--', 'sh', '-ec',
           'mount --bind /dev/null /dev/ptmx; exec "$@"', 'coldfix-act',
           'act', event, '--workflows', workflow, '--job', job_id, '--json',
           '--secret-file', '/dev/null', '--env-file', '/dev/null', '--var-file', '/dev/null',
           '--input-file', '/dev/null']
for runner in runners:
    command += ['-P', runner + '=-self-hosted']
for key, value in matrix.items():
    command += ['--matrix', key + ':' + value]
try:
    # act's host copy drops empty directories; a detached checkout otherwise
    # loses .git/refs and stops being a Git repository in the job workspace.
    subprocess.run(['git', 'update-ref', 'refs/coldfix/replay', 'HEAD'], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], stderr=subprocess.DEVNULL)
    tree_changes = subprocess.check_output(['git', 'diff', '--binary', 'HEAD'], stderr=subprocess.DEVNULL)
    path = os.environ.get('COLDFIX_GUEST_LOG')
    with (open(path, 'w+b') if path else tempfile.TemporaryFile()) as output:
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT)
        code = child.wait()
        output.seek(0)
        lines = output.read().decode('utf-8', 'replace').split('\n')
    if (revision != subprocess.check_output(['git', 'rev-parse', 'HEAD'], stderr=subprocess.DEVNULL)
            or tree_changes != subprocess.check_output(['git', 'diff', '--binary', 'HEAD'], stderr=subprocess.DEVNULL)):
        finish('INCOMPLETE', 'dependency, service, secret or infrastructure unavailable')
except (OSError, subprocess.CalledProcessError):
    finish('INCOMPLETE', 'local tool unavailable')
events = []
for line in lines:
    try:
        value = json.loads(line)
        if isinstance(value, dict): events.append(value)
    except ValueError:
        pass
steps = [e for e in events if e.get('jobID') == job_id and e.get('stage') == 'Main'
         and e.get('step') == target_step and e.get('stepResult') in ('success', 'failure')]
if len(steps) != 1:
    finish('INCOMPLETE', 'structured test results unavailable')
step = steps[0]
if {str(k): str(v) for k, v in (step.get('matrix') or {}).items()} != matrix:
    finish('INCOMPLETE', 'structured test results unavailable')
raw = ''.join(e.get('msg', '') for e in events if e.get('jobID') == job_id and e.get('stage') == 'Main'
              and e.get('step') == target_step and e.get('raw_output') is True and isinstance(e.get('msg'), str))
if re.search(r'ModuleNotFoundError|ImportError|command not found|No module named|connection refused|could not resolve|network is unreachable|No such file or directory|PermissionError|No space left', raw, re.I):
    finish('INCOMPLETE', 'dependency, service, secret or infrastructure unavailable')
failed = sorted({digest(x) for x in re.findall(r'(?m)^FAIL: (.+)$', raw)}) if kind == 'test' else []
passed = {digest(x) for x in re.findall(r'(?m)^(\w+ \([\w.]+\))(?: \.\.\. ok|\n[^\n]* \.\.\. ok)\s*$', raw)} if kind == 'test' else set()
if code == 0 and step['stepResult'] == 'success':
    if known_tests and not set(known_tests) <= passed:
        finish('INCOMPLETE', 'failed test did not pass after fix')
    finish('PASS', 'local command passed', tests=known_tests)
if code == 0 or step['stepResult'] != 'failure':
    finish('INCOMPLETE', 'unexpected local exit')
if kind == 'test' and (not failed or known_tests and set(known_tests) != set(failed)):
    finish('INCOMPLETE', 'structured test results unavailable')
match = re.search(r'(?m)^\s*(?:E\s+)?AssertionError[^\n]*|^.*error TS\d+:[^\n]*|^.*:\d+(?::\d+)?: (?:error:|[A-Z]\d{3}\b)[^\n]*', raw)
if not match and kind == 'lint':
    match = re.search(r'(?m)^FAIL: [^\n]+$', raw)
if not match:
    finish('INCOMPLETE', 'unrecognized failure diagnostic')
diagnostic = digest(' '.join(match[0].split()))
if known and known != diagnostic:
    finish('INCOMPLETE', 'diagnostic differs from retained failure')
finish('FAIL', 'known local diagnostic', diagnostic, failed)
'''


def check_script(plan, known=None, known_tests=None):
    fields = dict(plan, expected=ci.EXPECTED, known=known, known_tests=known_tests or [])
    return '\n'.join(key + ' = ' + repr(value) for key, value in fields.items()) + '\n' + CHECK


def replay_pair(repo, before, after, plan, work, timeout, *, provision=()):
    user_setup = (Path(__file__).parent / 'scripts' / 'provision-actions-user.sh').read_text()
    return replay.replay_pair(repo, before, after, plan, work, timeout,
                              provision=[*ACT_PROVISION, *provision, ['sh', '-ec', user_setup]],
                              builder=check_script)
