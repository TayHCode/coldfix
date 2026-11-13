"""Explicit local import of reviewed, private Coldpass replay proposals."""

import ast
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path, PurePosixPath
import re
import shlex

import coldfix as c
import ci_fix_learning as ci
import failure_ingest as fi

NAME = 'coldpass-replay-inputs.json'
HISTORY = 'coldpass-replay-input-history'
ARTIFACTS = 'coldpass-replay-artifacts'


def need(condition, reason='identity_mismatch'):
    c.require(condition, 'Coldpass replay input: ' + reason)


def fields(value, required, optional=''):
    need(isinstance(value, dict) and set(required.split()) <= value.keys()
         and value.keys() <= set((required + ' ' + optional).split()), 'invalid_manifest')


def sha(value, length=64):
    need(isinstance(value, str) and re.fullmatch('[0-9a-f]{' + str(length) + '}', value), 'invalid_digest')


def revision(value):
    need(isinstance(value, str) and re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', value), 'invalid_revision')


def relative(value):
    need(isinstance(value, str) and value and '\\' not in value and '\x00' not in value
         and not PurePosixPath(value).is_absolute() and '..' not in PurePosixPath(value).parts,
         'unsupported_reproducer')


def proof(value):
    fields(value, 'check check_sha256 cwd argv expected_failure runtime setup dependencies')
    c.text(value['check'], 'check')
    sha(value['check_sha256'])
    need(c.digest(value['check']) == value['check_sha256'], 'invalid_digest')
    relative(value['cwd'])
    argv = value['argv']
    need(isinstance(argv, list) and argv and all(isinstance(arg, str) and arg for arg in argv),
         'unsupported_reproducer')
    try:
        ci.supported(shlex.join(argv))
    except (ci.Skipped, ValueError, c.ColdfixError):
        raise c.ColdfixError('Coldpass replay input: unsupported_reproducer') from None
    c.text(value['expected_failure'], 'expected named failure', single_line=True)
    fields(value['runtime'], 'name version sha256')
    need(value['runtime']['name'] == argv[0], 'unsupported_reproducer')
    c.text(value['runtime']['version'], 'runtime version', single_line=True)
    sha(value['runtime']['sha256'])
    need(isinstance(value['setup'], list) and isinstance(value['dependencies'], list), 'invalid_manifest')
    for artifact in value['setup'] + value['dependencies']:
        fields(artifact, 'path version sha256')
        relative(artifact['path'])
        c.text(artifact['version'], 'artifact version', single_line=True)
        sha(artifact['sha256'])


def validate(document, failures):
    fields(document, 'schema_version entries')
    need(type(document['schema_version']) is int and document['schema_version'] == 1, 'invalid_manifest')
    need(isinstance(document['entries'], list), 'invalid_manifest')
    seen = {}
    for entry in document['entries']:
        fields(entry, 'failure_id source_ref type run_id repo repository_id failure_class before source_payload_sha256 upstream',
               'witness reproducer model test_version')
        need(('witness' in entry) != ('reproducer' in entry), 'missing_reproducer_or_witness')
        failure = failures.get(entry['failure_id'])
        need(failure is not None)
        metadata = json.loads(failure['error'], object_pairs_hook=c.unique_object)
        need(entry['type'] == metadata.get('type') == 'coldpass')
        need(entry['source_ref'] == failure['source_ref'])
        for key in ('run_id', 'failure_class'):
            need(entry[key] == metadata.get(key) and isinstance(entry[key], str) and bool(entry[key]))
        need(entry['repo'] == fi.repo_name(entry['repo']) == fi.repo_name(metadata['repo']))
        need(type(entry['repository_id']) is int and entry['repository_id'] > 0)
        if metadata.get('repository_id') is not None:
            need(entry['repository_id'] == metadata['repository_id'])
        for key in ('model', 'test_version'):
            if key in metadata:
                need(entry.get(key) == metadata[key])
            elif key in entry:
                need(key == 'model' and entry[key] == failure['agent'])
        revision(entry['before'])
        need(entry['before'] == c.recorded_revision(metadata))
        sha(entry['source_payload_sha256'])
        need(entry['source_payload_sha256'] == c.digest(failure['error']))
        proof(entry.get('witness', entry.get('reproducer')))
        upstream = entry['upstream']
        fields(upstream, 'after fix_commit evidence patch_sha256', 'paths')
        if 'paths' in upstream:
            need(isinstance(upstream['paths'], list) and upstream['paths'], 'fix_patch_unverified')
            for path in upstream['paths']:
                relative(path)
                need(path != '.', 'fix_patch_unverified')
        revision(upstream['after'])
        revision(upstream['fix_commit'])
        need(upstream['after'] != entry['before'], 'same_revision_not_fix')
        sha(upstream['patch_sha256'])
        prefix = 'https://github.com/' + entry['repo'] + '/'
        need(isinstance(upstream['evidence'], str) and
             re.fullmatch(re.escape(prefix) + r'(?:commit/[0-9a-f]{40}|pull/[1-9][0-9]*)', upstream['evidence']),
             'missing_upstream_fix')
        previous = seen.setdefault(entry['failure_id'], entry)
        need(previous == entry, 'conflicting_input')
    return dict(schema_version=1, entries=sorted(seen.values(), key=lambda row: row['failure_id']))


def acquire(entry, workspace):
    import learn_replay
    slug = entry['repo']
    try:
        with urllib.request.urlopen('https://api.github.com/repos/' + slug, timeout=30) as response:
            metadata = json.load(response)
        need(isinstance(metadata, dict) and type(metadata.get('id')) is int
             and metadata['id'] == entry['repository_id']
             and str(metadata.get('full_name', '')).casefold() == slug
             and metadata.get('private') is False and metadata.get('visibility') == 'public',
             'public_identity_unverified')
        env = learn_replay.SourceProjects(None, workspace, '', 120).env(public=True)
        env = {key: value for key, value in env.items() if not key.startswith('GIT_')}
        env.update(GIT_TERMINAL_PROMPT='0', GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_ASKPASS='/bin/false', GIT_CONFIG_COUNT='1',
                   GIT_CONFIG_KEY_0='credential.helper', GIT_CONFIG_VALUE_0='')
        repo = workspace / 'repository'
        subprocess.run(['git', '-c', 'core.hooksPath=' + os.devnull, 'clone', '--quiet',
                        '--no-checkout', '--no-tags', '--', 'https://github.com/' + slug + '.git', str(repo)],
                       env=env, check=True, capture_output=True, timeout=120)
        return repo, metadata
    except (OSError, ValueError, subprocess.SubprocessError):
        raise c.ColdfixError('Coldpass replay input: public_identity_unverified') from None


def upstream_history(entry, repo, metadata, deadline=None):
    def git(*args):
        return c.privacy_git(repo, *args, timeout=c.remaining_timeout(30, deadline))

    need(isinstance(metadata, dict) and type(metadata.get('id')) is int
         and metadata['id'] == entry['repository_id']
         and str(metadata.get('full_name', '')).casefold() == entry['repo']
         and metadata.get('private') is False and metadata.get('visibility') == 'public',
         'public_identity_unverified')
    before, upstream = entry['before'], entry['upstream']
    after, fix = upstream['after'], upstream['fix_commit']
    for commit in (before, after, fix):
        try:
            resolved = git('rev-parse', '--verify', commit + '^{commit}').decode().strip()
            need(resolved == commit, 'revision_unavailable')
        except c.ColdfixError:
            raise c.ColdfixError('Coldpass replay input: revision_unavailable') from None
    def ancestor(left, right):
        return left in git('rev-list', right).decode().splitlines()
    need(ancestor(before, after) and ancestor(fix, after) and fix != before,
         'unrelated_fix_history')
    need(git('rev-parse', before + '^{tree}') !=
         git('rev-parse', after + '^{tree}'), 'same_tree_not_fix')
    parents = git('rev-list', '--parents', '-n', '1', fix).decode().split()
    need(len(parents) == 2 and ancestor(before, parents[1]), 'unrelated_fix_history')
    paths = upstream.get('paths', [])
    patch = git('diff', '--binary', '--full-index', parents[1], fix,
                          '--', *[':(literal)' + path for path in paths])
    need(patch and hashlib.sha256(patch).hexdigest() == upstream['patch_sha256'], 'fix_patch_unverified')
    names = git('diff', '--name-only', '-z', parents[1], fix,
                          '--', *[':(literal)' + path for path in paths]).decode().split('\0')
    production = [name for name in names if name and not any(
        part.lower() in ('tests', 'test', 'docs', 'examples', '.github', 'harness')
        or part.lower().startswith('test_') for part in PurePosixPath(name).parts)
        and not re.search(r'(?:_test\.[^.]+|\.(?:test|spec)\.[^.]+)$', name, re.IGNORECASE)
        and PurePosixPath(name).suffix in ('.py', '.js', '.ts', '.tsx', '.rs', '.go', '.c', '.cpp', '.java')
        and PurePosixPath(name).name not in ('setup.py', 'conftest.py', 'model.py', 'test_version.py')]
    need(production, 'fix_patch_unverified')
    for path in production:
        need(git('diff', '--binary', '--full-index', before, after, '--', ':(literal)' + path)
             == git('diff', '--binary', '--full-index', parents[1], fix, '--', ':(literal)' + path),
             'fix_patch_unverified')
    evidence = upstream['evidence']
    if '/commit/' in evidence:
        need(evidence.rsplit('/', 1)[1] == fix, 'fix_patch_unverified')
    else:
        try:
            with urllib.request.urlopen('https://api.github.com/repos/' + entry['repo'] + '/pulls/'
                                        + evidence.rsplit('/', 1)[1], timeout=c.remaining_timeout(30, deadline)) as response:
                pull = json.load(response)
            need(pull.get('merged') is True and pull.get('merge_commit_sha') == fix
                 and pull['base']['repo']['id'] == entry['repository_id'], 'fix_patch_unverified')
        except (OSError, ValueError, KeyError, TypeError):
            raise c.ColdfixError('Coldpass replay input: fix_patch_unverified') from None


def available_proof(value, artifacts, deadline=None):
    executable = shutil.which(value['runtime']['name'])
    need(executable is not None, 'unsupported_reproducer')
    need(hashlib.sha256(Path(executable).read_bytes()).hexdigest() == value['runtime']['sha256'],
         'unsupported_reproducer')
    result = subprocess.run([executable, '--version'], capture_output=True, timeout=c.remaining_timeout(10, deadline), text=True)
    need(result.returncode == 0 and value['runtime']['version'] in (result.stdout + result.stderr).split(),
         'unsupported_reproducer')
    try:
        tree = ast.parse(value['check'])
    except (SyntaxError, ValueError):
        raise c.ColdfixError('Coldpass replay input: unsupported_reproducer') from None
    argv = value['argv']
    need(argv[:3] in (['python3', '-m', 'unittest'], ['python', '-m', 'unittest']),
         'unsupported_reproducer')
    targets = [arg for arg in argv[3:] if not arg.startswith('-')]
    need(len(targets) == 1 and re.fullmatch(r'[A-Za-z_]\w*\.[A-Za-z_]\w*\.test_\w+', targets[0]),
         'unsupported_reproducer')
    _, cls, method = targets[0].split('.')
    tests = [(node.name, member) for node in tree.body if isinstance(node, ast.ClassDef)
             and any(ast.unparse(base) in ('unittest.TestCase', 'TestCase') for base in node.bases)
             for member in node.body if isinstance(member, ast.FunctionDef) and member.name.startswith('test_')]
    need(len(tests) == 1 and tests[0][0] == cls and tests[0][1].name == method
         and any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                 and isinstance(node.func.value, ast.Name) and node.func.value.id == 'self'
                 and node.func.attr.startswith('assert') for node in ast.walk(tests[0][1]))
         and value['expected_failure'] == method + ' (' + targets[0] + ')', 'unsupported_reproducer')
    return artifact_bytes(value, artifacts)


def artifact_bytes(value, artifacts):
    base = c.ordinary_path(artifacts)
    retained = {}
    for artifact in value['setup'] + value['dependencies']:
        path = c.ordinary_path(base / artifact['path'])
        need(path.is_relative_to(base) and path.is_file(), 'artifact_unavailable')
        content = path.read_bytes()
        need(hashlib.sha256(content).hexdigest() == artifact['sha256'], 'artifact_unavailable')
        retained[str(PurePosixPath(artifact['path']))] = content
    return retained


def artifact_root(root, entry):
    return c.ordinary_path(Path(root) / ARTIFACTS / c.digest(c.encoded(entry)))


def audited(root, document):
    content = c.encoded(document)
    archive = c.ordinary_path(root / HISTORY / (c.digest(content) + '.json'))
    return archive.is_file() and c.read_text(archive) == content


def load(root):
    root, store = c.load_store(root)
    path = c.ordinary_path(root / NAME)
    if not path.exists():
        return dict(schema_version=1, entries=[])
    document = validate(json.loads(c.read_text(path), object_pairs_hook=c.unique_object), store['failures'])
    need(audited(root, document), 'unaudited_manifest')
    for entry in document['entries']:
        artifact_bytes(entry.get('witness', entry.get('reproducer')), artifact_root(root, entry))
    return document


def import_document(root, document, *, artifacts=None, revise=False):
    root = c.ordinary_path(root)
    need(not root.exists() or root.is_dir() and root.stat().st_mode & 0o077 == 0, 'private_store_required')
    with c.lesson_lock(root):
        root, store = c.load_store(root)
        incoming = validate(document, store['failures'])
        path = c.ordinary_path(root / NAME)
        previous = validate(json.loads(c.read_text(path), object_pairs_hook=c.unique_object),
                            store['failures']) if path.exists() else dict(schema_version=1, entries=[])
        replacements = {row['failure_id'] for row in incoming['entries']} if revise else set()
        merged = validate(dict(schema_version=1, entries=[row for row in previous['entries']
                              if row['failure_id'] not in replacements] + incoming['entries']), store['failures'])
        previous_audited = audited(root, previous)
        content = c.encoded(merged)
        digest = c.digest(content)
        archive = c.ordinary_path(root / HISTORY / (digest + '.json'))
        if archive.exists():
            need(c.read_text(archive) == content, 'conflicting_input')
        with tempfile.TemporaryDirectory(prefix='.coldpass-input-', dir=root) as temporary:
            staged = []
            for index, entry in enumerate(merged['entries']):
                proof_input = entry.get('witness', entry.get('reproducer'))
                destination = artifact_root(root, entry)
                revalidate = not previous_audited or entry in incoming['entries']
                source = artifacts if artifacts is not None else (
                    destination if previous_audited and entry in previous['entries'] else root)
                blobs = available_proof(proof_input, source) if revalidate else artifact_bytes(proof_input, destination)
                if revalidate:
                    workspace = Path(temporary) / str(index)
                    workspace.mkdir()
                    repo, metadata = acquire(entry, workspace)
                    upstream_history(entry, repo, metadata)
                for name, blob in blobs.items():
                    target = c.ordinary_path(destination / name)
                    if target.exists():
                        need(target.is_file() and target.read_bytes() == blob, 'artifact_unavailable')
                    else:
                        stage = Path(temporary) / ('artifact-' + str(len(staged)))
                        stage.write_bytes(blob)
                        stage.chmod(0o600)
                        with stage.open('rb') as stream:
                            os.fsync(stream.fileno())
                        staged.append((stage, target))
            for stage, target in staged:
                target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                c.ordinary_path(target)
                os.link(stage, target)
            if not archive.exists():
                c.atomic_create(archive, content, mode=0o600)
            if merged != previous or not path.exists():
                c.atomic_create(path, content, replace=True, mode=0o600)
        return digest


def proposed_pairs(root):
    document = load(root)
    digest = c.digest(c.encoded(document))
    pairs = [dict(type='coldpass', failure_id=row['failure_id'], source_ref=row['source_ref'],
                 run_id=row['run_id'], repo=row['repo'], repository_id=row['repository_id'],
                 failure_class=row['failure_class'], before=row['before'], after=row['upstream']['after'],
                 source_payload_sha256=row['source_payload_sha256'], manifest_sha256=digest,
                 proof_input=row.get('witness', row.get('reproducer')), upstream=row['upstream'],
                 artifact_root=str(artifact_root(root, row)), status='proposed') for row in document['entries']]
    import coldpass_replay
    for pair in pairs:
        pair['binding_sha256'] = coldpass_replay.binding_digest(pair)
        pair['screen_check_sha256'] = c.digest(coldpass_replay.reviewed_check(pair)[0])
    return pairs


def main():
    parser = c.Parser(description=__doc__)
    parser.add_argument('--store', required=True)
    parser.add_argument('--input', required=True)
    parser.add_argument('--revise', action='store_true', help='retain history and replace explicitly reviewed inputs')
    args = parser.parse_args()
    try:
        document = json.loads(c.read_text(Path(args.input)), object_pairs_hook=c.unique_object)
        print(json.dumps(dict(status='PASS', manifest_sha256=import_document(Path(args.store), document, artifacts=Path(args.input).parent, revise=args.revise))))
    except (c.ColdfixError, ValueError, KeyError, TypeError, OSError, RecursionError, subprocess.SubprocessError):
        parser.exit(2, 'invalid Coldpass replay input; no activation performed\n')


if __name__ == '__main__':
    main()
