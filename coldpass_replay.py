"""Exact Coldpass input selection and safe readiness diagnostics."""
import copy
import hashlib
import json
import os
import re
import subprocess

import coldfix as c
import coldpass_inputs as inputs
import flakiness

REASONS = {
    'missing_revision': 'The recorded source revision is missing.',
    'invalid_revision': 'The recorded source revision is invalid.',
    'revision_conflict': 'Recorded source revisions conflict.',
    'identity_mismatch': 'The reviewed input does not match this failure.',
    'public_source_not_admitted': 'The public source is not admitted.',
    'public_identity_unverified': 'The public repository identity could not be verified.',
    'revision_unavailable': 'A pinned source revision is unavailable.',
    'missing_reproducer_or_witness': 'A reviewed behavioral witness is missing.',
    'unsupported_reproducer': 'The witness or its pinned setup is unsupported.',
    'missing_upstream_fix': 'A verified upstream fix is missing.',
    'same_revision_not_fix': 'The proposed fix uses the failing revision.',
    'same_tree_not_fix': 'The proposed fix has the failing source tree.',
    'unrelated_fix_history': 'The proposed fix is outside the failing history.',
    'fix_patch_unverified': 'The causal production patch could not be verified.',
    'screen_rejected': 'Independent screening has not approved this witness.',
    'wrong_failure': 'The assertion does not establish the recorded failure.',
    'replay_incomplete': 'Behavioral replay did not complete.',
    'privacy_blocked': 'Privacy checks blocked the proposed lesson.',
}
MISSING = frozenset({'missing_revision', 'public_source_not_admitted',
                     'missing_reproducer_or_witness', 'missing_upstream_fix'})


def reason(error):
    code = str(error).removeprefix('Coldpass replay input: ')
    aliases = {'public repository identity not verified': 'public_identity_unverified',
               'public repository redirected': 'public_identity_unverified',
               'public repository is not explicitly admitted': 'public_source_not_admitted',
               'public revisions are not explicitly admitted': 'identity_mismatch',
               'public repository ID mismatch': 'identity_mismatch',
               'recorded commit not available in source checkout': 'revision_unavailable'}
    return code if code in REASONS else aliases.get(code, 'replay_incomplete')


def matches(failure, pair):
    row = flakiness.metadata(failure)
    try:
        before = c.recorded_revision(row)
    except c.ColdfixError:
        return False
    return (pair.get('binding_sha256') == binding_digest(pair) and pair.get('type') == row.get('type') == 'coldpass' and before is not None
            and pair.get('failure_id') == failure['id']
            and pair.get('source_ref') == failure.get('source_ref')
            and pair.get('run_id') == row.get('run_id')
            and pair.get('repo') == failure['repo'].lower() == row.get('repo', '').lower()
            and type(pair.get('repository_id')) is int and pair['repository_id'] > 0
            and (row.get('repository_id') is None or pair['repository_id'] == row['repository_id'])
            and pair.get('failure_class') == row.get('failure_class')
            and pair.get('before') == before
            and pair.get('source_payload_sha256') == c.digest(failure['error']))


def binding_digest(pair):
    return c.digest(c.encoded({key: pair.get(key) for key in (
        'failure_id', 'source_ref', 'run_id', 'repo', 'repository_id', 'failure_class',
        'before', 'after', 'source_payload_sha256', 'manifest_sha256', 'upstream', 'proof_input')}))


def identity(pair):
    return c.digest(c.encoded({key: pair.get(key) for key in (
        'failure_id', 'source_ref', 'run_id', 'repo', 'repository_id', 'failure_class',
        'before', 'after', 'source_payload_sha256', 'manifest_sha256', 'upstream', 'proof_input',
        'admission_digest', 'screen_check_sha256')}))


def draft_identity(pair):
    return identity(dict(pair, manifest_sha256=None))


def blockers(failure, pairs):
    row = flakiness.metadata(failure)
    codes = []
    try:
        revision = c.recorded_revision(row)
    except c.ColdfixError:
        revision = None
        codes.append('revision_conflict')
    if not revision and not codes:
        values = [row[k] for k in ('commit', 'head_sha', 'sha', 'head') if k in row]
        valid = all(isinstance(value, str) and re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', value) for value in values)
        codes.append('missing_revision' if not values else 'revision_conflict' if valid else 'invalid_revision')
    selected = [pair for pair in pairs if matches(failure, pair)]
    if not selected:
        if any(pair.get('failure_id') == failure['id'] for pair in pairs):
            codes.append('identity_mismatch')
        codes += ['missing_reproducer_or_witness', 'missing_upstream_fix']
    else:
        for pair in selected:
            if not pair.get('proof_input'):
                codes.append('missing_reproducer_or_witness')
            if not pair.get('after') or not pair.get('upstream'):
                codes.append('missing_upstream_fix')
            elif pair['before'] == pair['after']:
                codes.append('same_revision_not_fix')
            codes.extend(pair.get('blockers', []))
    return list(dict.fromkeys(codes))


def readiness(store, evidence, pairs):
    reasons = {ref: blockers(store['failures'][ref], pairs) for ref in evidence
               if flakiness.metadata(store['failures'][ref]).get('type') == 'coldpass'}
    return dict(ready=bool(reasons) and any(not codes for codes in reasons.values()), evidence=reasons,
                descriptions={code: REASONS[code] for codes in reasons.values() for code in codes})


def bind_admission(failure, pair, projects):
    import learn_replay
    admission = projects.admissions.get(learn_replay.repo_slug(failure))
    if not projects.owner or pair['repo'].split('/')[0].lower() != projects.owner.lower() or admission:
        inputs.need(admission is not None, 'public_source_not_admitted')
        inputs.need(admission['repository_id'] == pair['repository_id']
                    and admission['before'] == pair['before'] and admission['after'] == pair['after'])
        pair['admission_digest'] = c.digest(c.encoded({key: admission[key] for key in ('repo', 'repository_id', 'before', 'after')}))
    else:
        pair['admission_digest'] = c.digest(projects.owner.lower())


def bind_admissions(store, pairs, projects):
    for pair in pairs:
        if pair.get('type') == 'coldpass':
            try:
                bind_admission(store['failures'][pair['failure_id']], pair, projects)
            except (c.ColdfixError, KeyError, TypeError) as error:
                pair['blockers'] = [reason(error)]
    return pairs


def preflight(root, store, pairs, projects, deadline=None):
    import learn_replay
    if deadline is not None:
        projects = copy.copy(projects)
        projects.deadline = deadline
    for pair in pairs:
        if pair.get('type') != 'coldpass':
            continue
        pair['blockers'] = []
        try:
            c.remaining_timeout(projects.timeout, deadline)
            failure = store['failures'].get(pair['failure_id'])
            inputs.need(failure is not None and matches(failure, pair))
            proof = pair['proof_input']
            inputs.available_proof(proof, pair['artifact_root'], deadline=deadline)
            inputs.need(all(item['path'].endswith('.whl') or item in proof['setup'] and item['path'].endswith('.py')
                            for item in proof['setup'] + proof['dependencies']), 'unsupported_reproducer')
            bind_admission(failure, pair, projects)
            repo = learn_replay.source_repo(failure, projects, revisions=(pair['before'], pair['after']))
            inputs.upstream_history(dict(pair, upstream=pair['upstream']), repo,
                                    dict(id=pair['repository_id'], full_name=pair['repo'], private=False, visibility='public'),
                                    deadline=deadline)
            pair['fix_diff'] = c.repo_patch(repo, pair['before'], pair['after'], ['.'], projects.timeout, deadline=deadline).decode('utf-8', 'replace')[:4000]
        except (c.ColdfixError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
            pair['blockers'] = [reason(error)]
    return pairs


def reviewed_check(pair):
    import regression_adapter
    proof = pair['proof_input']
    target = next(arg for arg in proof['argv'][3:] if not arg.startswith('-'))
    module, _, name = target.split('.')
    path = module + '.py'
    import learn_replay
    patch = learn_replay.new_file_patch(path, proof['check'])
    return regression_adapter.build_coldpass(path, patch, name, proof, inputs.artifact_bytes(proof, pair['artifact_root']))


def screen_binding(pair, draft, drafter, reviewer, verdict, reason_text):
    return dict(version=1, input_digest=identity(pair), check_sha256=c.digest(draft["check_script"]),
                draft_sha256=c.digest(c.encoded({key: draft[key] for key in
                    ("kind", "title", "rule", "check_command", "check_script", "expected_failure")})),
                witness_sha256=pair['proof_input']['check_sha256'], failure_id=pair['failure_id'],
                source_payload_sha256=pair['source_payload_sha256'], manifest_sha256=pair['manifest_sha256'],
                expected_assertion=pair['proof_input']['expected_failure'],
                drafter=drafter, reviewer=reviewer, verdict=verdict, reason=reason_text)


def validate_screen(pair, entry, script):
    screen = entry.get('coldpass_screen', {})
    inputs.need(screen == screen_binding(pair, entry['draft'], entry['drafter'], entry['reviewer'], 'PASS', entry['reason'])
                and screen['drafter'] != screen['reviewer'], 'screen_rejected')
    inputs.need(entry['evidence'] == [pair['failure_id']] and entry['draft']['check_script'] == script,
                'wrong_failure')


def screened_pairs(root, store, pairs, ledger):
    result = list(pairs)
    for entry in ledger['drafts'].values():
        screen = entry.get('coldpass_screen')
        if not screen or entry['verdict'] != 'PASS':
            continue
        for index, pair in enumerate(result):
            if (pair.get('type') != 'coldpass' or entry['evidence'] != [pair['failure_id']]
                    or entry.get('input_digest') != draft_identity(pair)
                    or screen['manifest_sha256'] == pair['manifest_sha256']):
                continue
            path = root / inputs.HISTORY / (screen['manifest_sha256'] + '.json')
            document = json.loads(c.read_text(path), object_pairs_hook=c.unique_object)
            inputs.need(c.digest(c.encoded(document)) == screen['manifest_sha256'], 'screen_rejected')
            failure = store['failures'][pair['failure_id']]
            selected = [row for row in document['entries'] if row['failure_id'] == failure['id']]
            inputs.need(len(selected) == 1, 'screen_rejected')
            inputs.validate(dict(schema_version=1, entries=selected), {failure['id']: failure})
            rebound = dict(pair, manifest_sha256=screen['manifest_sha256'])
            rebound['binding_sha256'] = binding_digest(rebound)
            inputs.need(selected[0]['upstream'] == pair['upstream']
                        and selected[0].get('witness', selected[0].get('reproducer')) == pair['proof_input'], 'screen_rejected')
            validate_screen(rebound, entry, entry['draft']['check_script'])
            result[index] = rebound
    return result


def companion_key(screen):
    return c.digest(c.encoded(screen))


def companion_path(root, lesson):
    return root / 'coldpass-admissions' / (lesson['admission']['binding_sha256'] + '.json')


def save_companion(root, pair, entry):
    value = dict(version=1, pair={key: item for key, item in pair.items() if key not in ('artifact_root', 'blockers', 'fix_diff')},
                 screen=entry['coldpass_screen'], reason=entry['reason'])
    path = root / 'coldpass-admissions' / (companion_key(entry['coldpass_screen']) + '.json')
    if path.exists():
        inputs.need(json.loads(c.read_text(path)) == value, 'screen_rejected')
    else:
        c.atomic_create(path, c.encoded(value), mode=0o600)


def validate_companion(root, failure, lesson):
    path = companion_path(root, lesson)
    try:
        value = json.loads(c.read_text(path), object_pairs_hook=c.unique_object)
        inputs.need(value['version'] == 1)
        pair, screen = value['pair'], value['screen']
        inputs.need(companion_key(screen) == lesson['admission']['binding_sha256'], 'screen_rejected')
        inputs.need(matches(failure, pair))
        manifest = root / inputs.HISTORY / (pair['manifest_sha256'] + '.json')
        document = json.loads(c.read_text(manifest), object_pairs_hook=c.unique_object)
        inputs.need(c.digest(c.encoded(document)) == pair['manifest_sha256'])
        document = inputs.validate(dict(document, entries=[row for row in document['entries']
                                   if row['failure_id'] == failure['id']]), {failure['id']: failure})
        row = next(row for row in document['entries'] if row['failure_id'] == failure['id'])
        inputs.need(row['upstream'] == pair['upstream'] and row.get('witness', row.get('reproducer')) == pair['proof_input'])
        entry = dict(coldpass_screen=screen, drafter=screen['drafter'], reviewer=screen['reviewer'], reason=value['reason'],
                     evidence=lesson['evidence'], draft={key: lesson[key] for key in
                         ('kind', 'title', 'rule', 'check_command', 'check_script', 'expected_failure')})
        validate_screen(pair, entry, lesson['check_script'])
        import regression_adapter
        import learn_replay
        data = regression_adapter.payload(lesson['check_script'])
        target = next(arg for arg in pair['proof_input']['argv'][3:] if not arg.startswith('-'))
        inputs.need(data['proof'] == pair['proof_input']
                    and data['path'] == target.split('.')[0] + '.py'
                    and data['patch'] == learn_replay.new_file_patch(data['path'], pair['proof_input']['check']), 'wrong_failure')
        inputs.need(lesson['admission']['kind'] == 'verified_coldpass_incident'
                    and lesson['admission']['source_ref'] == failure['source_ref']
                    and c.check_pins(lesson['check_script'], lesson['admission']['test_sha256'], lesson['admission']), 'wrong_failure')
        return pair
    except (KeyError, OSError, ValueError, StopIteration):
        raise c.ColdfixError('screen_rejected') from None


def validate_receipt_revisions(pair, receipt):
    for side, expected in (('before', pair['before']), ('after', pair['after']), ('reversed', pair['after'])):
        row = receipt[side]
        inputs.need(row['commit'] == expected or row['status'] == 'INCOMPLETE' and row['commit'] == '',
                    'revision_conflict')


def prepare_pairs(root, store, pairs, projects=None):
    if not any(pair.get('type') == 'coldpass' for pair in pairs):
        return pairs
    import learn_replay
    try:
        admissions = learn_replay.public_admission.load(root)
    except (c.ColdfixError, OSError, ValueError, TypeError, KeyError):
        for pair in pairs:
            if pair.get('type') == 'coldpass':
                pair['blockers'] = ['public_identity_unverified']
        return pairs
    with learn_replay.replay_isolation.source_workspace(root, '.coldpass-ready-') as workspace:
        sources = learn_replay.SourceProjects(c.ordinary_path(projects) if projects else None,
            workspace, os.environ.get('COLDFIX_OWNER', ''), 120,
            admissions=admissions)
        return preflight(root, store, pairs, sources)


def causal_patch(root, lesson, repo):
    value = json.loads(c.read_text(companion_path(root, lesson)))
    upstream = value['pair']['upstream']
    fix = upstream['fix_commit']
    parents = c.privacy_git(repo, 'rev-list', '--parents', '-n', '1', fix).decode().split()
    inputs.need(len(parents) == 2, 'unrelated_fix_history')
    patch = c.privacy_git(repo, 'diff', '--binary', '--full-index', parents[1], fix, '--',
                          *[':(literal)' + path for path in upstream.get('paths', [])])
    inputs.need(hashlib.sha256(patch).hexdigest() == upstream['patch_sha256'], 'fix_patch_unverified')
    return patch


def artifact_members(blobs):
    import io
    import zipfile
    for name, blob in blobs.items():
        if name.endswith('.whl'):
            yield name, blob, False
            try:
                with zipfile.ZipFile(io.BytesIO(blob)) as archive:
                    inputs.need(sum(item.file_size for item in archive.infolist()) <= 50_000_000, 'privacy_blocked')
                    yield 'archive-comment', archive.comment, False
                    for item in archive.infolist():
                        yield item.orig_filename, archive.read(item), True
                        yield item.orig_filename + ':comment', item.comment, False
                        yield item.orig_filename + ':extra', item.extra, False
            except (zipfile.BadZipFile, RuntimeError, OSError):
                raise c.ColdfixError('privacy_blocked') from None
        else:
            yield name, blob, True


def privacy_text(proof, blobs):
    return '\n'.join([c.encoded(proof), *(name + '\n' + blob.decode('utf-8', 'replace')
                                       for name, blob, scan_controls in artifact_members(blobs) if scan_controls)])


def privacy_counts(proof, blobs, terms=None):
    counts = c.privacy_counts(privacy_text(proof, blobs), terms, filename_exempt=True)
    for _, blob, _ in artifact_members(blobs):
        if not blob:
            continue
        for encoding, width in (('utf-8', 1), ('utf-16-le', 2), ('utf-16-be', 2), ('utf-32-le', 4), ('utf-32-be', 4)):
            for offset in range(width):
                text = blob[offset:].decode(encoding, 'replace')
                for category, count in c.privacy_counts(text, terms, filename_exempt=True).items():
                    if category != 'control':
                        counts[category] = max(counts.get(category, 0), count)
    return counts
