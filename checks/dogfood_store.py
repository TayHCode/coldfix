#!/usr/bin/env python3
"""Read a real private store, exercise a temporary copy, and print only counts."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def snapshot(root):
    result = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise RuntimeError('store contains a symlink')
        if path.is_file():
            result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', type=Path, required=True)
    parser.add_argument('--repo', type=Path,
                        help='source checkout to verify; defaults to the checker checkout')
    args = parser.parse_args()
    source = args.store.resolve(strict=True)
    source_root = args.repo.resolve(strict=True) if args.repo else ROOT
    if not source_root.is_dir() or not (source_root / 'coldfix.py').is_file() \
            or not (source_root / 'coldrecall.py').is_file():
        parser.error('--repo must be a Coldfix source checkout')
    try:
        source_status = subprocess.run(
            ['git', '-C', str(source_root), 'status', '--porcelain=v1', '--untracked-files=all'],
            capture_output=True, text=True)
    except OSError:
        raise RuntimeError('source checkout could not be verified') from None
    if source_status.returncode != 0:
        raise RuntimeError('source checkout could not be verified')
    if source_status.stdout:
        raise RuntimeError('source checkout has uncommitted changes')
    before = snapshot(source)

    def cli(store, *args, stdin=None, expected=0):
        result = subprocess.run([sys.executable, '-B', str(source_root / 'coldfix.py'), '--store', str(store), *args],
                                input=stdin, text=True, capture_output=True)
        if result.returncode != expected:
            raise RuntimeError('private CLI check failed: ' + args[0])
        return json.loads(result.stdout) if result.stdout else None

    try:
        live = cli(source, 'status', '--json')
        with tempfile.TemporaryDirectory(prefix='coldfix-dogfood-') as temporary:
            store = Path(temporary) / 'store'
            shutil.copytree(source, store)
            assert snapshot(store) == before, 'snapshot changed during copy'
            assert cli(store, 'status', '--json') == live
            grouped = cli(store, 'group')
            assert grouped['members'] == live['totals']['events'] > 0
            assert grouped['groups'] == live['totals']['groups']
            records = sorted((store / 'failures').glob('*.json'))
            event = json.loads(records[0].read_text())
            selected = {key: event[key] for key in ('command', 'error', 'repo', 'agent')}
            selected.update(kind=event.get('kind', 'test'), occurred=event.get('occurred', event['created']),
                            source_ref='dogfood:isolated-reimport')
            line = json.dumps(selected) + '\n'
            imported = cli(store, 'ingest', stdin=line)
            assert imported['imported_records'] == 1
            duplicate = cli(store, 'ingest', stdin=line)
            assert duplicate['imported_records'] == 0 and duplicate['skipped_duplicates'] == 1
            assert cli(store, 'group')['members'] == grouped['members'] + 1
            stable = snapshot(store)
            cli(store, 'ingest', stdin=line + '{}\n', expected=2)
            assert snapshot(store) == stable, 'malformed batch wrote to store'
            assert cli(store, 'status', '--json')['totals']['events'] == grouped['members'] + 1
            repo = Path(temporary) / 'repo'
            cloned = subprocess.run(['git', 'clone', '--quiet', '--no-hardlinks', str(source_root), str(repo)],
                                    capture_output=True)
            assert cloned.returncode == 0, 'local clone failed'
            registry = json.loads((repo / '.coldlocker/rules.json').read_text())
            sys.dont_write_bytecode = True
            sys.path.insert(0, str(source_root))
            import coldrecall
            proofs = [coldrecall.read_rule(repo / '.coldlocker/rules' / (name + '.md'))[0]
                      for name, entry in registry.items() if entry.get('lesson') and entry.get('pending') is False]
            assert proofs, 'no delivered lesson'
            for proof in proofs:
                lesson_id, revision = proof['lesson'].split(':')[2:]
                retained = [json.loads(path.read_text()) for path in (store / 'lessons').glob('*.json')]
                if not any(row.get('lesson_id') == lesson_id and row.get('id') == revision for row in retained):
                    raise RuntimeError('registered lesson identity is absent from store')
                for ref in proof['source_evidence']:
                    if not (store / 'failures' / (ref.split(':')[-1] + '.json')).is_file():
                        raise RuntimeError('registered lesson evidence is absent from store')
            def recall(*args, stdin=None):
                result = subprocess.run([sys.executable, '-B', str(source_root / 'coldrecall.py'), *args],
                                        input=stdin, text=True, capture_output=True)
                assert result.returncode == 0, 'recall CLI failed'
                return result.stdout
            compiled = json.loads(recall('compile', '--repo', str(repo)))
            table = json.loads((repo / '.coldrecall/reflex.json').read_text())
            witnesses = []
            for proof in proofs:
                facts = proof.get('facts', [dict(id=None, text=proof['rule'])])
                for fact in facts:
                    ref = proof['lesson'] + '#' + fact['id'] if fact['id'] is not None else proof['lesson']
                    assert ref in compiled['active']
                    witnesses.append(table['atoms'][ref]['witness']['result'])
                    if witnesses[-1] != 'pass':
                        raise RuntimeError('dogfood requires passing lesson witnesses')
                    anchor = next(key[5:] for key, refs in table['anchors'].items() if ref in refs)
                    looked_up = recall('lookup', '--repo', str(repo), '--path', anchor)
                    assert fact['text'] in looked_up
                    payload = json.dumps(dict(cwd=str(repo), hook_event_name='PreToolUse',
                                              tool_input=dict(file_path=anchor)))
                    hooked = json.loads(recall('hook', stdin=payload))
                    assert fact['text'] in hooked['hookSpecificOutput']['additionalContext']
            print(json.dumps(dict(recall_status='PASS', delivered_proofs=len(proofs),
                                  regressions=len(compiled['regressions']), unanchored=len(compiled['unanchored']),
                                  witnesses=witnesses)))
            print(json.dumps(dict(status='PASS', totals=live['totals'], by_source=live['by_source'],
                                  copied_files=len(before), imported_records=1,
                                  malformed_batch_preserved=True)))
    finally:
        if snapshot(source) != before:
            raise RuntimeError('live store changed during verification; investigate concurrent writers')
    print(json.dumps(dict(live_store_unchanged=True)))


if __name__ == '__main__':
    try:
        main()
    except RuntimeError as error:
        print('INCOMPLETE: ' + str(error), file=sys.stderr)
        raise SystemExit(2)
