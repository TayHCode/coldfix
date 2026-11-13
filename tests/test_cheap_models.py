import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import coldfix as c
import learn_daily
import test_draft


ROOT = Path(__file__).resolve().parents[1]
PRICIER = ("gpt-6.1-terra", "gpt-6.1-sol", "gpt-6-astra", "claude-sonnet-5-5", "claude-opus-5-5")
CHEAP = dict(claudeAgent="claude-haiku-5-5", codex="gpt-6-luna")
MODELS_YAML = ("bulk: {provider: claudeAgent, model: claude-haiku-5-5, effort: default}\n"
               "bulk-codex: {provider: codex, model: gpt-6-luna, effort: default}\n"
               "builder-cheap: {provider: codex, model: gpt-6.1-terra, effort: high}\n"
               "tiers:\n  builder: [builder-cheap]\n  light: [bulk, bulk-codex]\n")


def model_process(command, **kwargs):
    if command[0] == "codex":
        Path(command[command.index("--output-last-message") + 1]).write_text('{"drafts": []}')
    return subprocess.CompletedProcess(command, 0, stdout=json.dumps(dict(is_error=False, structured_output=dict(drafts=[]))))


class CheapModelGuardTests(unittest.TestCase):
    def test_timer_paths_never_start_a_model_other_than_luna_haiku_or_jev(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            models = work / "models.yaml"
            models.write_text(MODELS_YAML)
            with patch.dict(os.environ, FLEET_MODELS_FILE=str(models)):
                for provider in CHEAP:
                    for model in PRICIER:
                        with self.subTest(path="learn_daily ask", provider=provider, model=model), \
                                patch("learn_daily.subprocess.run", side_effect=model_process) as run:
                            with self.assertRaisesRegex(c.ColdfixError, "Luna, Haiku or Jev"):
                                learn_daily.ask(dict(provider=provider, model=model), "prompt", learn_daily.DRAFTS, work)
                            run.assert_not_called()
                    with self.subTest(path="learn_daily ask", provider=provider, model=CHEAP[provider]), \
                            patch("learn_daily.subprocess.run", side_effect=model_process) as run:
                        learn_daily.ask(dict(provider=provider, model=CHEAP[provider]), "prompt", learn_daily.DRAFTS, work)
                        run.assert_called_once()
                terra = subprocess.CompletedProcess([], 0, stdout=json.dumps(dict(provider="codex", model="gpt-6.1-terra")))
                with self.subTest(path="learn_daily route", model="gpt-6.1-terra"), \
                        patch("learn_daily.subprocess.run", return_value=terra):
                    with self.assertRaises(c.ColdfixError):
                        learn_daily.route("builder-cheap")

            roots = work / "transcripts"
            roots.mkdir()
            scan = [sys.executable, str(ROOT / "transcript_mistakes.py"), str(roots), "--ledger", str(work / "ledger.sqlite"),
                    "--store", str(work / "store"), "--endpoint", "https://jev.invalid", "--limit", "1", "--max-calls", "1"]
            for model in (*PRICIER, *CHEAP.values()):
                with self.subTest(path="transcript scan", model=model):
                    result = subprocess.run([*scan, "--model", model], env={**os.environ, "JEV_API_KEY": "unused"},
                                            capture_output=True, text=True)
                    self.assertEqual(result.returncode, 2)
                    self.assertIn("only on a Jev model", result.stderr)
            with self.subTest(path="transcript scan", model="jev-latest"):
                result = subprocess.run([*scan, "--model", "jev-latest"], env={**os.environ, "JEV_API_KEY": "unused"},
                                        capture_output=True, text=True)
                self.assertEqual((result.returncode, json.loads(result.stdout)["scanned"]), (0, 0), result.stderr)

        fixture = test_draft.DraftTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        for model in PRICIER:
            with self.subTest(path="coldfix draft", model=model):
                (fixture.bin / "acme").write_text("#!" + sys.executable + "\nimport json\nprint(json.dumps("
                                                  + repr(dict(provider="claudeAgent", model=model)) + "))\n")
                result = fixture.draft(code=2)
                self.assertIn("model route", result.stderr)
                self.assertFalse(fixture.calls.exists())


if __name__ == "__main__":
    unittest.main()
