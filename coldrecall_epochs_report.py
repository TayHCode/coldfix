#!/usr/bin/env python3
"""Summarize complete executable-memory trials without exposing transcripts."""
import argparse
import json
from pathlib import Path
import statistics as stats



def report(works, allow_incomplete=False):
    metadata = [json.loads((work / 'meta.json').read_text()) for work in works]
    identity = ('source_head', 'source_hashes', 'lesson_hashes', 'model', 'experiment', 'repetitions', 'variants')
    if not metadata or any(any(meta.get(k) != metadata[0].get(k) for k in identity) for meta in metadata):
        raise ValueError('inconsistent experiment identity')
    meta = metadata[0]
    tasks = [task for run in metadata for task in run['tasks']]
    if len(tasks) != len(set(tasks)):
        raise ValueError('duplicate task panels')
    rows = [json.loads(path.read_text()) for work in works for path in sorted((work / 'sessions').glob('*.json'))]
    expected = {(v, t, r) for v in meta['variants'] for t in tasks for r in range(1, meta['repetitions'] + 1)}
    actual = {(r['variant'], r['task'], r['repetition']) for r in rows}
    if actual - expected or len(rows) != len(actual):
        raise ValueError('unexpected or duplicate sessions')
    missing = sorted(expected - actual)
    if missing and not allow_incomplete:
        raise ValueError('missing sessions')
    arms = {}
    for variant in meta['variants']:
        group = [r for r in rows if r['variant'] == variant]
        totals = [sum(r['tokens']['total'] for r in group if r['repetition'] == n) for n in range(1, meta['repetitions'] + 1)]
        errors = sum(bool(r['errors']) or not r['usage_complete'] for r in group)
        arms[variant] = dict(sessions=len(group), missing_sessions=len(tasks) * meta['repetitions'] - len(group), accepted=sum(r['accepted'] for r in group), errors=errors,
                             accepted_by_task={t: sum(r['accepted'] for r in group if r['task'] == t) for t in tasks},
                             epoch_count=sum(r['epochs'] for r in group), tokens_per_repetition=totals,
                             mean=stats.mean(totals), median=stats.median(totals), minimum=min(totals), maximum=max(totals),
                             sd=stats.stdev(totals) if len(totals) > 1 else None,
                             tokens_per_accepted=sum(totals) / sum(r['accepted'] for r in group) if any(r['accepted'] for r in group) else None)
    baseline = arms.get('reflex-current')
    for variant, arm in arms.items():
        if baseline and variant.startswith('contract-'):
            quality = all(arm['accepted_by_task'][t] >= baseline['accepted_by_task'][t] for t in tasks)
            gain = baseline['mean'] - arm['mean']
            noise = max(arm['sd'], baseline['sd']) if arm['sd'] is not None and baseline['sd'] is not None else None
            arm['change_percent'] = 100 * (arm['mean'] / baseline['mean'] - 1) if baseline['mean'] else None
            arm['verdict'] = ('incomplete' if arm['errors'] or baseline['errors'] or arm['missing_sessions'] or baseline['missing_sessions'] or noise is None else
                              'reject-quality' if not quality else 'keep' if gain > noise else 'reject-noise' if gain >= -noise else 'reject-worse')
    paired_repetitions = [n for n in range(1, meta['repetitions'] + 1)
                          if len([r for r in rows if r['repetition'] == n]) == len(meta['variants']) * len(tasks)
                          and all(not r['errors'] and r['usage_complete'] for r in rows if r['repetition'] == n)]
    paired_arms = {}
    for variant in meta['variants']:
        group = [r for r in rows if r['variant'] == variant and r['repetition'] in paired_repetitions]
        totals = [sum(r['tokens']['total'] for r in group if r['repetition'] == n) for n in paired_repetitions]
        paired_arms[variant] = dict(sessions=len(group), accepted=sum(r['accepted'] for r in group),
                                   accepted_by_task={t: sum(r['accepted'] for r in group if r['task'] == t) for t in tasks},
                                   mean=stats.mean(totals) if totals else None, median=stats.median(totals) if totals else None,
                                   minimum=min(totals) if totals else None, maximum=max(totals) if totals else None,
                                   sd=stats.stdev(totals) if len(totals) > 1 else None)
    interruptions = [item for run in metadata for item in run.get('recovery', {}).get('interruptions', []) + run.get('stopped_early', {}).get('interruptions', [])]
    if interruptions:
        for name, arm in arms.items():
            if name.startswith('contract-'):
                arm['provisional_verdict'] = arm.get('verdict', 'no-baseline')
                arm['verdict'] = 'incomplete-interrupted'
    for arm in arms.values():
        if arm['errors'] or arm['missing_sessions']:
            for key in ('mean', 'median', 'minimum', 'maximum', 'sd', 'tokens_per_accepted', 'change_percent'):
                if key in arm:
                    arm[key] = None
    return dict(metadata=metadata, sessions=rows, arms=arms, interruptions=interruptions,
                paired_repetitions=paired_repetitions, paired_arms=paired_arms, missing_sessions=missing)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', action='append', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--allow-incomplete', action='store_true', help='report missing slots explicitly; never promote a partial run')
    args = parser.parse_args()
    result = report(args.work, args.allow_incomplete)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(f"recorded_sessions={len(result['sessions'])} interrupted_attempts={len(result['interruptions'])} missing_sessions={len(result['missing_sessions'])}")
    for name, arm in result['arms'].items():
        paired = result['paired_arms'][name]
        print(f"{name}: accepted={arm['accepted']}/{arm['sessions']} errors={arm['errors']} paired_repetitions={result['paired_repetitions']} paired_mean_tokens={paired['mean']} verdict={arm.get('verdict', 'reference')}")


if __name__ == '__main__':
    main()
