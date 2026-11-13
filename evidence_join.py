#!/usr/bin/env python3
"""Reconcile recorded failures and replay receipts into a private proof queue."""
import argparse
import json
import re
import sys
from collections import defaultdict

import coldfix as c
import failure_ingest

PR = re.compile(r'https://github\.com/([^/]+/[^/]+)/pull/([1-9][0-9]*)(?:[#?].*)?$', re.I)
SHA = re.compile(r'[0-9a-f]{40,64}$')
COMMIT = re.compile(r'https://github\.com/([^/]+/[^/]+)/commit/([0-9a-f]{40,64})$', re.I)


def candidate(row):
    repo = row['repo'].lower()
    match = PR.fullmatch(row.get('thread', '')) or PR.fullmatch(row.get('source_ref', ''))
    pr = int(match[2]) if match and match[1].lower() == repo else None
    head = None
    failed_trace = False
    if row['source'] == 'failure-ingest':
        try:
            meta = json.loads(row['error'])
        except (ValueError, TypeError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        try:
            source = {k: v for k, v in meta.items() if k != 'classification'}
            if meta.get('type') == 'ci' and meta.get('repo', '').lower() == repo:
                failure_ingest.ci_event(source)
                pr = meta.get('pr')
                head = meta.get('head_sha')
            elif meta.get('type') == 'verify-trace' and meta.get('repo', '').lower() == repo:
                failure_ingest.trace_event(source)
                if meta['status'] == 'fail':
                    failed_trace = True
                    head = meta.get('head')
        except (c.ColdfixError, KeyError, TypeError, ValueError):
            pass
    if not isinstance(head, str) or not SHA.fullmatch(head):
        head = None
    return repo, pr, head, failed_trace


def reconcile(store, memberships):
    # Exact source references identify repeat imports; PR membership alone does not
    # prove that two findings concern the same defect.
    events = defaultdict(list)
    for row in store['failures'].values():
        events[(row['repo'].lower(), row.get('source_ref') or 'coldfix:failure:' + row['id'])].append(row)
    fix_records = defaultdict(dict)
    for row in store['failures'].values():
        match = COMMIT.fullmatch(row.get('source_ref', ''))
        if match and match[1].lower() == row['repo'].lower() and row['command'] == 'fix commit ' + match[2]:
            fix_records[row['repo'].lower()].setdefault(match[2], []).append(row['id'])
    incidents, queue = [], []
    for (repo, ref), rows in sorted(events.items()):
        ids = sorted(row['id'] for row in rows)
        variants = {candidate(row) for row in rows}
        prs = {pr for _, pr, _, _ in variants if type(pr) is int and pr > 0}
        heads = {head for _, _, head, _ in variants if head}
        head = next(iter(heads)) if len(heads) == 1 else None
        checks = [row for row in rows if row.get('kind') == 'ci' or
                  (row.get('kind') == 'test' and candidate(row)[3])]
        fix_ref = COMMIT.fullmatch(ref)
        direct_fix = (fix_ref[2] if fix_ref and fix_ref[1].lower() == repo and
                      any(row['command'] == 'fix commit ' + fix_ref[2] for row in rows) else None)
        if (head or direct_fix) and not prs:
            prs = {number for (owner, number), commits in memberships.items()
                   if owner == repo and (head or direct_fix) in commits}
        pr = next(iter(prs)) if len(prs) == 1 else None
        edges = set(memberships.get((repo, pr), set())) if pr else set()
        # CI ingest explicitly records both the PR number and failed head.
        if pr and head and any(row['source'] == 'failure-ingest' and candidate(row)[1:3] == (pr, head) for row in rows):
            edges.add(head)
        fixes = {sha: sorted(ids) for sha, ids in fix_records[repo].items() if sha in edges and sha != head}
        # A lesson's own evidence list is the explicit incident-to-replay edge.
        receipts = []
        for lesson in store['lessons'].values():
            if not set(ids).intersection(lesson['evidence']) or not lesson['receipt']:
                continue
            receipt = store['receipts'][lesson['receipt']]
            if lesson['verification'] == 'proven' and receipt['status'] == 'PASS':
                receipts.append(receipt)
        matched = [r for r in receipts if head and r['before']['commit'] == head
                   and r['after']['commit'] in edges and r['after']['commit'] != head]
        missing = []
        if not pr or not edges or not any(sha in edges for sha in (head, direct_fix) if sha):
            missing.append('PR-to-commit edge')
        if not head:
            missing.append('observed failing revision')
        if not checks:
            missing.append('failing check')
        if not direct_fix and not fixes and not any(r['after']['commit'] in edges and r['after']['commit'] != head for r in receipts):
            missing.append('fix commit')
        if fixes and not matched:
            missing.append('fix-to-incident edge')
        if not matched:
            missing.append('replay')
        item = dict(repo=repo, source_ref=ref, failure_ids=ids, pr=pr, failing_revision=head, recorded_fix=direct_fix, candidate_fix_records=fixes)
        if missing:
            queue.append(dict(item, missing=missing))
        else:
            incidents.append(dict(item, fix_commit=matched[0]['after']['commit'], replay=matched[0]['id']))
    return dict(version=1, incidents=incidents, missing_proof=queue)


def memberships(path):
    if not path:
        return {}
    rows = json.loads(c.read_text(path), object_pairs_hook=c.unique_object)
    c.require(isinstance(rows, list), 'PR membership must be an array')
    result = {}
    for row in rows:
        c.require(isinstance(row, dict) and set(row) == {'repo', 'pr', 'commits'}, 'invalid PR membership')
        c.require(isinstance(row['repo'], str) and type(row['pr']) is int and row['pr'] > 0, 'invalid PR identity')
        c.require(isinstance(row['commits'], list) and all(isinstance(s, str) and SHA.fullmatch(s) for s in row['commits']), 'invalid commit')
        key = (row['repo'].lower(), row['pr'])
        c.require(key not in result, 'duplicate PR membership')
        result[key] = set(row['commits'])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', required=True)
    parser.add_argument('--pr-commits', help='JSON array of recorded PR identities and commit SHAs')
    args = parser.parse_args()
    try:
        root = c.ordinary_path(args.store)
        links = memberships(args.pr_commits)
        with c.lesson_lock(root):
            _, store = c.load_store(root)
            result = reconcile(store, links)
            path = root / 'evidence-join.json'
            content = c.encoded(result)
            changed = not path.exists() or c.read_text(path) != content
            if changed:
                c.atomic_create(path, content, replace=path.exists())
        print(json.dumps(dict(joined=len(result['incidents']), missing_proof=len(result['missing_proof']), changed=changed), sort_keys=True))
        return 0
    except (c.ColdfixError, OSError, ValueError, TypeError, KeyError) as exc:
        print('evidence_join: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
