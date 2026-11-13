import json
from pathlib import Path
import subprocess
import sys
import unittest


class CodexFindingLessonTests(unittest.TestCase):
    def test_unclosed_fence_check_preserves_final_section(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([sys.executable, str(root / "checks/codex_unclosed_fence.py")],
                                cwd=root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"status": "PASS"})
