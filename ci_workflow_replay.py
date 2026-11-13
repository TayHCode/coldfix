"""Replay a selected CI workflow check at both commits and with the fix reversed."""

import json
from pathlib import Path
import shlex
import tempfile

import ci_fix_learning as ci
import ci_exact_unittest
import ci_vm_executor as vm
import coldfix as c


def summarize_by_reason(records):
    """Summarize one record per original pair, including pairs not replayed."""
    groups = {}
    for record in records:
        reason = record['reason']
        group = groups.setdefault(reason, dict(total=0, attempted=0, proven=0))
        group['total'] += 1
        proof = record.get('proof') or {}
        receipts = proof.get('receipts', {})
        if any(receipts.values()):
            group['attempted'] += 1
        if (proof.get('verification') == 'proven'
                and all(receipts.get(side) for side in ('before', 'after', 'reversed'))
                and [proof.get(side, {}).get('status') for side in ('before', 'after', 'reversed')]
                == ['FAIL', 'PASS', 'FAIL']):
            group['proven'] += 1
    for group in groups.values():
        group['reproduction_rate'] = group['proven'] / group['total']
    return dict(sorted(groups.items()))


def replay_pair(repo, before, after, plan, work, timeout, *, provision=(), builder=None):
    """Return a private proof with one confirmed lifecycle receipt per execution.

    The plan is produced by ci_fix_learning.workflow_plan. Unsupported workflow
    features remain held by that parser. No repository commands run on the host.
    """
    work = c.ordinary_path(Path(work))
    work.mkdir(parents=True, exist_ok=True, mode=0o700)
    c.require(work.stat().st_mode & 0o077 == 0, 'workflow replay directory must be private')
    provision = list(provision)
    if plan.get('setup'):
        provision.extend([['apt-get', 'update', '-qq'],
                          ['env', 'DEBIAN_FRONTEND=noninteractive', 'apt-get', 'install', '-y', '-qq',
                           'python3-venv', 'python3-pip']])
    with tempfile.TemporaryDirectory(prefix='workflow-replay-', dir=work) as name:
        root = Path(name)
        check = root / 'check.py'
        patch = root / 'fix.patch'
        patch.write_bytes(c.repo_patch(repo, before, after, [], timeout))
        if builder is None:
            builder = ci.check_script
            if plan['kind'] == 'test':
                try:
                    ci_exact_unittest.check_script(plan)
                except (ci.Skipped, KeyError):
                    pass
                else:
                    builder = ci_exact_unittest.check_script
        script = builder(plan)
        c.atomic_create(check, script)
        receipts = {}

        def run(side, revision, reverse=None):
            paths = []
            row = vm.replay_tree(repo, revision, root / side,
                'python3 -I -S ' + shlex.quote(str(check)), ci.EXPECTED, timeout,
                reverse, receipts=paths, provision=provision)
            if not c.check_unchanged(check, script):
                raise ci.Skipped('retained check changed during replay')
            receipts[side] = paths
            return row

        probe = run('probe', before)
        if probe['status'] != 'FAIL':
            return dict(status='INCOMPLETE', verification='unproven', probe=probe, receipts=receipts,
                        provision=provision)
        diagnostic = ci.diagnostic(probe)
        script = builder(plan, diagnostic['diagnostic'], diagnostic['tests'])
        c.atomic_create(check, script, replace=True)
        rows = {side: run(side, revision, reverse) for side, revision, reverse in (
            ('before', before, None), ('after', after, None), ('reversed', after, patch))}
        status, verification = c.replay_state(rows['before'], rows['after'], rows['reversed'])
        return dict(status=status, verification=verification, probe=probe, receipts=receipts,
                    check_sha256=c.digest(script), provision=provision, **rows)
