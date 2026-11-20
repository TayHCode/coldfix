import json
import os
import shlex
import shutil
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import loop_setup
import replay_isolation
import test_cli


class LoopInitTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.cwd = Path(self.temporary.name)
        self.home = self.cwd / "home"
        self.bin = self.cwd / "bin"
        self.bin.mkdir()
        os.symlink(shutil.which("cat"), self.bin / "cat")
        self.cron = self.cwd / "crontab.txt"
        self.calls = self.cwd / "calls.txt"
        self.repo = self.cwd / "my repo"
        self.repo.mkdir()
        self.store = self.repo / ".coldfix"
        (self.home / ".claude" / "projects" / "demo").mkdir(parents=True)
        (self.home / ".claude" / "projects" / "demo" / "session.jsonl").write_text("{}\n")
        self.tool("gh", 'echo "gh $*" >> "$CALLS"; [ "$1 $2" = "api user" ] && echo fixture-login')
        self.tool("claude", "exit 0")
        self.tool("systemctl", "exit 1")
        self.tool("crontab", '[ "$1" = "-l" ] && { cat "$CRON" 2>/dev/null || { echo "no crontab for runner" >&2; exit 1; }; exit 0; }; cat > "$CRON"')
        self.environment = dict(HOME=str(self.home), PATH=str(self.bin), CALLS=str(self.calls),
                                CRON=str(self.cron), XDG_CONFIG_HOME=str(self.cwd / "config"),
                                COLDFIX_PRIVATE_HOSTS=str(self.cwd / "no-private-hosts"),
                                **{replay_isolation.RUNNER: "runner {script}"})
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True,
                       env=dict(self.environment, PATH=os.environ["PATH"]))
        self.setups = []
        stub = patch.object(loop_setup, "setup", side_effect=lambda args: self.setups.append(args) or dict(candidates=0))
        stub.start()
        self.addCleanup(stub.stop)

    def tool(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)

    def init(self, code=0):
        with patch.dict(os.environ, self.environment):
            return test_cli.CliTests.run_cli(self, "--store", str(self.store), "init", "--yes", "--repo", str(self.repo), code=code)

    def test_init_yes_writes_wrapper_schedules_and_runs_first_pass(self):
        (self.home / ".codex" / "sessions").mkdir(parents=True)
        value = json.loads(self.init().stdout)
        folders = [str(self.home / ".claude" / "projects"), str(self.home / ".codex" / "sessions")]
        self.assertEqual(value["config"], {"repo": str(self.repo)})
        self.assertEqual((value["loop"]["model_cli"], value["loop"]["transcripts"]), ("claude", folders))
        wrapper = self.store.resolve() / "model-cmd"
        self.assertEqual(value["loop"]["model_command"], shlex.quote(str(wrapper)))
        self.assertTrue(os.access(wrapper, os.X_OK))
        self.assertIn(" claude ", wrapper.read_text())
        self.assertEqual([(args.transcripts, args.backend) for args in self.setups], [(folders, "local")])
        entry, = self.cron.read_text().splitlines()
        self.assertTrue(entry.startswith("17 * * * * ") and entry.endswith(loop_setup.CRON_TAG))
        self.assertIn(" && env " + shlex.quote("COLDFIX_MODEL_CMD=" + shlex.quote(str(wrapper))) + " ", entry)
        self.assertIn("setup --backend local --transcripts " + " ".join(folders), entry)
        self.assertIn("gh api user", self.calls.read_text())
        self.cron.write_text("0 1 * * * keep-me\n" + entry + "\n")
        self.init()
        self.assertEqual(self.cron.read_text().splitlines(), ["0 1 * * * keep-me", entry])

    def test_init_yes_prefers_a_systemd_user_timer(self):
        self.tool("systemctl", 'echo "systemctl $*" >> "$CALLS"')
        self.tool("loginctl", 'echo yes')
        value = json.loads(self.init().stdout)
        self.assertEqual(value["loop"]["schedule"], "systemd user timer coldfix-loop.timer")
        units = self.cwd / "config" / "systemd" / "user"
        service = (units / "coldfix-loop.service").read_text()
        self.assertIn(f'ExecStart="{sys.executable}" ', service)
        self.assertIn(f'Environment="COLDFIX_MODEL_CMD={shlex.quote(str(self.store.resolve() / "model-cmd"))}"', service)
        self.assertIn("OnCalendar=hourly", (units / "coldfix-loop.timer").read_text())
        self.assertIn("systemctl --user enable --now coldfix-loop.timer", self.calls.read_text())
        self.assertFalse(self.cron.exists())
        self.cron.write_text("0 1 * * * keep-me\n17 * * * * old " + loop_setup.CRON_TAG + "\n")
        self.init()
        self.assertEqual(self.cron.read_text(), "0 1 * * * keep-me\n")

    def test_init_yes_uses_cron_when_the_user_manager_would_stop_at_logout(self):
        self.tool("systemctl", 'echo "systemctl $*" >> "$CALLS"')
        self.tool("loginctl", 'echo no')
        self.assertEqual(json.loads(self.init().stdout)["loop"]["schedule"], "cron entry at minute 17 of every hour")
        self.assertNotIn("enable", self.calls.read_text())

    def test_switching_to_cron_retires_the_systemd_timer(self):
        self.tool("systemctl", 'echo "systemctl $*" >> "$CALLS"')
        self.tool("loginctl", 'echo yes')
        self.init()
        units = self.cwd / "config" / "systemd" / "user"
        self.assertTrue((units / "coldfix-loop.timer").exists())
        self.tool("loginctl", 'echo no')
        self.assertEqual(json.loads(self.init().stdout)["loop"]["schedule"], "cron entry at minute 17 of every hour")
        self.assertFalse((units / "coldfix-loop.timer").exists() or (units / "coldfix-loop.service").exists())
        self.assertIn("systemctl --user disable --now coldfix-loop.timer", self.calls.read_text())

    def test_a_line_break_in_a_schedule_value_leaves_the_schedule_unchanged(self):
        self.cron.write_text("0 1 * * * keep-me\n")
        with patch.dict(os.environ, self.environment), \
                self.assertRaisesRegex(loop_setup.c.ColdfixError, "line break"):
            loop_setup.install_schedule(self.cwd, ["run"], dict(PATH="a\nb"))
        self.assertEqual(self.cron.read_text(), "0 1 * * * keep-me\n")

    def test_systemd_values_are_quoted(self):
        self.assertEqual(loop_setup.systemd_quote('a "b" \\ 5%'), '"a \\"b\\" \\\\ 5%%"')

    def test_cron_escapes_percent_and_keeps_an_unreadable_crontab(self):
        entry = loop_setup.install_schedule
        with patch.dict(os.environ, self.environment):
            entry(self.cwd, ["run", "50%"], dict(PATH="p"))
            self.assertIn(" run 50\\% ", self.cron.read_text())
            self.tool("crontab", '[ "$1" = "-l" ] && { echo "permission denied" >&2; exit 1; }; cat > "$CRON"')
            with self.assertRaisesRegex(loop_setup.c.ColdfixError, "crontab -l failed"):
                entry(self.cwd, ["run"], dict(PATH="p"))
        self.assertIn("50", self.cron.read_text())

    def test_wrapper_replaces_a_symlink_instead_of_writing_through_it(self):
        target = self.cwd / "target"
        target.write_text("keep")
        (self.cwd / "model-cmd").symlink_to(target)
        try:
            loop_setup.write_wrapper(self.cwd, "claude")
        except loop_setup.c.ColdfixError:
            pass
        self.assertEqual(target.read_text(), "keep")

    def test_a_failing_first_pass_installs_no_schedule(self):
        loop_setup.setup.side_effect = loop_setup.c.ColdfixError("first pass failed")
        self.assertIn("first pass failed", self.init(code=2).stderr)
        self.assertFalse(self.cron.exists())

    def test_init_yes_names_each_missing_prerequisite_and_installs_nothing(self):
        cases = (("gh", 'exit 1', "gh auth login"), ("claude", None, "install claude or codex"),
                 ("transcripts", None, "no transcripts"))
        for missing, body, message in cases:
            with self.subTest(missing=missing):
                if missing == "transcripts":
                    self.tool("claude", "exit 0")
                    (self.home / ".claude" / "projects" / "demo" / "session.jsonl").unlink()
                    for folder in ((self.home / ".claude" / "projects" / "demo"), self.home / ".claude" / "projects"):
                        folder.rmdir()
                elif body is None:
                    (self.bin / missing).unlink()
                else:
                    self.tool(missing, body)
                self.assertIn(message, self.init(code=2).stderr)
                self.assertFalse((self.store / "model-cmd").exists())
                self.assertFalse(self.cron.exists())
                self.assertEqual(self.setups, [])
                self.tool("gh", 'echo fixture-login')

    def wrapper(self, cli):
        loop_setup.write_wrapper(self.cwd, cli)
        return subprocess.run([str(self.cwd / "model-cmd"), "judge", json.dumps({"type": "object"})], input="prompt",
                              capture_output=True, text=True, env=dict(os.environ, **self.environment))

    def test_wrapper_returns_the_claude_structured_output(self):
        self.tool("claude", 'cat > "$CALLS"; echo \'{"is_error": false, "structured_output": {"score": 0.9}}\'')
        result = self.wrapper("claude")
        self.assertEqual((result.returncode, json.loads(result.stdout)), (0, {"score": 0.9}), result.stderr)
        self.assertEqual(self.calls.read_text(), "prompt")

    def test_wrapper_returns_the_codex_last_message(self):
        self.tool("codex", 'while [ "$1" != "--output-last-message" ]; do shift; done; echo \'{"score": 0.2}\' > "$2"')
        result = self.wrapper("codex")
        self.assertEqual((result.returncode, json.loads(result.stdout)), (0, {"score": 0.2}), result.stderr)

    def test_status_reports_transcripts_and_the_scheduled_run(self):
        self.store.mkdir()
        db = sqlite3.connect(self.store / loop_setup.SCAN_LEDGER)
        db.execute("CREATE TABLE scans (path TEXT PRIMARY KEY, digest TEXT NOT NULL, status TEXT NOT NULL, score REAL)")
        db.executemany("INSERT INTO scans VALUES (?, 'd', ?, ?)", [("a", "screened_out", None), ("b", "scored", 0.1)])
        db.commit()
        db.close()
        self.cron.write_text(f"17 * * * * run --store {shlex.quote(str(self.store.resolve()))} setup {loop_setup.CRON_TAG}\n")
        other = self.cwd / "other"
        with patch.dict(os.environ, self.environment):
            value = json.loads(test_cli.CliTests.run_cli(self, "--store", str(self.store), "status", "--json").stdout)
            text = test_cli.CliTests.run_cli(self, "--store", str(self.store), "status").stdout
            elsewhere = json.loads(test_cli.CliTests.run_cli(self, "--store", str(other), "status", "--json").stdout)
        self.assertIsNone(elsewhere["next_scheduled_run"])
        self.assertEqual((value["transcripts"], value["next_scheduled_run"]), ({"analyzed": 2, "judged": 1}, "cron 17 * * * *"))
        self.assertIn("Transcripts analyzed: 2 (1 judged by the model)", text)
        self.assertIn("Next scheduled run: cron 17 * * * *", text)


if __name__ == "__main__":
    unittest.main()
