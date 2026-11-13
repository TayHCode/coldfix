import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import coldrecall_epochs_resume as resume


class RecoveryTests(unittest.TestCase):
    def test_recovery_preserves_records_and_requires_the_measured_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source'; source.mkdir()
            (source / '.coldlocker').mkdir()
            measured = source / 'driver.py'; measured.write_text('measured source')
            previous = root / 'previous'; previous.mkdir()
            (previous / 'sessions').mkdir()
            (previous / 'workspaces' / 'arm.task.2').mkdir(parents=True)
            raw = previous / '.measure' / 'arm.task.2'; raw.mkdir(parents=True)
            (raw / 'epoch-1.stdout').write_text(json.dumps(dict(type='turn.completed', usage=dict(input_tokens=10, output_tokens=2))))
            record = previous / 'sessions' / 'arm.task.1.json'
            record.write_text(json.dumps({'id': 'arm.task.1'}))
            original = record.read_bytes()
            meta = dict(source_hashes={'driver.py': hashlib.sha256(measured.read_bytes()).hexdigest()},
                        variants=['arm'], tasks=['task'], repetitions=2, lesson_hashes=resume.epochs.lesson_hashes(source))
            (previous / 'meta.json').write_text(json.dumps(meta))
            _, rows, interruptions = resume.recovery(source, previous)
            self.assertEqual(set(rows), {'arm.task.1'})
            self.assertEqual(interruptions, [dict(id='arm.task.2', known_tokens=12, usage_complete=False)])
            self.assertEqual(record.read_bytes(), original)
            lesson = source / '.coldlocker' / 'new-rule.md'
            lesson.write_text('new knowledge')
            with self.assertRaisesRegex(ValueError, 'lesson input inventory'):
                resume.recovery(source, previous)
            lesson.unlink()
            measured.write_text('changed source')
            with self.assertRaisesRegex(ValueError, 'measured source changed'):
                resume.recovery(source, previous)
