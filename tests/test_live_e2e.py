import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_cli
import test_incident_admission
import test_learn
import test_review_import


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get("COLDFIX_AGENTLOCKER") and test_review_import.RELEASE_CHECK,
                     "set COLDFIX_AGENTLOCKER and COLDFIX_RELEASE_CHECK for the live end-to-end test")
class LiveE2ETests(unittest.TestCase):
    run_cli = test_cli.CliTests.run_cli
    git = test_incident_admission.AdmissionCase.git
    public_inputs = test_review_import.release_stage

    def setUp(self):
        test_cli.CliTests.setUp(self)
        home = self.cwd / "home"
        home.mkdir()
        environment = patch.dict(os.environ, HOME=str(home),
            XDG_CONFIG_HOME=str(home / "config"), XDG_DATA_HOME=str(home / "data"),
            XDG_CACHE_HOME=str(home / "cache"), XDG_STATE_HOME=str(home / "state"),
            AGENTFLEET_USAGE_HISTORY=str(home / "model-usage.sqlite"))
        environment.start()
        self.addCleanup(environment.stop)
        self.repo, self.before, self.after = test_incident_admission.AdmissionCase.repo(self, name="arithmetic-fixture")
        test_learn.LearnTests.use_real_hub(self)
        self.bin = self.cwd / "bin"
        self.bin.mkdir()
        reply = self.cwd / "reply.json"
        self.rule = "Check that add sums both operands before accepting changes."
        reply.write_text(json.dumps(dict(kind="test", expected_failure="arithmetic",
            failure="Addition subtracted its operands.", fix="Sum both operands.", rule=self.rule,
            check_script='import json,sys\nfrom pathlib import Path\n'
                         'sys.path.insert(0,str(Path.cwd()))\nfrom calc import add\n'
                         'ok=add(2,3)==5\n'
                         'print(json.dumps({"status":"PASS"} if ok else '
                         '{"status":"FAIL","failure":"arithmetic"}))\n'
                         'sys.exit(0 if ok else 1)\n')))
        claude = self.bin / "claude"
        claude.write_text("#!" + sys.executable + "\nfrom pathlib import Path\n"
                          + "print(Path(" + repr(str(reply)) + ").read_text())\n")
        claude.chmod(0o700)
        model_environment = patch.dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"])
        model_environment.start()
        self.addCleanup(model_environment.stop)

    def test_learn_recall_and_retire_with_real_hub(self):
        receipt = test_review_import.review_receipt(self.before)
        receipt["findings"] = [receipt["findings"][0] + " at line " + str(line) for line in (10, 20)]
        source = self.cwd / "review.json"
        source.write_text(json.dumps(receipt))

        learned = self.run_cli("--store", str(self.store), "learn", "--format", "cold-review",
            "--input", str(source), "--repo", str(self.repo), "--before", self.before, "--after", self.after,
            *self.delivery)
        self.assertEqual(json.loads(learned.stdout), dict(ingested=2, drafted=1, proven=1, delivered=1, skipped_flaky=0))
        rule, = (self.repo / ".coldlocker/rules").glob("lesson-*.md")
        self.assertIn(rule.name, (self.repo / "AGENTS.md").read_text())
        registration = json.loads((self.repo / ".coldlocker/rules.json").read_text())[rule.stem]
        self.assertIs(registration["pending"], False)
        proof = json.loads(rule.read_text().split("```json\n", 1)[1].split("\n```", 1)[0])
        self.assertEqual(proof["replay"]["before"]["status"], "FAIL")
        self.assertEqual(proof["replay"]["after"]["status"], "PASS")
        self.assertEqual(proof["gates"], dict(privacy="PASS", release_check="PASS"))
        compiled = subprocess.run([sys.executable, str(ROOT / "coldrecall.py"), "compile", "--repo", str(self.repo)],
            text=True, capture_output=True, cwd=self.cwd, timeout=30)
        self.assertEqual(compiled.returncode, 0, compiled.stderr)
        self.assertTrue(json.loads(compiled.stdout)["active"])
        recalled = subprocess.run([sys.executable, str(ROOT / "coldrecall.py"), "hook"],
            input=json.dumps(dict(cwd=str(self.repo), session_id="next-session",
                hook_event_name="UserPromptSubmit", prompt="Fix addition")),
            text=True, capture_output=True, cwd=self.cwd, timeout=30)
        self.assertEqual(recalled.returncode, 0, recalled.stderr)
        self.assertIn(self.rule, json.loads(recalled.stdout)["hookSpecificOutput"]["additionalContext"])

        (self.repo / "calc.py").write_text(test_incident_admission.BUGGY)
        checked = self.run_cli("--store", str(self.store), "recheck", "--repo", str(self.repo), code=1)
        self.assertEqual(json.loads(checked.stdout), dict(delivered=1, passed=0, withdrawn=1, unchecked=0, lessons=[
            dict(lesson=rule.stem.removeprefix("lesson-"), target=str(self.repo), result="withdrawn")]))
        self.assertFalse(rule.exists())
        self.assertNotIn(rule.name, (self.repo / "AGENTS.md").read_text())
        self.assertNotIn(rule.stem, json.loads((self.repo / ".coldlocker/rules.json").read_text()))
