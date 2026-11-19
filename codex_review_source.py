"""Collect Codex findings and hand in-PR bug/fix pairs to CI learning replay."""

import argparse
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid

import ci_fix_learning as ci
import ci_workflow_replay as workflow_replay
import coldfix as c
import learn_replay
import regression_adapter
import replay_isolation
from failure_ingest import SOURCE_ERRORS, instant, integer, repo_name
from github_sources import SHA, private_store

MARKER = '<!-- codex-code-review-finding:v1 -->'
BOT = 'chatgpt-codex-connector'
RETRYABLE = {'VM_EXECUTOR_UNAVAILABLE', 'REPLAY_TIMEOUT', 'read operation unavailable',
             'local tool unavailable', 'replay timed out or checkout unavailable',
             'dependency, service, secret or infrastructure unavailable', 'tool unavailable or operation timed out',
             'CI_COMMAND_UNAVAILABLE_OR_AMBIGUOUS', 'CI_COMMAND_UNAVAILABLE'}
REVIEWED = re.compile(r'Reviewed commit[^`\n]*`([0-9a-f]{7,40})`')
BADGE = re.compile(r'!\[P([0-3]) Badge\]\(https://img\.shields\.io/badge/P\1-')
HUNK = re.compile(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@')
RESERVE = 1000


def retryable(reason):
    prefix = 'reversed replay unavailable: '
    return reason in RETRYABLE or isinstance(reason, str) and reason.startswith(prefix) and reason[len(prefix):] in RETRYABLE


def stamp(previous):
    today, revision = learn_replay.utc_day(), learn_replay.code_revision()
    same = isinstance(previous, dict) and previous.get('revision') == revision
    confirmed = same and (bool(previous.get('confirmed')) or previous.get('day') not in (None, today))
    return dict(revision=revision, day=today, confirmed=bool(confirmed))


def settled(record):
    return (isinstance(record, dict) and bool(record.get('confirmed'))
            and record.get('revision') == learn_replay.code_revision() and learn_replay.recent(record.get('day')))


def skip_classes(rows):
    classes = {}
    for row in rows:
        if row['reason']:
            name = 'no_fix_pair' if not row.get('after') else 'retry_pending' if retryable(row['reason']) else 'replay_failed'
            counts = classes.setdefault(name, {})
            counts[row['reason']] = counts.get(row['reason'], 0) + 1
    return classes


class API:
    def __init__(self, directory):
        self.directory = directory
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        ci.need(directory.stat().st_mode & 0o077 == 0)
        self.requests = 0
        self.remaining = None
        self.get('rate_limit', fresh=True)

    def get(self, endpoint, fresh=False):
        retained = self.directory / (c.digest(endpoint) + '.json')
        unavailable = retained.with_suffix('.unavailable')
        if unavailable.exists() and not fresh:
            raise ci.Skipped('SOURCE_UNAVAILABLE')
        if retained.exists() and not fresh:
            return json.loads(retained.read_text(), object_pairs_hook=c.unique_object)
        if self.remaining is not None and self.remaining <= RESERVE:
            raise ci.Skipped('API_RESERVE_REACHED')
        self.requests += 1
        if self.remaining is not None:
            self.remaining -= 1
        try:
            raw = ci.run(['gh', 'api', '--hostname', 'github.com', '--method', 'GET', '--include', endpoint])
        except ci.Skipped:
            if not fresh:
                c.atomic_create(unavailable, 'source unavailable\n')
            raise
        headers, body = re.split(rb'\r?\n\r?\n', raw, maxsplit=1)
        remaining = re.search(rb'(?im)^x-ratelimit-remaining:\s*(\d+)', headers)
        ci.need(remaining is not None)
        self.remaining = int(remaining[1])
        value = json.loads(body, object_pairs_hook=c.unique_object)
        if endpoint == 'rate_limit':
            self.remaining = value['resources']['core']['remaining']
        else:
            c.atomic_create(retained, c.encoded(value))
        return value

    def pages(self, endpoint):
        rows = []
        for page in range(1, 1001):
            join = '&' if '?' in endpoint else '?'
            batch = self.get(f'{endpoint}{join}per_page=100&page={page}')
            ci.need(isinstance(batch, list) and len(batch) <= 100)
            rows.extend(batch)
            if len(batch) < 100:
                return rows
        raise ci.Skipped('PAGINATION_BOUND_REACHED')


class GitHub(ci.GitHub):
    def __init__(self, repo, client):
        super().__init__(repo)
        self.client = client

    def api(self, endpoint):
        return self.client.get(f'repos/{self.repo}/{endpoint}')


def is_bot(user):
    return isinstance(user, dict) and user.get('login') in (BOT, BOT + '[bot]')


def owned(repo, owner=None):
    owner = (owner or os.environ.get('COLDFIX_OWNER', '')).casefold()
    return bool(owner) and repo_name(repo).split('/')[0].casefold() == owner


def cached_receipt(cache, repo, number, before):
    if cache is None:
        return False
    directory = cache
    for part in repo.split('/'):
        if (directory / part).is_dir():
            directory /= part
        elif directory.is_dir():
            matches = [path for path in directory.iterdir() if path.is_dir() and path.name.casefold() == part.casefold()]
            if len(matches) != 1:
                return False
            directory = matches[0]
        else:
            return False
    path = directory / 'Pull-Requests' / f'{number:04}.md'
    if not path.is_file():
        return False
    text = path.read_text(encoding='utf-8')
    if not re.search(r'^source_id: ' + re.escape(repo) + f'/pull/{number}$', text, re.M | re.I):
        return False
    for heading, body in re.findall(r'^## (review: [^\n]+)\n(.*?)(?=^## |\Z)', text, re.M | re.S):
        if re.search(r' · chatgpt-codex-connector(?:\[bot\])? · ', heading):
            match = REVIEWED.search(body)
            if match and before.startswith(match[1]):
                return True
    return False


def reviewed_commit(github, comment, number, cache):
    before = comment.get('original_commit_id', '')
    ci.need(isinstance(before, str) and SHA.fullmatch(before))
    if cached_receipt(cache, github.repo, number, before):
        return before, 'coldvault'
    review = github.api(f"pulls/{number}/reviews/{integer(comment['pull_request_review_id'])}")
    match = REVIEWED.search(review.get('body') or '')
    if not (is_bot(review.get('user')) and match and before.startswith(match[1]) and review.get('commit_id') == before):
        raise ci.Skipped('REVIEWED_COMMIT_UNAVAILABLE')
    return before, 'github'


def changes(patch):
    if not isinstance(patch, str) or not patch:
        raise ci.Skipped('PATCH_UNAVAILABLE')
    groups, old, added, deleted, expected, used = [], 0, 0, 0, None, [0, 0]

    def flush():
        nonlocal added, deleted
        if added or deleted:
            groups.append((old - deleted, deleted, added))
            added, deleted = 0, 0

    for line in patch.split('\n'):
        match = HUNK.match(line)
        if match:
            flush()
            if expected is not None and used != expected:
                raise ci.Skipped('PATCH_UNAVAILABLE')
            old = int(match[1])
            expected = [int(match[2] or 1), int(match[4] or 1)]
            if expected[0] == 0:
                old += 1
            used = [0, 0]
        elif line.startswith('\\ No newline at end of file') or not line:
            continue
        elif expected is None or line[0] not in ' +-':
            raise ci.Skipped('PATCH_UNAVAILABLE')
        elif line[0] == '-':
            old += 1
            deleted += 1
            used[0] += 1
        elif line[0] == '+':
            added += 1
            used[1] += 1
        else:
            flush()
            old += 1
            used[0] += 1
            used[1] += 1
    flush()
    if expected is None or used != expected:
        raise ci.Skipped('PATCH_UNAVAILABLE')
    return groups


def commit_detail(github, sha):
    detail, files = None, []
    for page in range(1, 32):
        value = github.api(f'commits/{sha}?per_page=100&page={page}')
        ci.need(value['sha'] == sha and isinstance(value['files'], list))
        detail = value if detail is None else detail
        files.extend(value['files'])
        if len(value['files']) < 100:
            return dict(detail, files=files)
    raise ci.Skipped('COMMIT_FILES_INCOMPLETE')


def overlaps(edits, start, end):
    return any((at <= end and at + removed - 1 >= start) if removed else start <= at <= end + 1
               for at, removed, added in edits)


def survives(github, row, head):
    files = github.api(f"compare/{row['before']}...{head}")['files']
    ci.need(isinstance(files, list))
    if len(files) >= 300:
        raise ci.Skipped('COMMIT_FILES_INCOMPLETE')
    files = [f for f in files if f.get('filename') == row['path']]
    ci.need(len(files) <= 1)
    return bool(files) and overlaps(changes(files[0].get('patch')), row['start_line'], row['end_line'])


def pair(github, row, comment):
    commits = github.pages(f"pulls/{row['pr']}/commits")
    if len(commits) >= 250:
        raise ci.Skipped('PR_COMMIT_HISTORY_LIMIT')
    shas = [value['sha'] for value in commits]
    ci.need(all(isinstance(sha, str) and SHA.fullmatch(sha) for sha in shas) and len(set(shas)) == len(shas))
    if row['before'] not in shas:
        raise ci.Skipped('REVIEWED_COMMIT_NOT_IN_PR')
    later = shas[shas.index(row['before']) + 1:]
    if not later:
        raise ci.Skipped('NO_LATER_COMMIT')
    previous, start, end = row['before'], row['start_line'], row['end_line']
    for sha in later:
        detail = commit_detail(github, sha)
        if [p.get('sha') for p in detail.get('parents', [])] != [previous]:
            raise ci.Skipped('NONLINEAR_PR_HISTORY')
        previous = sha
        matches = [f for f in detail['files'] if f.get('filename') == row['path'] or f.get('previous_filename') == row['path']]
        if not matches:
            continue
        ci.need(len(matches) == 1)
        file = matches[0]
        if file.get('status') == 'renamed' or file.get('filename') != row['path']:
            raise ci.Skipped('FLAGGED_FILE_RENAMED')
        edits = changes(file.get('patch'))
        if overlaps(edits, start, end):
            occurred = instant(detail['commit']['committer']['date'])
            if c.time_key(occurred) <= c.time_key(instant(comment['created_at'])):
                raise ci.Skipped('HUNK_CHANGED_BEFORE_REVIEW')
            if not survives(github, row, shas[-1]):
                raise ci.Skipped('FIX_REVERTED_IN_PR')
            row.update(after=sha, fix_occurred=occurred)
            return
        offset = sum(added - removed for at, removed, added in edits if at < start)
        start, end = start + offset, end + offset
    raise ci.Skipped('FLAGGED_HUNK_UNCHANGED')


def collect(github, cache):
    results, seen = [], set()
    for comment in github.pages('pulls/comments'):
        if comment.get('in_reply_to_id') or not is_bot(comment.get('user')) or MARKER not in (comment.get('body') or ''):
            continue
        identity = integer(comment['id'])
        if identity in seen:
            continue
        seen.add(identity)
        results.append(finding(github, comment, cache))
    return results


def finding(github, comment, cache):
    identity = integer(comment['id'])
    row = dict(repo=github.repo, finding_id=identity, before=None, after=None, reason=None,
               red_reproduced=False, proven=False, promoted=False)
    try:
        badge = BADGE.search(comment['body'])
        if not badge:
            raise ci.Skipped('PRIORITY_BADGE_UNAVAILABLE')
        match = re.fullmatch(r'https://github\.com/' + re.escape(github.repo) + r'/pull/(\d+)#discussion_r' + str(identity), comment['html_url'], re.I)
        ci.need(match is not None)
        number = integer(int(match[1]))
        before, metadata = reviewed_commit(github, comment, number, cache)
        if comment.get('side') != 'RIGHT' or comment.get('start_side') not in (None, 'RIGHT'):
            raise ci.Skipped('LEFT_SIDE_LOCATION_UNSUPPORTED')
        end = integer(comment['original_line'])
        start = integer(comment.get('original_start_line') or end)
        path = comment['path']
        ci.need(start <= end and isinstance(path, str) and path and not path.startswith('/') and '..' not in Path(path).parts)
        row.update(pr=number, finding_url=comment['html_url'], before=before, priority='P' + badge[1],
                   path=path, start_line=start, end_line=end, review_metadata=metadata)
        pair(github, row, comment)
    except ci.Skipped as error:
        row['reason'] = str(error)
    except SOURCE_ERRORS:
        row['reason'] = 'INVALID_OR_UNAVAILABLE_METADATA'
    return row


def replay_plan(clone, timeout, revision='HEAD'):
    paths = ci.run(['git', '-C', str(clone), 'ls-tree', '-r', '--name-only', revision, '.github/workflows'], timeout).decode().splitlines()
    plans = {}
    for path in paths:
        if not re.fullmatch(r'\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml', path):
            continue
        content = ci.run(['git', '-C', str(clone), 'show', revision + ':' + path], timeout).decode('utf-8')
        try:
            for names, block in ci.workflow_jobs(content):
                body = '\n'.join(block)
                steps = [ci.scalar(s) for s in re.findall(r'^      (?:- |  )name:\s*(.+)$', body, re.M)]
                steps += ['Run ' + ci.scalar(s) for s in re.findall(r'^      (?:- |  )run:\s*([^|>\s].*)$', body, re.M)]
                for lines in re.findall(r'^      (?:- |  )run:\s*\|-? *\n((?: {10}.*\n| *\n)+)', body + '\n', re.M):
                    steps.append('Run ' + '\n'.join(line[10:] for line in lines.splitlines() if line.strip()))
                for name in names:
                    for step in steps:
                        try:
                            plan = ci.workflow_plan(content, dict(name=name, steps=[dict(name=step, conclusion='failure')]))
                            plans[c.encoded(plan)] = dict(plan, workflow=path, job=name)
                        except ci.Skipped:
                            continue
        except ci.Skipped:
            continue
    if len(plans) > 1:
        raise ci.Skipped('CI_COMMAND_AMBIGUOUS')
    return next(iter(plans.values())) if plans else declared_plan(clone, timeout, revision)


def declared_plan(clone, timeout, revision):
    def show(path):
        try:
            return ci.run(['git', '-C', str(clone), 'show', revision + ':' + path], timeout)
        except ci.Skipped:
            return None
    try:
        config = tomllib.loads((show('coldbox.toml') or b'').decode('utf-8'))
    except (UnicodeError, tomllib.TOMLDecodeError):
        config = {}
    checks, commands = config.get('checks', {}), config.get('commands', {})
    test = commands.get('test') if isinstance(commands, dict) and isinstance(checks, dict) else None
    if isinstance(test, str) and checks.get('test') is not False:
        source, command = 'coldbox.toml', test
    elif show('.fleet/verify.sh') in regression_adapter.VERIFY_RECIPES:
        source, command = '.fleet/verify.sh', 'python3 -m unittest discover -s tests'
    else:
        raise ci.Skipped('CI_COMMAND_UNAVAILABLE')
    argv, kind = ci.supported(command)
    return dict(command=argv, source_command=command, kind=kind, runtime_name='python3', setup=[],
                workflow=source, job='test')


def reverse_local(clone, before, after, plan, temporary, timeout, proof):
    files = ci.run(['git', '-C', str(clone), 'diff', '--name-only', '-z', before, after], timeout).decode().split('\0')
    tests = [name for name in files if name and (re.search(r'(^|/)(test_[^/]*|[^/]*_test)\.py$', name)
             or re.search(r'(^|/)(tests?|fixtures?)/', name))]
    patch = c.repo_patch(clone, before, after, ['.', *[':(exclude,literal)' + name for name in tests]], timeout)
    patch_file = temporary / 'product.patch'
    patch_file.write_bytes(patch)
    check = temporary / 'check.py'
    c.require(c.check_unchanged(check, proof['check_script']), 'retained check changed during replay')
    command = proof['check_command']
    if plan['command'][1:2] == ['-m']:
        command = shlex.quote(plan['command'][0]) + ' -I -S {check}'
    reversed_row = replay_isolation.replay_tree(clone, after, temporary / 'reversed', command.replace('{check}', shlex.quote(str(check))),
                                 proof['expected_failure'], timeout, patch_file)
    c.require(c.check_unchanged(check, proof['check_script']), 'retained check changed during replay')
    proof['reversed'] = reversed_row
    proof['patch_sha256'] = hashlib.sha256(patch).hexdigest()
    proof['status'], proof['verification'] = c.replay_state(proof['before'], proof['after'], reversed_row)


def attempt(row, root, timeout):
    if row['reason']:
        return
    prior_artifact = row.get('artifact')
    prior_red = row['red_reproduced']
    local = c.on_replay_host()
    if not local and not callable(getattr(ci, 'vm_replay_tree', None)):
        row['reason'] = 'VM_EXECUTOR_UNAVAILABLE'
        return
    with tempfile.TemporaryDirectory(prefix='review-', dir=root) as name, \
            replay_isolation.source_workspace(root, 'review-sources-') as workspace:
        temporary = Path(name)
        proof, plan = None, None
        try:
            try:
                clone = learn_replay.SourceProjects(None, workspace, None, timeout).mirror(
                    row['repo'], (row['before'], row['after']))
            except subprocess.CalledProcessError:
                raise ci.Skipped('read operation unavailable') from None
            except c.ColdfixError as error:
                raise ci.Skipped(str(error)) from None
            plan = replay_plan(clone, timeout, row['before'])
            if local:
                proof = ci.replay_pair(clone, row['before'], row['after'], plan, temporary, timeout,
                                       executor=replay_isolation.replay_tree)
                if proof['verification'] == 'proven':
                    try:
                        reverse_local(clone, row['before'], row['after'], plan, temporary, timeout, proof)
                    except c.ColdfixError as error:
                        raise ci.Skipped('reversed replay unavailable: ' + str(error)) from None
                if proof['verification'] == 'proven':
                    before_root = c.repo_root(clone, row['before'], timeout)
                    after_root = c.repo_root(clone, row['after'], timeout)
                    if not before_root or not after_root or set(before_root.split(',')).isdisjoint(after_root.split(',')):
                        raise ci.Skipped('REPOSITORY_ROOT_UNAVAILABLE')
                    proof['repo_root'] = after_root
            else:
                proof = workflow_replay.replay_pair(clone, row['before'], row['after'], plan, temporary, timeout)
                if 'before' not in proof:
                    proof['before'] = dict(proof['probe'])
                proof.setdefault('after', dict(commit='', tree='', status='INCOMPLETE', exit=None, stdout='', stderr=''))
            row['red_reproduced'] = proof['before']['status'] == 'FAIL'
            row['proven'] = proof['verification'] == 'proven'
            reversal_failed = 'reversed' in proof and proof['after']['status'] == 'PASS'
            failed = proof['before'] if not row['red_reproduced'] else proof['reversed'] if reversal_failed else proof['after']
            row['reason'] = None if row['proven'] else ci.diagnostic(failed)['reason']
        except ci.Skipped as error:
            row.update(proven=False, reason=str(error))
        except subprocess.TimeoutExpired:
            row.update(proven=False, reason='REPLAY_TIMEOUT')
        except SOURCE_ERRORS:
            row.update(proven=False, reason='REPLAY_METADATA_UNAVAILABLE')
        if proof is not None:
            row['red_reproduced'] = prior_red or proof['before']['status'] == 'FAIL'
            incomplete = row['reason'] or any(proof[side]['status'] == 'INCOMPLETE' for side in ('before', 'after'))
            if prior_artifact and incomplete and all(proof[side]['status'] == 'INCOMPLETE' for side in ('before', 'after')):
                return
            prior_history = list(row.get('artifacts', []))
            try:
                for side in [side for side in ('probe', 'before', 'after', 'reversed') if side in proof]:
                    proof[side]['stderr'] = c.encoded(ci.diagnostic(proof[side])).strip()
                    proof[side]['stdout'] = c.encoded(dict(status=proof[side]['status'])).strip()
                artifact = 'review-proof-' + uuid.uuid4().hex + '.json'
                if prior_artifact and incomplete:
                    row['artifacts'] = prior_history + [artifact]
                else:
                    row['artifact'] = artifact
                    if prior_artifact:
                        row['artifacts'] = prior_history + [prior_artifact]
                c.atomic_create(root / artifact, c.encoded(dict(pair=row, plan=plan, replay=proof,
                    promotion_hold='Independent held-out evidence is required; the source finding cannot grade its lesson.')))
            except SOURCE_ERRORS:
                if prior_artifact:
                    row['artifact'] = prior_artifact
                else:
                    row.pop('artifact', None)
                if prior_history:
                    row['artifacts'] = prior_history
                else:
                    row.pop('artifacts', None)
                row['red_reproduced'] = prior_red
                row.update(proven=False, reason='REPLAY_METADATA_UNAVAILABLE')


def resume(args):
    root = private_store(args.store)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    report = json.loads(c.read_text(args.resume), object_pairs_hook=c.unique_object)
    ci.need(isinstance(report, dict) and report.get('version') == 1 and isinstance(report.get('results'), list))
    rows = report['results']
    for row in rows:
        ci.need(isinstance(row, dict) and isinstance(row.get('reason'), (str, type(None)))
                and type(row.get('retry_order', 0)) is int
                and all(type(row.get(key)) is bool for key in ('red_reproduced', 'proven', 'promoted')))
        if retryable(row['reason']) and row.get('before') and row.get('after'):
            ci.need(owned(row.get('repo'))
                    and isinstance(row.get('before'), str) and SHA.fullmatch(row['before'])
                    and isinstance(row.get('after'), str) and SHA.fullmatch(row['after']))
    due = sorted((row for row in rows if retryable(row['reason']) and row.get('before') and row.get('after')
                  and not settled(row.get('retried'))),
                 key=lambda row: row.get('retry_order', 0))
    order = max((row.get('retry_order', 0) for row in rows), default=0)
    report['skipped_by_class'] = skip_classes(rows)
    if not due:
        report['deferred'] = 0
        return report, Path(args.resume).parent
    output = root / ('codex-review-' + uuid.uuid4().hex)
    output.mkdir(mode=0o700)
    for row in rows:
        extra = row.get('artifacts', [])
        ci.need(isinstance(extra, list))
        for artifact in ([row['artifact']] if row.get('artifact') else []) + extra:
            ci.need(isinstance(artifact, str) and re.fullmatch(r'review-proof-[0-9a-f]{32}\.json', artifact))
            source = c.ordinary_path(Path(args.resume).parent / artifact)
            ci.need(source.is_file() and not source.is_symlink())
            if not (output / artifact).exists():
                c.atomic_create(output / artifact, c.read_text(source))
    path = output / 'codex-review-source.json'
    c.atomic_create(path, c.encoded(report))
    progress = getattr(args, 'progress', None)
    if progress:
        c.atomic_create(progress, c.encoded(dict(report=str(path))), replace=Path(progress).exists())
    budget = getattr(args, 'budget', None)
    deadline = None if budget is None else time.monotonic() + budget
    report['deferred'] = 0
    for index, row in enumerate(due):
        if learn_replay.expired(deadline):
            report['deferred'] = len(due) - index
            c.atomic_create(path, c.encoded(report), replace=True)
            break
        previous = row.get('retried')
        row['reason'] = None
        attempt(row, output, args.timeout)
        row['retried'] = stamp(previous)
        order += 1
        row['retry_order'] = order
        report['counts'].update(red_reproduced=sum(r['red_reproduced'] for r in rows),
                                proven=sum(r['proven'] for r in rows),
                                skipped=sum(r['reason'] is not None for r in rows))
        report['skipped_by_reason'] = dict(Counter(r['reason'] for r in rows if r['reason']))
        report['skipped_by_class'] = skip_classes(rows)
        c.atomic_create(path, c.encoded(report), replace=True)
    return report, output


def execute(args):
    if getattr(args, 'resume', None):
        return resume(args)
    root = private_store(args.store)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    output = root / ('codex-review-' + uuid.uuid4().hex)
    output.mkdir(mode=0o700)
    client = API(root / 'codex-review-http')
    repos = list(dict.fromkeys(repo_name(repo) for repo in args.repo))
    if args.owner:
        ci.need(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]*', args.owner))
        repos += [repo_name(row['full_name']) for row in client.pages('user/repos?affiliation=owner')
                  if row['owner']['login'].casefold() == args.owner.casefold() and row['full_name'] not in repos]
    repos = list(dict.fromkeys(repos))
    ci.need(repos and all(owned(repo, args.owner) for repo in repos))
    results, incomplete = [], []
    for index, repo in enumerate(repos):
        print(f'codex-review-source repository {index + 1}/{len(repos)}', file=sys.stderr, flush=True)
        try:
            rows = collect(GitHub(repo, client), Path(args.cache) if args.cache else None)
            for row in rows:
                attempt(row, output, args.timeout)
            results.extend(rows)
        except ci.Skipped as error:
            incomplete.append(dict(repo=repo, reason=str(error)))
        except SOURCE_ERRORS:
            incomplete.append(dict(repo=repo, reason='SOURCE_UNAVAILABLE'))
    counts = dict(findings_found=len(results), fixed_in_pr_pairs=sum(row['after'] is not None for row in results),
                  red_reproduced=sum(row['red_reproduced'] for row in results), proven=sum(row['proven'] for row in results),
                  promoted=sum(row['promoted'] for row in results), skipped=sum(row['reason'] is not None for row in results))
    report = dict(version=1, counts=counts, skipped_by_reason=dict(Counter(row['reason'] for row in results if row['reason'])),
                  skipped_by_class=skip_classes(results),
                  observed_at=c.new_record()['created'], api_gets=client.requests, core_remaining=client.remaining, core_reserve=RESERVE,
                  repositories=repos, incomplete=incomplete, enumeration_complete=not incomplete,
                  cached_review_receipts=sum(row.get('review_metadata') == 'coldvault' for row in results),
                  results=results, promotion_policy='Never grade a lesson on its source finding. No promotion without independent held-out proof.')
    c.atomic_create(output / 'codex-review-source.json', c.encoded(report))
    return report, output


def markdown(report):
    counts = report['counts']
    lines = ['# Codex review source', '',
             f"Findings found {counts['findings_found']}; fixed-in-PR pairs {counts['fixed_in_pr_pairs']}; red reproduced {counts['red_reproduced']}; proven {counts['proven']}; promoted {counts['promoted']}.", '',
             f"API GETs {report['api_gets']}; core requests remaining {report['core_remaining']}; reserve {RESERVE}.",
             f"Cached Coldvault review receipts reused {report['cached_review_receipts']}.",
             f"Enumeration complete for selected repositories: {report['enumeration_complete']}.", '',
             '| Skipped reason | Count |', '|---|---:|']
    lines += [f'| {reason} | {count} |' for reason, count in sorted(report['skipped_by_reason'].items())]
    lines += ['', report['promotion_policy'], '',
              'Run `python3 codex_review_source.py --owner OWNER --store .coldfix` to collect and attempt replay. Raw GET snapshots and proof artifacts stay in the private store. Reuse that store to resume the snapshot; select a new private `.coldfix` directory for fresh metadata.', '',
              'On the replay host (`COLDFIX_REPLAY_HOST`), replay uses the local executor and requires the fix-reversed check. Elsewhere it uses the CI-learning VM executor and holds pairs as `VM_EXECUTOR_UNAVAILABLE` if that executor is unavailable; reviewed code never runs on the host. Supported unittest plans run the original command, and proof requires failure after reversing the fix. Commands use the conservative Actions workflow parser. Nonlinear history, left-side locations, renames, and missing patches are held explicitly.', '',
              'See [codex-review-source.json](codex-review-source.json) for pair commits, finding links, and incomplete sources.', '']
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', action='append', default=[])
    parser.add_argument('--owner')
    parser.add_argument('--cache', help='optional local mirror of GitHub pull request notes')
    parser.add_argument('--store', default='.coldfix')
    parser.add_argument('--resume', help='retry unavailable pairs from an existing private report')
    parser.add_argument('--timeout', type=float, default=120)
    args = parser.parse_args(argv)
    try:
        ci.need(args.timeout > 0 and math.isfinite(args.timeout))
        report, output = execute(args)
        c.atomic_create(output / 'codex-review-source.md', markdown(report))
        print(c.encoded(dict(report=str(output / 'codex-review-source.json'), markdown=str(output / 'codex-review-source.md'), **report['counts'])).strip())
    except (ci.Skipped, *SOURCE_ERRORS, subprocess.TimeoutExpired):
        print('codex-review-source: invalid input or unavailable source; no source output printed', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
