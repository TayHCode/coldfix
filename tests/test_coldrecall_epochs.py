import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import coldrecall_epochs as epochs


CLI = r'''import json, sys
from pathlib import Path
a = sys.argv[1:]
store = Path(a[a.index('--store') + 1])
if '--help' in a:
    print('{capture,show} --tail')
    raise SystemExit(0)
if 'capture' in a:
    def opt(k): return a[a.index(k) + 1]
    if '--log-file' in a:
        text = Path(opt('--log-file')).read_text()
        text = ''.join(text.splitlines(keepends=True)[-2:]) if not Path(__file__).with_name('fixed').exists() else '\n'.join(text.split('\n')[-3:])
    else:
        text = opt('--error')
    p = store / 'failures'; p.mkdir(parents=True, exist_ok=True)
    (p / 'one.json').write_text(json.dumps({'error': text, 'command': opt('--command')}))
    print(json.dumps({'failure_id': 'one'}))
elif 'show' in a:
    record = json.loads((store / 'failures' / 'one.json').read_text())
    print(json.dumps({'id': 'one'} if Path(__file__).with_name('fixed').exists() else record))
'''

AGENT = r'''import json, sys
from pathlib import Path
p = Path('calls')
n = int(p.read_text()) + 1 if p.exists() else 1
p.write_text(str(n))
prompt = sys.argv[-1]
Path('prompt' + str(n)).write_text(prompt)
print(json.dumps({'type':'item.completed','item':{'type':'file_change','status':'failed'}}))
print(json.dumps({'type':'item.completed','item':{'type':'command_execution','command':'sed -i x file','status':'failed','exit_code':1}}))
if not Path('bad-usage').exists() or n == 1:
    print(json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':3}}))
'''

class EpochTests(unittest.TestCase):
    def test_probes_detect_and_fix_real_cli_behavior(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'coldfix.py').write_text(CLI)
            self.assertEqual(epochs.probe(root, 5), [epochs.TAIL, epochs.SHOW])
            (root / 'fixed').touch()
            self.assertEqual(epochs.probe(root, 5), [])

    def test_preflight_reports_missing_requested_interfaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'coldfix.py').write_text("print('{capture,ingest,propose}')")
            self.assertEqual(epochs.probe(root, 5), [])
            self.assertEqual(epochs.probe(root, 5, task_id='show-failure'), [epochs.SHOW])
            self.assertEqual(epochs.probe(root, 5, task_id='capture-tail'), [epochs.TAIL])

    def test_duplicate_variants_fail_before_creating_any_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / 'run'
            result = subprocess.run([sys.executable, str(epochs.ROOT / 'coldrecall_epochs.py'), 'run',
                                     '--work', str(work), '--variant', 'without', '--variant', 'without'], capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn(b'duplicate variants', result.stderr)
            self.assertFalse(work.exists())

    def test_lesson_manifest_tracks_changes_and_file_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / '.coldlocker'; folder.mkdir()
            rule = folder / 'rule.md'; rule.write_text('first')
            before = epochs.lesson_hashes(root)
            rule.write_text('changed')
            self.assertNotEqual(epochs.lesson_hashes(root), before)
            rule.write_text('first')
            extra = folder / 'added.md'; extra.write_text('added')
            self.assertNotEqual(epochs.lesson_hashes(root), before)
            extra.unlink()
            self.assertEqual(epochs.lesson_hashes(root), before)
            rule.unlink()
            self.assertNotEqual(epochs.lesson_hashes(root), before)

    def run_session(self, fail=False):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        workspace = root / 'workspace'; workspace.mkdir()
        if fail:
            (workspace / 'bad-usage').touch()
        agent = root / 'agent.py'; agent.write_text(AGENT)
        config = {'variants': [{'id': 'without', 'params': None}], 'agent_command': [sys.executable, str(agent)],
                  'agent_sandbox': 'workspace-write', 'session_timeout_seconds': 5, 'acceptance_timeout_seconds': 5}
        task = {'id': 'case', 'kind': 'knowledge', 'prompt': 'Original task.', 'base': 'unused', 'accept': {}}
        with patch.object(epochs.measure, 'start_workspace', return_value=workspace), \
             patch.object(epochs.measure, 'prepare_codex', return_value=(root, None, root / 'log')), \
             patch.object(epochs.measure, 'accepted', return_value=True) as accepted, \
             patch.object(epochs, 'probe', side_effect=[[epochs.TAIL], []]):
            record = epochs.session(config, {'model': 'fixture', 'effort': 'default'}, root, root, [], task, 'contract-after', 1, 2)
        return root, workspace, record, accepted

    def test_two_fresh_epochs_sum_metrics_and_pass_only_diagnostics(self):
        root, workspace, record, accepted = self.run_session()
        self.assertTrue(record['accepted'])
        self.assertEqual((record['epochs'], record['tokens']['total'], record['tool_calls'], record['edits_failed']), (2, 26, 4, 4))
        self.assertEqual((workspace / 'prompt1').read_text(), 'Original task.')
        self.assertEqual((workspace / 'prompt2').read_text(), epochs.prompt('Original task.', [epochs.TAIL]))
        self.assertNotIn('turn.completed', (workspace / 'prompt2').read_text())
        self.assertTrue((root / '.measure' / 'contract-after.case.1' / 'epoch-2.stdout').exists())
        accepted.assert_called_once()

    def test_failed_epoch_is_not_accepted_and_known_usage_is_counted(self):
        _, _, record, accepted = self.run_session(fail=True)
        self.assertFalse(record['accepted'])
        self.assertEqual(record['errors'], ['invalid-transcript-or-usage'])
        self.assertEqual(record['tokens']['total'], 13)
        self.assertEqual(record['epochs'], 2)
        accepted.assert_not_called()

    def test_transcript_validation(self):
        self.assertFalse(epochs.valid_transcript('{"type":"turn.completed","usage":{}}'))
        self.assertFalse(epochs.valid_transcript('{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":2}}\ninvalid'))
        self.assertTrue(epochs.valid_transcript('{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":2},"text":"a\u2028b"}'))

    def test_existing_work_is_never_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'existing'; path.mkdir(); (path / 'keep').write_text('keep')
            result = subprocess.run([sys.executable, str(epochs.ROOT / 'coldrecall_epochs.py'), 'run', '--work', str(path)], capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertEqual((path / 'keep').read_text(), 'keep')

if __name__ == '__main__':
    unittest.main()
