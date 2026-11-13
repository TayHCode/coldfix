import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


CLI = Path(__file__).resolve().parents[1] / "coldfix.py"
WORKFLOW = ".github/workflows/coldfix-review.yml"
HOOK = ".githooks/pre-push"
RULES_WORKFLOW = ".github/workflows/coldfix-rules.yml"


class InstallTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.global_config = Path(temporary.name) / "global.gitconfig"
        self.global_config.touch()
        patcher = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(self.global_config), "GIT_CONFIG_NOSYSTEM": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repo = Path(temporary.name) / "target"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)

    def install(self, *args, code=0):
        result = subprocess.run([sys.executable, str(CLI), "install", "--repo", str(self.repo), *args],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, code, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return result

    def hooks_path(self):
        result = subprocess.run(["git", "-C", str(self.repo), "config", "--local", "--get", "core.hooksPath"],
                                text=True, capture_output=True)
        return result.stdout.strip()

    def snapshot(self):
        rows = {}
        for path in sorted(self.repo.rglob("*")):
            if ".git" in path.relative_to(self.repo).parts[:1]:
                continue
            stat = path.stat()
            rows[str(path.relative_to(self.repo))] = (
                path.read_bytes() if path.is_file() else None, stat.st_mode, stat.st_mtime_ns)
        return rows, (self.repo / ".git" / "config").read_bytes()

    def test_dry_run_prints_the_plan_and_writes_nothing(self):
        before = self.snapshot()
        result = self.install("--dry-run")
        self.assertEqual(result.stdout.splitlines(), [
            "would create: " + WORKFLOW,
            "would create: " + HOOK,
            "would create: " + RULES_WORKFLOW,
            "would set: git config core.hooksPath .githooks"])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.hooks_path(), "")

    def test_install_writes_workflow_and_executable_hook(self):
        result = self.install()
        self.assertEqual(result.stdout.splitlines(), [
            "create: " + WORKFLOW, "create: " + HOOK, "create: " + RULES_WORKFLOW, "set: git config core.hooksPath .githooks"])
        self.assertIn('--body "@codex review"', (self.repo / WORKFLOW).read_text())
        self.assertTrue(os.access(self.repo / HOOK, os.X_OK))
        self.assertTrue((self.repo / HOOK).read_text().startswith("#!/usr/bin/env python3\n"))
        self.assertEqual(self.hooks_path(), ".githooks")

    def test_install_writes_rules_workflow_running_the_hook(self):
        self.install()
        workflow = (self.repo / RULES_WORKFLOW).read_text()
        self.assertIn("  pull_request:\n", workflow)
        self.assertIn("  push:\n", workflow)
        self.assertIn("github.event.repository.default_branch", workflow)
        self.assertIn("github.event.pull_request.head.repo.full_name == github.repository", workflow)
        self.assertIn("permissions:\n  contents: read\n", workflow)
        self.assertIn("uses: actions/checkout@v4", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertIn("fetch-depth: 0", workflow)
        self.assertIn('runs-on: ["ubuntu-latest"]', workflow)
        self.assertIn("run: python3 .githooks/pre-push", workflow)

    def test_runs_on_labels_are_respected(self):
        self.install("--runs-on", "self-hosted", "Linux", "rules-runner")
        workflow = (self.repo / RULES_WORKFLOW).read_text()
        self.assertIn('runs-on: ["self-hosted", "Linux", "rules-runner"]', workflow)
        self.assertNotIn("ubuntu-latest", workflow)
        before = self.snapshot()
        self.install("--runs-on", "self-hosted", "Linux", "rules-runner")
        self.assertEqual(self.snapshot(), before)

    def test_dry_run_lists_rules_workflow(self):
        result = self.install("--dry-run")
        self.assertIn("would create: " + RULES_WORKFLOW, result.stdout.splitlines())
        self.assertFalse((self.repo / RULES_WORKFLOW).exists())

    def test_rerun_changes_nothing(self):
        self.install()
        before = self.snapshot()
        result = self.install()
        self.assertEqual(result.stdout.splitlines(), [
            "unchanged: " + WORKFLOW, "unchanged: " + HOOK, "unchanged: " + RULES_WORKFLOW, "unchanged: git config core.hooksPath .githooks"])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.install("--dry-run").stdout, result.stdout)

    def test_missing_execute_bit_is_restored_once(self):
        self.install()
        (self.repo / HOOK).chmod(0o644)
        self.assertEqual(self.install().stdout.splitlines()[1], "chmod: " + HOOK)
        self.assertTrue(os.access(self.repo / HOOK, os.X_OK))
        self.assertEqual(self.install().stdout.splitlines()[1], "unchanged: " + HOOK)

    def test_conflicting_file_aborts_before_any_write(self):
        (self.repo / HOOK).parent.mkdir()
        (self.repo / HOOK).write_text("#!/bin/sh\necho mine\n")
        before = self.snapshot()
        result = self.install(code=2)
        self.assertIn("different content", result.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.repo / WORKFLOW).exists())

    def test_other_hooks_path_aborts_before_any_write(self):
        subprocess.run(["git", "-C", str(self.repo), "config", "core.hooksPath", "elsewhere"], check=True)
        before = self.snapshot()
        result = self.install(code=2)
        self.assertIn("core.hooksPath", result.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_global_hooks_path_aborts_before_any_write(self):
        subprocess.run(["git", "config", "--file", str(self.global_config), "core.hooksPath", "/elsewhere"], check=True)
        before = self.snapshot()
        result = self.install(code=2)
        self.assertIn("core.hooksPath", result.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.hooks_path(), "")

    def test_global_hooks_path_matching_ours_is_unchanged(self):
        subprocess.run(["git", "config", "--file", str(self.global_config), "core.hooksPath", ".githooks"], check=True)
        self.assertIn("unchanged: git config", self.install().stdout.splitlines()[3])

    def test_target_must_be_a_git_repository(self):
        self.repo = self.repo / "plain"
        self.repo.mkdir()
        self.assertIn("git repository", self.install(code=2).stderr)

    def run_hook(self, rules, fence="```"):
        self.install()
        (self.repo / ".coldlocker" / "rules").mkdir(parents=True)
        registry = {}
        for name, (pending, check) in rules.items():
            registry[name] = {"pending": pending}
            (self.repo / ".coldlocker" / "rules" / (name + ".md")).write_text(
                f"# Rule\n\n## Verify\n\nKind: test\n\n{fence}sh\n{check}\n{fence}\n\n## Evidence\n")
        (self.repo / ".coldlocker" / "rules.json").write_text(json.dumps(registry))
        return subprocess.run([sys.executable, str(self.repo / HOOK)], cwd=self.repo, text=True, capture_output=True)

    def test_hook_passes_when_active_checks_pass_and_skips_pending(self):
        result = self.run_hook({"ok": (False, "exit 0"), "held": (True, "exit 1")})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_hook_blocks_on_a_failing_active_check(self):
        result = self.run_hook({"ok": (False, "exit 0"), "bad": (False, "exit 3")})
        self.assertEqual(result.returncode, 1)
        self.assertIn("bad", result.stderr)
        self.assertNotIn("ok", result.stderr.split(":", 1)[1])

    def test_hook_runs_rules_exported_with_longer_fences(self):
        check = "echo '```' >/dev/null; exit 3"
        result = self.run_hook({"bad": (False, check)}, fence="````")
        self.assertEqual(result.returncode, 1)
        self.assertIn("failing rule checks: bad", result.stderr)

    def test_hook_passes_a_longer_fence_rule_that_succeeds(self):
        result = self.run_hook({"ok": (False, "echo '```' >/dev/null")}, fence="````")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_hook_fails_loudly_on_an_unparseable_active_rule(self):
        result = self.run_hook({"broken": (False, "exit 0"), "held": (True, "exit 0")}, fence="~~~")
        self.assertEqual(result.returncode, 1)
        self.assertIn("cannot parse", result.stderr)
        self.assertIn("broken", result.stderr)
        self.assertNotIn("held", result.stderr)

    def test_hook_fails_loudly_on_a_missing_active_rule_file_but_skips_pending(self):
        self.run_hook({"present": (False, "exit 0")})
        (self.repo / ".coldlocker" / "rules.json").write_text(json.dumps(
            {"present": {"pending": False}, "gone": {"pending": False}, "held": {"pending": True}}))
        result = subprocess.run([sys.executable, str(self.repo / HOOK)], cwd=self.repo, text=True, capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("rule file missing for active rule(s): gone", result.stderr)
        self.assertNotIn("held", result.stderr)

    def test_hook_without_rules_passes(self):
        self.install()
        result = subprocess.run([sys.executable, str(self.repo / HOOK)], cwd=self.repo, capture_output=True)
        self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
