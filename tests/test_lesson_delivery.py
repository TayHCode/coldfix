import json
import subprocess
import unittest
from unittest.mock import patch

import test_cli


class AutoDeliveryTests(unittest.TestCase):
    setUp = test_cli.CliTests.setUp
    run_cli = test_cli.CliTests.run_cli
    event = test_cli.CliTests.event
    public_inputs = test_cli.CliTests.public_inputs

    def prepare(self):
        events = '\n'.join(json.dumps(self.event(source_ref='thread:occurrence-' + str(index))) for index in range(2))
        self.run_cli('ingest-and-propose', stdin=events)
        pending = json.loads((self.store / 'pending.json').read_text())['pending']
        signature = pending[0]['group_id']
        self.assertFalse((self.store / 'approval-request.json').exists())
        repo = self.cwd / 'consumer-checkout'
        repo.mkdir()
        def git(*args):
            return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()
        git('init', '-q')
        git('config', 'user.name', 'Fixture')
        git('config', 'user.email', 'fixture@example.invalid')
        revisions = []
        for value in ('bad', 'good'):
            (repo / 'value').write_text(value)
            git('add', '.')
            git('commit', '-qm', value)
            revisions.append(git('rev-parse', 'HEAD'))
        check = self.cwd / 'check.py'
        check.write_text('import json,sys\nfrom pathlib import Path\nok=Path("value").read_text()=="good"\n'
                         'print(json.dumps({"status":"PASS"} if ok else {"status":"FAIL","failure":"value"}))\n'
                         'sys.exit(0 if ok else 1)\n')
        fixes = self.cwd / 'fixes.json'
        fix = dict(repo=str(repo), before=revisions[0], after=revisions[1], check_file=str(check),
                   expected_failure='value', kind='test', failure='The value was invalid.',
                   fix='Write the expected value.', rule='Check the value before accepting changes.')
        fixes.write_text(json.dumps({signature: fix}))
        hub = self.cwd / 'hub.py'
        hub.write_text('''import json,sys
from pathlib import Path
assert '--reviewed' not in sys.argv
root=Path(__file__).parent
if sys.argv[1]=='import-rule':
    (root/'imported.md').write_bytes(Path(sys.argv[2]).read_bytes())
    (root/'catalog.json').write_text(json.dumps([dict(name=sys.argv[sys.argv.index('--name')+1], owner='coldfix', origin='coldfix', source='imported')]))
elif sys.argv[1]=='list':
    print((root/'catalog.json').read_text() if (root/'catalog.json').exists() else '[]')
elif sys.argv[1]=='remove':
    (root/'catalog.json').write_text('[]')
else:
    target=Path(sys.argv[sys.argv.index('--target')+1])
    name=sys.argv[sys.argv.index('--rule')+1]
    path=target/'.coldlocker/rules'/(name+'.md')
    path.parent.mkdir(parents=True,exist_ok=True)
    if '--withdraw' in sys.argv:
        path.unlink(missing_ok=True)
        (target/'AGENTS.md').write_text('')
        sys.exit(0)
    if (root/'block-delivery').exists(): sys.exit(1)
    body=(root/'imported.md').read_text()
    marker='<!-- coldlocker:lesson -->\\n'
    path.write_text(body if body.startswith(marker) else marker+body)
    (target/'AGENTS.md').write_text('.coldlocker/rules/'+name+'.md')
''')
        args = ['ingest-and-propose', '--fixes', str(fixes), '--agentlocker', str(hub),
                '--target', str(repo), '--exports', str(self.cwd / 'exports')]
        # Exercise the real release-check subprocess/report protocol with a fixture checker.
        inputs = self.public_inputs('incomplete')[1:]
        return args, inputs, fixes, fix, repo, signature

    def test_retry_with_new_hub_after_incompatible_hub_installed_nothing(self):
        args, inputs, fixes, fix, repo, signature = self.prepare()
        self.public_inputs('pass')
        hub = self.cwd / 'hub.py'
        compatible = self.cwd / 'compatible.py'
        compatible.write_text(hub.read_text())
        hub.write_text(hub.read_text().replace("else:\n    target=", "else:\n    sys.exit(2)\n    target="))
        first = json.loads(self.run_cli(*args, *inputs, stdin='').stdout)
        self.assertEqual(first['delivered'], [])
        self.assertFalse((repo / '.coldlocker').exists())
        self.assertFalse((repo / 'AGENTS.md').exists())
        args[args.index('--agentlocker') + 1] = str(compatible)
        retried = json.loads(self.run_cli(*args, *inputs, stdin='').stdout)
        self.assertEqual(retried['pending'], 0)
        self.assertEqual(len(retried['delivered']), 1)
        self.assertTrue((repo / 'AGENTS.md').exists())
        self.assertEqual(len(list((repo / '.coldlocker/rules').glob('*.md'))), 1)
        delivered = next((repo / '.coldlocker/rules').glob('*.md')).read_bytes()
        exported = next((self.cwd / 'exports').glob('lesson-*.md')).read_bytes()
        self.assertEqual(delivered, exported)
        self.assertEqual(delivered.count(b'<!-- coldlocker:lesson -->'), 1)

    def test_unreadable_replacement_invalidates_delivery(self):
        args, inputs, fixes, fix, repo, signature = self.prepare()
        self.public_inputs('pass')
        self.run_cli(*args, *inputs, stdin='')
        fix['check_file'] = str(self.cwd / 'unavailable.py')
        fixes.write_text(json.dumps({signature: fix}))
        self.run_cli(*args, *inputs, stdin='', code=2)
        self.assertFalse(list((repo / '.coldlocker/rules').glob('*.md')))
        self.assertEqual(len(json.loads((self.store / 'pending.json').read_text())['pending']), 1)
        no_fixes = list(args)
        del no_fixes[no_fixes.index('--fixes'):no_fixes.index('--fixes') + 2]
        self.assertEqual(json.loads(self.run_cli(*no_fixes, *inputs, stdin='').stdout)['delivered'], [])

    def test_proven_complete_lesson_delivers_without_approval_and_retries_safety_failure(self):
        args, inputs, fixes, fix, repo, signature = self.prepare()
        result = json.loads(self.run_cli(*args, *inputs, stdin='').stdout)
        self.assertEqual(result['pending'], 1)
        self.assertEqual(result['delivered'], [])
        self.assertFalse((repo / 'AGENTS.md').exists())
        self.assertFalse(list((self.cwd / 'exports').glob('*.md')))
        self.public_inputs('pass')
        result = json.loads(self.run_cli(*args, *inputs, stdin='').stdout)
        self.assertEqual(result['pending'], 0)
        self.assertEqual(len(result['delivered']), 1)
        exported, = (self.cwd / 'exports').glob('*.md')
        text = exported.read_text()
        payload = json.loads(text.split('```json\n', 1)[1].split('\n```', 1)[0])
        self.assertEqual(payload['what_went_wrong'], fix['failure'])
        self.assertEqual(payload['fix'], fix['fix'])
        self.assertEqual(payload['rule'], fix['rule'])
        self.assertTrue(payload['source_evidence'])
        self.assertEqual(payload['replay']['before']['status'], 'FAIL')
        self.assertEqual(payload['replay']['after']['status'], 'PASS')
        self.assertEqual(payload['gates'], {'privacy': 'PASS', 'release_check': 'PASS'})
        receipt_count = len(list((self.store / 'receipts').glob('*.json')))
        before_bytes = exported.read_bytes()
        again = json.loads(self.run_cli(*args, *inputs, stdin='').stdout)
        self.assertEqual(again['delivered'], result['delivered'])
        self.assertEqual(exported.read_bytes(), before_bytes)
        self.assertEqual(len(list((self.store / 'receipts').glob('*.json'))), receipt_count)
        self.assertIn('pending', (self.store / 'delivery-events.jsonl').read_text())

        other = self.cwd / 'other-checkout'
        other.mkdir()
        subprocess.run(['git', '-C', str(other), 'init', '-q'], check=True)
        wrong_target = list(args)
        wrong_target[wrong_target.index('--target') + 1] = str(other)
        blocked = json.loads(self.run_cli(*wrong_target, *inputs, stdin='').stdout)
        self.assertEqual(blocked['delivered'], [])
        self.assertEqual(blocked['pending'], 1)
        self.assertFalse((other / 'AGENTS.md').exists())
        fix['kind'] = 'lint'
        fixes.write_text(json.dumps({signature: fix}))
        changed = json.loads(self.run_cli(*args, *inputs, stdin='').stdout)
        self.assertEqual(changed['pending'], 0)
        self.assertGreater(len(list((self.store / 'receipts').glob('*.json'))), receipt_count)
        updated = json.loads((self.store / 'lessons' / (changed['delivered'][0] + '.json')).read_text())
        self.assertEqual(updated['kind'], 'lint')
        clone = self.cwd / 'migrated-checkout'
        subprocess.run(['git', 'clone', '--quiet', '--local', str(repo), str(clone)], check=True)
        previous_receipts = len(list((self.store / 'receipts').glob('*.json')))
        fix['repo'] = str(clone)
        fix.pop('failure')
        fix.pop('fix')
        fixes.write_text(json.dumps({signature: fix}))
        args[args.index('--target') + 1] = str(clone)
        migrated = json.loads(self.run_cli(*args, *inputs, stdin='').stdout)
        self.assertEqual(migrated['pending'], 0)
        self.assertFalse(list((repo / '.coldlocker/rules').glob('*.md')))
        self.assertGreater(len(list((self.store / 'receipts').glob('*.json'))), previous_receipts)
        previous_receipts = len(list((self.store / 'receipts').glob('*.json')))
        self.run_cli(*args, *inputs, stdin='')
        self.assertEqual(len(list((self.store / 'receipts').glob('*.json'))), previous_receipts)
        fix['rule'] = 'password=example-private-canary'
        fixes.write_text(json.dumps({signature: fix}))
        blocked = json.loads(self.run_cli(*args, *inputs, stdin='').stdout)
        self.assertEqual(blocked['delivered'], [])
        self.assertEqual(blocked['pending'], 1)
        no_fixes = list(args)
        del no_fixes[no_fixes.index('--fixes'):no_fixes.index('--fixes') + 2]
        still_blocked = json.loads(self.run_cli(*no_fixes, *inputs, stdin='').stdout)
        self.assertEqual(still_blocked['delivered'], [])
        self.run_cli('export', '--lesson', migrated['delivered'][0], '--output', 'invalidated.md', code=2)
        self.assertFalse((self.cwd / 'invalidated.md').exists())
        for target in (repo, clone):
            self.assertFalse(list((target / '.coldlocker/rules').glob('*.md')))
        fix['rule'] = 'Check the value before accepting changes.'
        fix['expected_failure'] = 'different-failure'
        fixes.write_text(json.dumps({signature: fix}))
        blocked = json.loads(self.run_cli(*args, *inputs, stdin='').stdout)
        self.assertEqual(blocked['delivered'], [])
        pending = json.loads((self.store / 'pending.json').read_text())['pending']
        self.assertNotEqual(pending[0]['replay']['verification'], 'proven')

        fix['expected_failure'] = 'value'
        fixes.write_text(json.dumps({signature: fix}))
        active = json.loads(self.run_cli(*args, *inputs, stdin='').stdout)['delivered'][0]
        self.run_cli('lesson', '--revise', active, '--status', 'retired')
        other_hub = self.cwd / 'other-hub.py'
        other_hub.write_text('raise RuntimeError("the current catalog must not be used for old withdrawal")')
        retired = json.loads(self.run_cli('ingest-and-propose', '--agentlocker', str(other_hub), stdin='').stdout)
        self.assertEqual(retired['delivered'], [])
        self.assertFalse(list((clone / '.coldlocker/rules').glob('*.md')))
        self.assertIn('retired', (self.store / 'pending.json').read_text())

    def test_corrupt_runtime_journals_are_preserved_and_fail_closed(self):
        self.store.mkdir()
        for name in ('invalidations.json', 'deliveries.json'):
            with self.subTest(name=name):
                path = self.store / name
                path.write_text('{broken')
                result = self.run_cli('ingest-and-propose', stdin='', code=2)
                self.assertIn('invalid', result.stderr)
                self.assertEqual(path.read_text(), '{broken')
                path.unlink()

    def test_pending_intent_withdraws_after_post_install_journal_failure(self):
        args, inputs, fixes, fix, repo, signature = self.prepare()
        self.public_inputs('pass')
        shim = self.cwd / 'journal-failure.py'
        shim.write_text('import sys,json\n'
            + 'sys.path.insert(0,' + repr(str(test_cli.CLI.parent)) + ')\n'
            + 'import coldfix as c\noriginal=c.atomic_create\n'
            + 'def fail(path, content, replace=False):\n'
            + '    if path.name=="deliveries.json" and any(row["state"]=="delivered" for row in json.loads(content).values()): raise OSError("injected journal failure")\n'
            + '    return original(path,content,replace=replace)\n'
            + 'c.atomic_create=fail\nsys.exit(c.main())\n')
        with patch.object(test_cli, 'CLI', shim):
            result = json.loads(self.run_cli(*args, *inputs, stdin='').stdout)
        self.assertEqual(result['delivered'], [])
        records = json.loads((self.store / 'deliveries.json').read_text())
        self.assertEqual(next(iter(records.values()))['state'], 'pending')
        self.assertTrue(list((repo / '.coldlocker/rules').glob('*.md')))
        active = json.loads((self.store / 'pending.json').read_text())['pending'][0]['replay']['id']
        self.run_cli('lesson', '--revise', active, '--status', 'retired')
        self.run_cli('ingest-and-propose', stdin='')
        self.assertFalse(list((repo / '.coldlocker/rules').glob('*.md')))
        self.assertTrue(all(row['state']=='withdrawn' for row in json.loads((self.store/'deliveries.json').read_text()).values()))

    def test_failed_delivery_export_is_owned_for_a_newer_retry(self):
        args, inputs, fixes, fix, repo, signature = self.prepare()
        self.public_inputs('pass')
        blocker = self.cwd / 'block-delivery'
        blocker.touch()
        result = json.loads(self.run_cli(*args, *inputs, stdin='').stdout)
        self.assertEqual(result['delivered'], [])
        exported, = (self.cwd / 'exports').glob('*.md')
        previous = exported.read_bytes()
        self.assertEqual(next(iter(json.loads((self.store/'deliveries.json').read_text()).values()))['state'], 'pending')
        blocker.unlink()
        fix['rule'] = 'Validate the expected value before accepting the result.'
        fixes.write_text(json.dumps({signature: fix}))
        retry = json.loads(self.run_cli(*args, *inputs, stdin='').stdout)
        self.assertEqual(retry['pending'], 0)
        self.assertEqual(len(retry['delivered']), 1)
        self.assertNotEqual(exported.read_bytes(), previous)
        self.assertIn(fix['rule'], exported.read_text())
