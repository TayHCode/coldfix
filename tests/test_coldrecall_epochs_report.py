import json
from pathlib import Path
import tempfile
import unittest

import coldrecall_epochs_report as report


class ReportTests(unittest.TestCase):
    def fixture(self, root):
        (root / 'sessions').mkdir()
        meta = dict(source_head='abc', source_hashes={}, model={}, experiment={}, repetitions=2,
                    variants=['reflex-current', 'contract-after'], tasks=['one'])
        (root / 'meta.json').write_text(json.dumps(meta))
        for variant, tokens in [('reflex-current', 100), ('contract-after', 50)]:
            for repetition in (1, 2):
                row = dict(variant=variant, task='one', repetition=repetition, tokens={'total': tokens},
                           errors=[], usage_complete=True, accepted=True, epochs=1)
                (root / 'sessions' / f'{variant}.{repetition}.json').write_text(json.dumps(row))

    def test_report_requires_complete_runs_and_rejects_false_gains(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            result = report.report([root])
            self.assertEqual(result['arms']['contract-after']['verdict'], 'keep')
            self.assertEqual(result['arms']['contract-after']['change_percent'], -50)
            meta_path = root / 'meta.json'
            meta = json.loads(meta_path.read_text())
            meta['recovery'] = dict(interruptions=[{'id': 'lost', 'usage_complete': False}])
            meta_path.write_text(json.dumps(meta))
            self.assertEqual(report.report([root])['arms']['contract-after']['verdict'], 'incomplete-interrupted')
            del meta['recovery']
            meta_path.write_text(json.dumps(meta))
            path = root / 'sessions' / 'contract-after.1.json'
            row = json.loads(path.read_text())
            row['accepted'] = False
            path.write_text(json.dumps(row))
            self.assertEqual(report.report([root])['arms']['contract-after']['verdict'], 'reject-quality')
            row['errors'] = ['timeout']
            path.write_text(json.dumps(row))
            result = report.report([root])
            self.assertEqual(result['arms']['contract-after']['verdict'], 'incomplete')
            self.assertIsNone(result['arms']['contract-after']['mean'])
            self.assertEqual(result['paired_repetitions'], [2])
            path.unlink()
            with self.assertRaisesRegex(ValueError, 'missing'):
                report.report([root])
            partial = report.report([root], allow_incomplete=True)
            self.assertEqual(partial['paired_repetitions'], [2])
            self.assertEqual(len(partial['missing_sessions']), 1)
            self.assertIsNone(partial['arms']['contract-after']['mean'])

    def test_interrupted_contract_only_run_has_no_comparison_baseline(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            meta_path = root / 'meta.json'
            meta = json.loads(meta_path.read_text())
            meta['variants'] = ['contract-after']
            meta['recovery'] = dict(interruptions=[{'id': 'lost', 'usage_complete': False}])
            meta_path.write_text(json.dumps(meta))
            for path in (root / 'sessions').glob('reflex-current.*.json'):
                path.unlink()
            arm = report.report([root], allow_incomplete=True)['arms']['contract-after']
            self.assertEqual(arm['verdict'], 'incomplete-interrupted')
            self.assertEqual(arm['provisional_verdict'], 'no-baseline')

    def test_panels_with_different_lesson_inventories_cannot_be_combined(self):
        with tempfile.TemporaryDirectory() as temporary:
            roots = [Path(temporary) / name for name in ('first', 'second')]
            for index, root in enumerate(roots):
                root.mkdir()
                self.fixture(root)
                path = root / 'meta.json'
                meta = json.loads(path.read_text())
                meta['lesson_hashes'] = {'rule.md': str(index)}
                path.write_text(json.dumps(meta))
            with self.assertRaisesRegex(ValueError, 'inconsistent experiment identity'):
                report.report(roots)
