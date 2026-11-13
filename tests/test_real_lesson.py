import json
from pathlib import Path
import subprocess
import sys
import unittest


class RealLessonTests(unittest.TestCase):
    def test_unicode_lesson_check_uses_real_ingest(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([sys.executable, str(root / "checks/jsonl_unicode.py")],
                                cwd=root, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"status": "PASS"})
