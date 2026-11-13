import argparse
import json
import re
import sys

import coldfix as c
import flakiness
from failure_ingest import SOURCE_ERRORS, gh
from github_sources import private_store

PATH = 'replay-repos.json'
SLUG = re.compile(r'[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*')
RUN = re.compile(r'https://github\.com/[^/]+/[^/]+/actions/runs/([0-9]+)(?![0-9])')
MISSING = 'recorded commit not available on GitHub'
RESERVE = 1000
DIRECT = ('review', 'correction')


def load(root):
    path = root / PATH
    if not path.exists():
        return {}
    value = json.loads(c.read_text(path), object_pairs_hook=c.unique_object)
    c.require(isinstance(value, dict) and all(isinstance(key, str) and (moved is None or isinstance(moved, str)
              and SLUG.fullmatch(moved)) for key, moved in value.items()), 'invalid replay repository map')
    return value


def record(root, changes):
    with c.lesson_lock(root, name='.replay-repos.lock'):
        moved = load(root)
        moved.update({ref: value for ref, value in changes.items() if moved.get(ref) is None})
        c.atomic_create(root / PATH, c.encoded(moved), replace=(root / PATH).exists())
    return moved


def missing(root):
    if not (root / 'learn-drafts.json').exists():
        return []
    ledger = json.loads(c.read_text(root / 'learn-drafts.json'))
    refs = []
    for entry in ledger['drafts'].values():
        for attempt in entry.get('replay_attempts') or []:
            reason = attempt.get('reason') or ''
            if reason == MISSING and attempt.get('failure'):
                refs.append(attempt['failure'])
            refs += re.findall(r'(?:^|; )([0-9a-f]{32}): ' + MISSING + '(?=;|$)', reason)
    return list(dict.fromkeys(refs))


def remaining():
    return gh(['api', 'rate_limit'])['resources']['core']['remaining']


def run_of(slug, run_id):
    value = gh(['api', f'repos/{slug}/actions/runs/{run_id}'], missing='HTTP 404')
    return value if isinstance(value, dict) else {}


def run_head(slug, run_id):
    return run_of(slug, run_id).get('head_sha')


def merges_head(slug, sha, head):
    value = gh(['api', f'repos/{slug}/commits/{sha}'], missing='HTTP 404')
    parents = value.get('parents') if isinstance(value, dict) else None
    return isinstance(parents, list) and len(parents) == 2 and isinstance(parents[1], dict) \
        and parents[1].get('sha') == head


def owner_repos(owner):
    names = gh(['api', '--paginate', 'user/repos?affiliation=owner&per_page=100', '--jq', '.[].full_name'], raw=True)
    return [name for name in names.split() if name.split('/')[0].casefold() == owner.casefold()]


def locate(failure, owned):
    metadata = flakiness.metadata(failure)
    match = RUN.match(failure.get('source_ref') or '')
    head = metadata.get('head_sha')
    if metadata.get('type') != 'ci' or not match or not isinstance(head, str):
        return None
    if run_head(failure['repo'], match[1]) == head:
        return None
    if 'repository_id' in metadata:
        value = gh(['api', f"repositories/{int(metadata['repository_id'])}"], missing='HTTP 404')
        candidates = [value['full_name']] if isinstance(value, dict) else []
    else:
        name = failure['repo'].split('/')[1].casefold()
        candidates = [slug for slug in owned() if slug.split('/')[1].casefold().startswith(name)]
    for slug in candidates:
        if slug.casefold() != failure['repo'].casefold() and SLUG.fullmatch(slug) and run_head(slug, match[1]) == head:
            return slug
    return None


def backfill(root, owner, limit):
    moved = load(root)
    _, store = c.load_store(root)
    owned_cache = []
    def owned():
        if not owned_cache:
            owned_cache.append(owner_repos(owner))
        return owned_cache[0]
    pending = sorted((ref for ref in missing(root) if moved.get(ref) is None and ref in store['failures']
                      and store['failures'][ref]['repo'].split('/')[0].casefold() == owner.casefold()),
                     key=lambda ref: ref in moved)
    checked = 0
    for ref in pending[:limit]:
        if remaining() < RESERVE:
            break
        moved[ref] = locate(store['failures'][ref], owned)
        record(root, {ref: moved[ref]})
        checked += 1
    return dict(checked=checked, moved=sum(value is not None for value in moved.values()),
                pending=len(pending) - checked)


def recorded_run(failure):
    metadata = flakiness.metadata(failure)
    if metadata.get('type') == 'ci':
        match = RUN.match(failure.get('source_ref') or '')
        run, head = match and match[1], metadata.get('head_sha')
    elif metadata.get('type') == 'coldbox' and isinstance(metadata.get('github'), dict):
        run, head = metadata['github'].get('run_id'), metadata.get('sha')
    else:
        return None
    if type(run) not in (int, str) or not str(run).isdigit() or not isinstance(head, str):
        return None
    return str(run), head


def is_ci(failure):
    return failure.get('kind') == 'ci' or flakiness.metadata(failure).get('type') in ('ci', 'coldbox')


def under(store, moved, slug):
    return sorted(ref for ref, failure in store['failures'].items()
                  if failure['repo'].casefold() == slug.casefold() and moved.get(ref) is None)


def mappable(failure):
    return recorded_run(failure) is not None if is_ci(failure) else failure.get('kind') in DIRECT


def unmapped(root, slug):
    _, store = c.load_store(root)
    return sum(mappable(store['failures'][ref]) for ref in under(store, load(root), slug))


def rename(root, old, new, budget):
    c.require(SLUG.fullmatch(old) and SLUG.fullmatch(new) and old.casefold() != new.casefold(),
              'invalid --rename repository slugs')
    c.require(budget >= 1, '--budget must be at least 1')
    value = gh(['api', f'repos/{old}'], missing='HTTP 404')
    name = value.get('full_name') if isinstance(value, dict) else None
    c.require(isinstance(name, str) and name.casefold() == new.casefold(),
              f'GitHub does not redirect {old} to {new}')
    _, store = c.load_store(root)
    refs = under(store, load(root), old)
    direct = [ref for ref in refs if store['failures'][ref].get('kind') in DIRECT and not is_ci(store['failures'][ref])]
    moved = record(root, dict.fromkeys(direct, name))
    runs = sorted((ref for ref in refs if is_ci(store['failures'][ref]) and recorded_run(store['failures'][ref])),
                  key=lambda ref: ref in moved)
    calls = 1
    for ref in runs:
        if calls >= budget:
            break
        failure = store['failures'][ref]
        run, sha = recorded_run(failure)
        value = run_of(name, run)
        calls += 1
        found = value.get('head_sha') == sha
        if not found and flakiness.metadata(failure).get('type') == 'coldbox' \
                and value.get('event') == 'pull_request' and isinstance(value.get('head_sha'), str):
            if calls >= budget:
                break
            found = merges_head(name, sha, value['head_sha'])
            calls += 1
        moved = record(root, {ref: name if found else None})
    return dict(calls=calls, unchecked=sum(ref not in moved for ref in runs), unmapped=unmapped(root, old),
                unmappable=sum(not mappable(store['failures'][ref]) for ref in refs))


def main(argv=None):
    parser = argparse.ArgumentParser(description='Map failures to the GitHub repository that still holds their run.')
    parser.add_argument('--store', required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--owner', help='hourly: map failures whose recorded commit GitHub no longer has')
    action.add_argument('--rename', nargs=2, metavar=('OLD', 'NEW'),
                        help='map failures recorded under OLD to NEW after GitHub confirms OLD now redirects to NEW')
    action.add_argument('--unmapped', metavar='OLD',
                        help='print how many failures recorded under OLD --rename can still map and has not')
    parser.add_argument('--limit', type=int, default=20)
    parser.add_argument('--budget', type=int, help='with --rename: most GitHub calls this run makes')
    args = parser.parse_args(argv)
    if args.rename and args.budget is None:
        parser.error('--rename needs --budget')
    try:
        root = private_store(args.store)
        if args.unmapped:
            print(unmapped(root, args.unmapped))
        elif args.rename:
            print(json.dumps(rename(root, *args.rename, args.budget), sort_keys=True))
        else:
            print(json.dumps(backfill(root, args.owner, args.limit), sort_keys=True))
    except SOURCE_ERRORS as error:
        print('replay-repos: ' + str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
