import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

import test_cli
import test_draft


class LearnTests(unittest.TestCase):
    run_cli = test_cli.CliTests.run_cli
    event = test_cli.CliTests.event
    git = test_draft.DraftTests.git
    public_inputs = test_cli.CliTests.public_inputs

    def setUp(self):
        test_draft.DraftTests.setUp(self)
        self.reply.write_text(json.dumps(self.response))
        self.inputs = self.cwd / 'events.jsonl'
        self.inputs.write_text('\n'.join(json.dumps(self.event(error='value mismatch', source_ref=ref))
                                         for ref in ('third', 'fourth')))
        self.delivery = []

    def learn(self, code=0, extra=()):
        return json.loads(self.run_cli('--store', str(self.store), 'learn', '--repo', str(self.repo), '--before', self.before,
            '--after', self.after, '--input', str(self.inputs), *self.delivery, *extra, code=code).stdout)

    def use_real_hub(self):
        selected = os.environ.get('COLDFIX_AGENTLOCKER')
        if not selected:
            self.skipTest('set COLDFIX_AGENTLOCKER to a real agentlocker checkout')
        source = Path(selected)
        hub = self.cwd / 'hub'
        files = subprocess.check_output(['git', '-C', str(source), 'ls-files', '-z'])
        for raw in files.split(b'\0'):
            if raw:
                relative = Path(os.fsdecode(raw))
                destination = hub / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source / relative, destination)
        self.delivery = ['--agentlocker', str(hub / 'hub.py'), '--target', str(self.repo),
                         '--exports', str(self.cwd / 'exports'), *self.public_inputs()[1:]]

    def prepare_review(self):
        self.use_real_hub()
        gate = self.cwd / 'gate.py'
        gate.write_text(gate.read_text().replace("'example' in a.private_repo", "'example/repo' in a.private_repo"))
        self.store = self.cwd / 'learn-store'
        self.inputs.write_text(json.dumps(dict(repo='example/repo', pr=1, sha=self.before,
            model='fixture', verdict='FAIL', summary='Value mismatch', findings=['Value mismatch at line 10', 'Value mismatch at line 20'])))
        return ('--format', 'cold-review')

    def test_ingest_draft_prove_and_deliver_with_real_hub(self):
        options = self.prepare_review()
        self.assertEqual(self.learn(extra=options), dict(skipped_flaky=0, ingested=2, drafted=1, proven=1, delivered=1))
        rule, = (self.repo / '.coldlocker/rules').glob('lesson-*.md')
        self.assertIn('Verification: proven', rule.read_text())
        self.assertIn(rule.name, (self.repo / 'AGENTS.md').read_text())
        registration = json.loads((self.repo / '.coldlocker/rules.json').read_text())[rule.stem]
        self.assertIs(registration['pending'], False)
        fixes, = (self.store / 'drafts').glob('learn-*.json')
        self.assertEqual(fixes.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(json.loads(fixes.read_text())), 1)
        self.calls.unlink()
        self.assertEqual(self.learn(extra=options), dict(skipped_flaky=0, ingested=0, drafted=0, proven=0, delivered=0))
        self.assertFalse(self.calls.exists())

    def test_wrong_draft_is_rejected_and_not_delivered(self):
        self.use_real_hub()
        self.response['check_script'] = 'print(\'{"status":"PASS"}\')\n'
        self.reply.write_text(json.dumps(self.response))
        self.assertEqual(self.learn(code=2), dict(skipped_flaky=0, ingested=2, drafted=1, proven=0, delivered=0))
        pending, = json.loads((self.store / 'pending.json').read_text())['pending']
        self.assertEqual(pending['replay']['status'], 'FAIL')
        self.assertFalse((self.repo / '.coldlocker').exists())
        self.reply.write_text(json.dumps(dict(self.response, check_script=self.script)))
        self.assertEqual(self.learn(), dict(skipped_flaky=0, ingested=0, drafted=1, proven=1, delivered=1))

    def test_covered_group_is_not_redrafted(self):
        test_draft.DraftTests.draft(self)
        test_draft.DraftTests.prove(self)
        self.calls.unlink()
        self.assertEqual(self.learn(code=2), dict(skipped_flaky=0, ingested=2, drafted=0, proven=0, delivered=0))
        self.assertFalse(self.calls.exists())

    def test_failed_draft_does_not_block_other_groups(self):
        self.inputs.write_text('\n'.join(json.dumps(self.event(error=error, source_ref=ref))
            for error in ('another mismatch', 'value mismatch') for ref in ('third', 'fourth')))
        claude = self.bin / 'claude'
        claude.write_text(claude.read_text().replace('print(Path(os.environ["DRAFT_REPLY"]).read_text())',
                     'print("{}" if "another mismatch" in Path(os.environ["DRAFT_PROMPT"]).read_text() '
                     'else Path(os.environ["DRAFT_REPLY"]).read_text())'))
        self.assertEqual(self.learn(code=2), dict(skipped_flaky=0, ingested=4, drafted=1, proven=1, delivered=0))
