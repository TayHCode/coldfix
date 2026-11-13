#!/usr/bin/env python3
"""Continue an interrupted experiment in a new directory, preserving prior evidence."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

import coldrecall_epochs as epochs


def recovery(source, previous):
    meta = json.loads((previous / 'meta.json').read_text())
    for name, expected in meta['source_hashes'].items():
        path = source / name
        if path.is_absolute() and not path.is_relative_to(source) or '..' in Path(name).parts:
            raise ValueError('source path is outside the checkout')
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError('measured source changed: ' + name)
    if epochs.lesson_hashes(source) != meta.get('lesson_hashes'):
        raise ValueError('lesson input inventory changed or was not recorded')
    rows = {p.stem: json.loads(p.read_text()) for p in (previous / 'sessions').glob('*.json')}
    expected = {f'{v}.{t}.{r}' for v in meta['variants'] for t in meta['tasks'] for r in range(1, meta['repetitions'] + 1)}
    if not set(rows).issubset(expected) or any(row['id'] != sid for sid, row in rows.items()):
        raise ValueError('unexpected saved sessions')
    interruptions = []
    for workspace in sorted((previous / 'workspaces').iterdir()):
        if workspace.name in rows:
            continue
        if workspace.name not in expected:
            raise ValueError('unexpected workspace')
        transcripts = list((previous / '.measure' / workspace.name).glob('epoch-*.stdout'))
        known = sum(epochs.measure.analyze_codex(p.read_text())['tokens']['total'] for p in transcripts)
        interruptions.append(dict(id=workspace.name, known_tokens=known, usage_complete=False))
    return meta, rows, interruptions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=epochs.ROOT)
    parser.add_argument('--previous', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    args = parser.parse_args()
    source, previous, work = args.source.resolve(), args.previous.resolve(), args.work.resolve()
    meta, rows, interruptions = recovery(source, previous)
    model = epochs.measure.resolve_model(meta['model']['role'], 'codex')
    if model != meta['model']:
        raise ValueError('resolved model differs from the measured model')
    work.mkdir(parents=True, exist_ok=False, mode=0o700)
    (work / 'sessions').mkdir()
    for sid in rows:
        shutil.copyfile(previous / 'sessions' / (sid + '.json'), work / 'sessions' / (sid + '.json'))
    meta['recovery'] = dict(previous=previous.name, completed_preserved=len(rows),
                            interruptions=meta.get('recovery', {}).get('interruptions', []) + interruptions)
    meta['jobs'] = 1
    (work / 'meta.json').write_text(json.dumps(meta, indent=2) + '\n')
    config = epochs.measure.load_climb(source / meta['experiment']['climb_config'])
    facts = epochs.measure.load_facts(source / config['facts'])
    tasks = [t for t in config['tasks'] if t['id'] in meta['tasks']]

    def panel(task):
        for repetition in range(1, meta['repetitions'] + 1):
            variants = meta['variants']
            offset = (repetition - 1) % len(variants)
            for variant in variants[offset:] + variants[:offset]:
                sid = f"{variant}.{task['id']}.{repetition}"
                if sid in rows:
                    continue
                result = epochs.session(config, model, source, work, facts, task, variant, repetition, meta['experiment']['max_epochs'])
                print(f"{sid} accepted={result['accepted']} errors={','.join(result['errors']) or 'none'}", flush=True)
    for task in tasks:
        panel(task)


if __name__ == '__main__':
    main()
