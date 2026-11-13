"""Reproduce public open-source CI fix pairs at their exact commits in disposable VM guests.

Git fetches anonymously with credential helpers disabled, so only public repositories
can be cloned; the GitHub API must also report each repository as public.
"""

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import uuid

import ci_actions_replay as actions
import ci_fix_learning as ci
import ci_vm_executor as vm
import ci_workflow_replay as replay
import coldfix as c
import github_sources as gh

SEEDS = Path(__file__).resolve().parent / 'docs' / 'measurements' / 'ci-replay-pass2b.json'
LINK = re.compile(r'https://github\.com/([^/\s]+/[^/\s]+)/actions/runs/(\d+)/attempts/\d+#job-(\d+)\Z')
SIDES = ('probe', 'before', 'after', 'reversed')


def read_seeds(path):
    value = json.loads(c.read_text(c.ordinary_path(path)))
    return seed_pairs(value['red_reproduced'] if isinstance(value, dict) else value)


def seed_pairs(rows):
    c.require(isinstance(rows, list) and rows, 'seed file names no pairs')
    seeds = {}
    for row in rows:
        c.require(isinstance(row, dict) and isinstance(row.get('links'), list) and row['links'], 'invalid seed')
        repo, before, after, patch = row.get('repo'), row.get('before'), row.get('after'), row.get('patch')
        before = before.get('commit') if isinstance(before, dict) else None
        after = after.get('commit') if isinstance(after, dict) else None
        match = LINK.fullmatch(str(row['links'][0]))
        c.require(isinstance(repo, str) and gh.REPO.fullmatch(repo) and match and match[1] == repo
                  and isinstance(before, str) and gh.SHA.fullmatch(before)
                  and (isinstance(after, str) and gh.SHA.fullmatch(after) and patch is None
                       or isinstance(patch, str) and patch.strip() and 'after' not in row), 'invalid seed')
        seed = dict(repo=repo, before=before, after=after, link=row['links'][0], run=int(match[2]),
                    job=int(match[3]), reason='seed ' + str(row.get('status', 'unknown')).lower(), patch=patch)
        seeds.setdefault((repo, before, after or c.digest(patch), seed['job']), seed)
    return list(seeds.values())


def public_metadata(github, seed):
    repo = github.api(f"repos/{seed['repo']}")
    if repo.get('private') is not False or repo.get('visibility') != 'public':
        raise ci.Skipped('repository is not public')
    run = github.api(f"repos/{seed['repo']}/actions/runs/{seed['run']}")
    job = github.api(f"repos/{seed['repo']}/actions/jobs/{seed['job']}")
    ci.need(isinstance(run, dict) and isinstance(job, dict))
    if ci.context(run, seed['repo'])[0] != seed['repo'].casefold():
        raise ci.Skipped('fork checkout unsupported')
    ci.need(run.get('id') == seed['run'] and job.get('run_id') == seed['run']
            and run.get('head_sha') == seed['before'] == job.get('head_sha'))
    try:
        c.public_text(job['name'], [])
        ci.need(not c.privacy_counts(job['name']))
    except (c.ColdfixError, KeyError, TypeError):
        raise ci.Skipped('job metadata privacy screen blocked') from None
    if not re.fullmatch(r'\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml', str(run.get('path'))):
        raise ci.Skipped('unsupported workflow source path')
    return run, job


def anonymous_clone(url, revisions, clone, timeout):
    env = dict(PATH=os.defpath, HOME=str(clone.parent), LANG='C.UTF-8', LC_ALL='C.UTF-8',
               GIT_TERMINAL_PROMPT='0', GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
               GIT_ASKPASS='/bin/false', SSH_ASKPASS='/bin/false')
    git = ['git', '-c', 'core.hooksPath=' + os.devnull, '-c', 'credential.helper=']
    ci.run([*git, 'init', '--quiet', str(clone)], timeout, env)
    ci.run([*git, '-C', str(clone), 'fetch', '--quiet', '--depth=1', '--no-tags', '--', url, *revisions], timeout, env)
    return env


def commit_fix(clone, before, patch, env, timeout):
    """Commit a supplied fix onto the failing commit so replay can prove it like a green commit."""
    git = ['git', '-C', str(clone)]
    env = dict(env, GIT_INDEX_FILE=str(clone / '.git' / 'coldfix-fix.index'), GIT_AUTHOR_NAME='Coldfix',
               GIT_AUTHOR_EMAIL='coldfix@example.invalid', GIT_COMMITTER_NAME='Coldfix',
               GIT_COMMITTER_EMAIL='coldfix@example.invalid', GIT_AUTHOR_DATE='2000-01-01T00:00:00Z',
               GIT_COMMITTER_DATE='2000-01-01T00:00:00Z')
    path = clone / '.git' / 'coldfix-fix.patch'
    path.write_text(patch)
    ci.run([*git, 'read-tree', before], timeout, env)
    try:
        ci.run([*git, 'apply', '--cached', str(path)], timeout, env)
    except ci.Skipped:
        raise ci.Skipped('fix patch does not apply to the failing commit') from None
    tree = ci.run([*git, 'write-tree'], timeout, env).decode('ascii').strip()
    return ci.run([*git, 'commit-tree', tree, '-p', before, '-m', 'Coldfix verified fix'], timeout, env).decode('ascii').strip()


def screened(proof):
    """Keep statuses, commits, diagnostics and receipt paths; drop raw guest output."""
    result = dict(status=proof['status'], verification=proof['verification'], receipts=proof.get('receipts', {}),
                  check_sha256=proof.get('check_sha256'))
    for side in SIDES:
        if side in proof:
            row = proof[side]
            result[side] = dict(commit=row['commit'], tree=row['tree'], status=row['status'], exit=row['exit'],
                                **ci.diagnostic(row))
    return result


def reproduce(github, seed, work, timeout, url=None):
    record = {k: v for k, v in seed.items() if k != 'patch'}
    record.update(patch_sha256=c.digest(seed['patch']) if seed.get('patch') else None, status='SKIPPED', proof=None)
    try:
        run, job = public_metadata(github, seed)
        with tempfile.TemporaryDirectory(prefix='oss-', dir=work) as name:
            clone = Path(name) / 'clone'
            fix = seed.get('patch')
            env = anonymous_clone(url or f"https://github.com/{seed['repo']}.git",
                                  [seed['before']] + ([] if fix else [seed['after']]), clone, timeout)
            record['after'] = commit_fix(clone, seed['before'], fix, env, timeout) if fix else seed['after']
            content = ci.run(['git', '-C', str(clone), 'show', seed['before'] + ':' + run['path']], timeout, env)
            plan = actions.select_plan(content.decode('utf-8'), run['path'], job, run.get('event'))
            proof = screened(actions.replay_pair(clone, seed['before'], record['after'], plan, Path(name) / 'replay', timeout))
        record.update(status='PROVEN' if proof['verification'] == 'proven' else 'SKIPPED', proof=proof,
                      plan=dict(workflow=plan['workflow'], job=plan['job_id'], step=plan['target_step'], kind=plan['kind']))
        if record['status'] != 'PROVEN':
            failed = next((proof[s] for s in ('before', 'after', 'reversed') if s in proof
                           and proof[s]['status'] != dict(after='PASS').get(s, 'FAIL')), proof.get('probe'))
            record['skip'] = failed['reason'] if failed else 'replay incomplete'
    except (ci.Skipped, gh.RateLimited) as error:
        record['skip'] = str(error)
    except (c.ColdfixError, UnicodeError, KeyError, TypeError):
        record['skip'] = 'invalid metadata or replay source unavailable'
    return record


def markdown(report):
    lines = ['# OSS clone-at-commit replay', '',
             f"Pairs {len(report['records'])}; proven in VM {report['proven']}; repositories with a proof "
             f"{report['repositories_proven']}. Every clone was anonymous and every repository public.", '',
             '| Outcome | Pairs |', '| --- | ---: |']
    lines += [f'| {k} | {v} |' for k, v in report['outcomes'].items()]
    lines += ['', '| Seed reason | Total | Attempted | Proven |', '| --- | ---: | ---: | ---: |']
    lines += [f"| {k} | {v['total']} | {v['attempted']} | {v['proven']} |" for k, v in report['by_reason'].items()]
    lines += ['', '| Repository | Before | After | Before | After | Reversed | Result | Receipts |',
              '| --- | --- | --- | --- | --- | --- | --- | --- |']
    for row in report['records']:
        proof = row['proof'] or {}
        sides = [proof.get(s, {}).get('status', '-') for s in ('before', 'after', 'reversed')]
        receipts = ' '.join(path for s in ('before', 'after', 'reversed') for path in proof.get('receipts', {}).get(s, []))
        result = row['status'] if row['status'] == 'PROVEN' else row['status'] + ': ' + row['skip']
        lines.append(f"| {row['repo']} | `{row['before'][:12]}` | `{(row['after'] or 'patch')[:12]}` | "
                     + ' | '.join(sides) + f" | {result} | {receipts or '-'} |")
    return '\n'.join(lines) + '\n'


def write(output, records):
    proven = [r for r in records if r['status'] == 'PROVEN']
    outcomes = Counter('proven' if r['status'] == 'PROVEN' else r['skip'] for r in records)
    report = dict(version=1, records=records, proven=len(proven), repositories_proven=len({r['repo'] for r in proven}),
                  outcomes=dict(outcomes.most_common()), by_reason=replay.summarize_by_reason(records))
    c.atomic_create(output / 'report.json', c.encoded(report), replace=True)
    c.atomic_create(output / 'report.md', markdown(report), replace=True)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seeds', default=str(SEEDS), help='pass report, or a list of pairs whose fix is a later commit or a supplied patch; default pass 2b')
    parser.add_argument('--repo', action='append', default=[], help='OWNER/NAME; repeat to select seed repositories')
    parser.add_argument('--timeout', type=float, default=900, help='per-guest timeout in seconds, default 900')
    parser.add_argument('--store', default='.coldfix')
    args = parser.parse_args(argv)
    try:
        seeds = [s for s in read_seeds(args.seeds) if not args.repo or s['repo'] in args.repo]
        c.require(seeds and args.timeout > 0, 'no seed pairs selected')
        root = gh.private_store(args.store)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        work = c.ordinary_path(root / 'oss-replay')
        work.mkdir(mode=0o700, exist_ok=True)
        c.require(work.stat().st_mode & 0o077 == 0, 'replay directory must be private')
        output = work / ('run-' + uuid.uuid4().hex)
        output.mkdir(mode=0o700)
    except (c.ColdfixError, OSError, ValueError, KeyError, TypeError) as error:
        print('oss_replay: ' + str(error), file=sys.stderr)
        return 2
    records = []
    for index, seed in enumerate(seeds, 1):
        print(f"oss_replay: pair {index}/{len(seeds)}", file=sys.stderr, flush=True)
        try:
            records.append(reproduce(gh.PublicGitHub(seed['repo']), seed, output, args.timeout))
        except vm.CleanupFailed as error:
            write(output, records)
            print(str(error), file=sys.stderr)
            return 2
        report = write(output, records)
    print(json.dumps(dict(report=str(output / 'report.json'), markdown=str(output / 'report.md'),
                          pairs=len(records), proven=report['proven'],
                          repositories_proven=report['repositories_proven']), sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
