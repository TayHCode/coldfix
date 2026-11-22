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
        os.symlink(shutil.which("env"), self.bin / "env")
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

    def test_second_checkout_cannot_take_over_cron(self):
        self.init()
        first_entry = self.cron.read_bytes()
        self.repo = self.cwd / "second checkout"
        self.repo.mkdir()
        self.store = self.repo / ".coldfix"
        result = self.init(code=2)
        self.assertIn("schedule", result.stderr)
        self.assertEqual(self.cron.read_bytes(), first_entry)

    def legacy_cron(self, store):
        command = [sys.executable, str(test_cli.CLI), "--store", str(store), "setup", "--backend",
                   "local", "--transcripts", str(self.home / ".claude/projects")]
        environment = dict(COLDFIX_MODEL_CMD=shlex.quote(str(store / "model-cmd")), PATH=str(self.bin))
        environment.update({key: self.environment.get(key, os.environ.get(key)) for key in
                            ("GH_CONFIG_DIR", "XDG_CONFIG_HOME") if self.environment.get(key, os.environ.get(key))})
        assignments = " ".join(shlex.quote(key + "=" + value) for key, value in environment.items())
        entry = (f"17 * * * * cd {shlex.quote(str(store.parent))} && env {assignments} {shlex.join(command)}"
                 f" >> {shlex.quote(str(store / 'loop.log'))} 2>&1")
        return entry.replace("%", "\\%") + " # coldfix-loop"

    def test_foreign_legacy_cron_survives_init(self):
        foreign = self.legacy_cron(self.cwd / "other" / ".coldfix") + "\n"
        self.cron.write_text(foreign)
        self.assertIn("schedule", self.init(code=2).stderr)
        self.assertEqual(self.cron.read_text(), foreign)

    def test_migration_preserves_foreign_legacy_and_current_entries(self):
        other = self.repo / "other store"
        with patch.dict(os.environ, self.environment):
            loop_setup.install_schedule(other, ["run"], {})
            foreign_current = self.cron.read_text()
            loop_setup.uninstall_schedule(other)
        foreign = foreign_current + self.legacy_cron(other) + "\n"
        self.cron.write_text(foreign + self.legacy_cron(self.store) + "\n")
        self.tool("systemctl", "exit 0")
        self.tool("loginctl", "echo yes")
        self.init()
        self.assertEqual(self.cron.read_text(), foreign)

    def test_second_store_cannot_replace_or_retire_systemd(self):
        self.tool("systemctl", 'echo "systemctl $*" >> "$CALLS"')
        self.tool("loginctl", "echo yes")
        self.init()
        units = self.cwd / "config/systemd/user"
        service = (units / "coldfix-loop.service").read_bytes()
        self.store = self.repo / "other"
        for linger in ("yes", "no"):
            self.tool("loginctl", "echo " + linger)
            self.assertIn("schedule", self.init(code=2).stderr)
            self.assertEqual((units / "coldfix-loop.service").read_bytes(), service)
            self.assertTrue((units / "coldfix-loop.timer").exists())

    def test_generated_cron_bounds_output_and_keeps_latest_failure(self):
        self.store.mkdir()
        script = self.cwd / "output.py"
        status = self.cwd / "exit-code"
        script.write_text("import sys\nfrom pathlib import Path\ncode = int(Path(sys.argv[1]).read_text())\n"
                          "print('x' * 400000)\nprint('diagnostic', code, file=sys.stderr)\nsys.exit(code)\n")
        with patch.dict(os.environ, self.environment):
            loop_setup.install_schedule(self.store, [sys.executable, str(script), str(status)], {})
            command = self.cron.read_text().split(maxsplit=5)[5]
            for code in (3, 7, 0, 0, 0, 0):
                status.write_text(str(code))
                result = subprocess.run(["/bin/sh", "-c", command], capture_output=True)
                self.assertEqual(result.returncode, code, result.stderr)
        logs = list(self.store.glob("loop.log*"))
        self.assertLessEqual(sum(path.stat().st_size for path in logs), 3 * 65536)
        self.assertEqual((self.store / "loop.log.failure").read_bytes()[-2:], b"7\n")
        self.assertIn(b"diagnostic 7", (self.store / "loop.log.failure").read_bytes())
        self.assertTrue((self.store / "loop.log.1").is_file())

    def test_cron_startup_failure_is_recorded_under_the_log_cap(self):
        self.store.mkdir()
        missing = self.cwd / "missing interpreter"
        log = self.store / "loop.log"
        log.write_bytes(b"x" * loop_setup.LOG_CAP)
        with patch.dict(os.environ, self.environment), patch.object(loop_setup.sys, "executable", str(missing)):
            loop_setup.install_schedule(self.store, ["/bin/true"], {})
            entry = self.cron.read_text().split(maxsplit=5)[5]
            for _ in range(3):
                result = subprocess.run(["/bin/sh", "-c", entry], capture_output=True)
                self.assertEqual(result.returncode, 127)
        failure = self.store / "loop.log.failure"
        self.assertTrue(failure.is_file(), "cron startup failure must survive in the failure log")
        self.assertIn(b"failed to start: 127", failure.read_bytes())
        self.assertIn(b"x", log.read_bytes())
        self.assertEqual(log.read_bytes().count(b"failed to start: 127"), 3)
        self.assertLessEqual(log.stat().st_size, loop_setup.LOG_CAP)
        self.assertLessEqual(failure.stat().st_size, loop_setup.LOG_CAP)

    def test_refused_uninstall_leaves_the_schedule_wrapper_and_repo_files_untouched(self):
        self.init()
        environment = dict(self.environment, PATH=str(self.bin) + os.pathsep + os.environ["PATH"])
        with patch.dict(os.environ, environment):
            test_cli.CliTests.run_cli(self, "--store", str(self.store), "install", "--repo", str(self.repo))
            cron = self.cron.read_text()
            def snapshot():
                return {p: (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns)
                        for root in (self.repo, self.home) for p in root.rglob("*") if p.is_file()}
            for edited in (cron.replace(" && env ", " && env EDITED=yes "), cron.replace("17 *", "18 *")):
                self.cron.write_text(edited)
                before = snapshot()
                result = test_cli.CliTests.run_cli(self, "--store", str(self.store), "uninstall", "--repo", str(self.repo), code=2)
                self.assertIn("cron", result.stderr)
                self.assertEqual(snapshot(), before)
                self.assertEqual(self.cron.read_text(), edited)
            self.cron.write_text(cron)
            (self.repo / ".git/config.lock").touch()
            before = snapshot()
            result = test_cli.CliTests.run_cli(self, "--store", str(self.store), "uninstall", "--repo", str(self.repo), code=2)
            self.assertIn("locked", result.stderr)
            self.assertEqual(snapshot(), before)
            self.assertEqual(self.cron.read_text(), cron)

    def test_uninstall_removes_owned_job_wrapper_and_hook(self):
        self.tool("gh", '[ "$1 $2" = "api user" ] && { echo fixture-login; exit; }; echo "[]"')
        self.tool("loginctl", "echo no")
        environment = dict(self.environment, PATH=str(self.bin) + os.pathsep + os.environ["PATH"])
        def cli(store, *args, code=0):
            result = subprocess.run([sys.executable, str(test_cli.CLI), "--store", str(store), *args],
                                    cwd=self.repo, env=environment, text=True, capture_output=True)
            self.assertEqual(result.returncode, code, result.stderr)
            return result
        cli(self.store, "init", "--yes")
        self.cron.write_text("0 1 * * * keep-me\n" + self.cron.read_text())
        first_entry = self.cron.read_bytes()
        other = self.cwd / "other checkout"
        subprocess.run(["git", "init", "-q", str(other)], check=True)
        other_store = other / ".coldfix"
        self.assertIn("schedule", cli(other_store, "init", "--yes", "--repo", str(other), code=2).stderr)
        self.assertEqual(self.cron.read_bytes(), first_entry)
        other_hook = self.repo / ".githooks/post-commit"
        evidence = self.store / "transcript-scan.sqlite"
        original = evidence.read_bytes()
        cli(self.store, "install")
        other_hook.write_text("keep hook")
        cli(other_store, "uninstall")
        self.assertEqual(self.cron.read_bytes(), first_entry)
        self.assertTrue((self.repo / ".githooks/pre-push").is_file())
        cli(self.store, "uninstall")
        cli(self.store, "uninstall")
        self.assertEqual(self.cron.read_text(), "0 1 * * * keep-me\n")
        self.assertFalse((self.store / "model-cmd").exists())
        self.assertFalse((self.store / loop_setup.CRON_FAILURE).exists())
        self.assertFalse((self.repo / ".githooks/pre-push").exists())
        self.assertEqual(other_hook.read_text(), "keep hook")
        self.assertEqual(evidence.read_bytes(), original)
        result = subprocess.run(["git", "-C", str(self.repo), "config", "core.hooksPath"],
                                capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), ".githooks")

    def test_uninstall_keeps_preexisting_hook_and_edited_workflow(self):
        from repo_install import HOOK
        hook = self.repo / ".githooks/pre-push"
        hook.parent.mkdir()
        hook.write_text(HOOK)
        hook.chmod(0o755)
        workflow = self.repo / ".github/workflows/coldfix-review.yml"
        with patch.dict(os.environ, {**self.environment, "PATH": str(self.bin) + os.pathsep + os.environ["PATH"]}):
            test_cli.CliTests.run_cli(self, "--store", str(self.store), "install", "--repo", str(self.repo))
            workflow.write_bytes(b"edited\xff")
            test_cli.CliTests.run_cli(self, "--store", str(self.store), "uninstall", "--repo", str(self.repo))
        self.assertEqual(hook.read_text(), HOOK)
        self.assertEqual(workflow.read_bytes(), b"edited\xff")
        self.assertFalse((self.repo / ".github/workflows/coldfix-rules.yml").exists())

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
        self.cron.write_text("0 1 * * * keep-me\n" + self.legacy_cron(self.store) + "\n")
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

    def test_failed_timer_disable_does_not_add_a_cron_job(self):
        self.tool("loginctl", "echo yes")
        self.tool("systemctl", "exit 0")
        self.init()
        self.tool("loginctl", "echo no")
        self.tool("systemctl", "exit 1")
        self.assertIn("disable", self.init(code=2).stderr)
        self.assertFalse(self.cron.exists())
        self.tool("systemctl", "exit 0")
        self.assertIn("cron entry", self.init().stdout)

    def test_retry_after_failed_systemd_reload_accepts_the_written_service(self):
        self.tool("loginctl", "echo yes")
        self.tool("systemctl", "exit 0")
        self.init()
        self.environment["GH_CONFIG_DIR"] = str(self.cwd / "gh")
        self.tool("systemctl", '[ "$2" != "daemon-reload" ]')
        self.assertIn("daemon-reload failed", self.init(code=2).stderr)
        self.tool("systemctl", "exit 0")
        self.assertIn("systemd user timer", self.init().stdout)

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

    def test_reinit_updates_a_wrapper_from_a_previous_python_and_checkout(self):
        self.store.mkdir()
        wrapper = self.store / "model-cmd"
        old = self.cwd / "old checkout's copy"
        wrapper.write_text(f'#!/bin/sh\nexec {shlex.quote(str(old / "bin/python3"))} '
                           f'{shlex.quote(str(old / "loop_setup.py"))} codex "$@"\n')
        self.init()
        self.assertIn(shlex.quote(sys.executable), wrapper.read_text())
        self.assertNotIn(str(old), wrapper.read_text())

    def test_uninstall_recognizes_a_relocated_wrapper_and_preserves_user_edits(self):
        self.store.mkdir()
        wrapper = self.store / "model-cmd"
        old = '#!/bin/sh\nexec /old/python3 /old/loop_setup.py claude "$@"\n'
        with patch.dict(os.environ, self.environment):
            for content in (old + "echo custom\n", old.replace('"$@"', '"custom"')):
                wrapper.write_text(content)
                loop_setup.uninstall_schedule(self.store)
                self.assertEqual(wrapper.read_text(), content)
            wrapper.write_text(old)
            loop_setup.uninstall_schedule(self.store)
            self.assertFalse(wrapper.exists())

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
