"""Status behavior through the real CLI and immutable synthetic stores."""

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import coldfix as c
import status_summary
from test_cli import run_main


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cwd = Path(self.temp.name)
        self.store = self.cwd / "store"

    def cli(self, *args, root=None, stdin=None, code=0):
        result = run_main(("--store", str(root or self.store), *args), self.cwd, stdin=stdin)
        self.assertEqual(result.returncode, code, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("private-canary", result.stdout + result.stderr)
        return result

    def event(self, ref, **changes):
        return dict(dict(command="check", error="private-canary failure", repo="private-canary-repo",
                         agent="private-canary-agent", source_ref=ref, kind="test", occurred="2025-01-01"), **changes)

    def ingest(self, *rows, root=None):
        self.cli("ingest", root=root, stdin="\n".join(json.dumps(row) for row in rows))

    def status(self, root=None):
        return json.loads(self.cli("status", "--json", root=root).stdout)

    def manifest(self):
        return {str(p.relative_to(self.store)): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
                for p in self.store.rglob("*") if p.is_file()}

    def lesson(self):
        _, store = c.load_store(self.store)
        check = self.cwd / "check.py"
        check.write_text("raise SystemExit('private-canary never executed')\n")
        result = self.cli("lesson", "--group", c.signature(next(iter(store["failures"].values()))),
                          "--kind", "test", "--level", "test", "--title", "Verify value",
                          "--rule", "Check the expected value.", "--check-command", "python3 {check}",
                          "--check-file", str(check), "--expected-failure", "value")
        return json.loads(result.stdout)["id"]

    def run_output(self, root):
        source = self.cwd / "source.jsonl"
        source.write_text(json.dumps(self.event("sample:extra", error="different private-canary")))
        manifest = self.cwd / "manifest.json"
        manifest.write_text(json.dumps(dict(version=1, lesson_store=str(self.store),
                                            sources=[dict(format="jsonl", input=str(source))])))
        before = set((root / "runs").glob("run-*"))
        self.cli("run", "--input", str(manifest), root=root, code=2)
        return (set((root / "runs").glob("run-*")) - before).pop()

    def test_missing_and_empty_stores_are_zero_without_creation(self):
        for existing in (False, True):
            if existing:
                self.store.mkdir()
            value = self.status()
            self.assertEqual(value["totals"], dict(events=0, groups=0, lessons=0, outcomes=0))
            self.assertEqual(value["scope"], "empty")
            self.assertIsNone(value["last_completed_run"])
            self.assertIn("Last completed run: unknown", self.cli("status").stdout)
            self.assertEqual(self.store.exists(), existing)
            self.assertEqual(list(self.store.rglob("*")), [])

    def test_ci_failures_count_only_github_ci_events(self):
        self.ingest(self.event("https://github.com/o/r/actions/runs/2/job/3", kind="ci", command="ci"),
                    self.event("https://github.com/o/r/issues/5", command="local"),
                    self.event("https://github.com/o/r/pull/1#discussion_r2", kind="review", command="review"))
        self.assertEqual(self.status()["ci_failures"], 1)
        self.assertIn("CI failures collected: 1", self.cli("status").stdout)

    def test_sources_overlap_stale_groups_latest_revisions_and_outcomes_are_read_only(self):
        self.ingest(self.event("https://github.com/example/repo/pull/1#discussion_r2"),
                    self.event("t3:project:p:thread:t:item:i"), self.event("sample:unknown"))
        original = self.lesson()
        self.cli("measure", code=2)
        self.cli("lesson", "--revise", original, "--status", "retired")
        (self.store / "groups.json").write_text("private-canary corrupt stale snapshot")
        before = self.manifest()
        value = self.status()
        self.assertEqual(value["totals"], dict(events=3, groups=1, lessons=1, outcomes=1))
        for source in ("github", "t3", "other_unknown"):
            self.assertEqual(value["by_source"][source], dict(events=1, groups=1, lessons=1, outcomes=1))
        latest, = value["latest_lessons"]
        self.assertEqual((latest["lesson_id"], latest["status"], latest["verification"]),
                         (original, "retired", "unproven"))
        self.assertNotEqual(latest["revision"], original)
        top, = value["top_recurring_groups"]
        self.assertEqual(top["events"], 3)
        self.assertRegex(top["signature"], r"^[0-9a-f]{64}$")
        terminal = self.cli("status").stdout
        self.assertIn("events: 3  groups: 1  lessons: 1  outcomes: 1", terminal)
        self.assertIn("retired  unproven  test", terminal)
        self.assertEqual(self.manifest(), before)
        self.assertIsNone(value["last_completed_run"])

    def test_source_refs_not_labels_filenames_or_log_text_decide_attribution(self):
        refs = ["https://github.com/o/r/commit/" + "a" * 40,
                "https://github.com/o/r/actions/runs/2/job/3",
                "https://api.github.com/repos/o/r/check-runs/4", "https://github.com/o/r/issues/5",
                "t3:project:p%3Ax:thread:t%3Ay:item:i%3Az",
                "https://github.com.evil.invalid/o/r/pull/1", "https://evil.invalid/github.com/o/r/pull/1",
                "github:uncertain", "t3:project:p:thread:t", "https://github.com/o/r/unknown/1"]
        source = self.cwd / "github-t3-events.jsonl"
        source.write_text("\n".join(json.dumps(self.event(ref, command="github t3")) for ref in refs))
        self.cli("ingest", "--input", str(source))
        self.cli("capture", "--command", "github t3", "--repo", "github", "--agent", "t3", stdin="private-canary")
        value = self.status()
        self.assertEqual({k: v["events"] for k, v in value["by_source"].items()},
                         dict(github=4, t3=1, other_unknown=6))

    def test_top_normalized_groups_are_ranked_limited_and_singletons_excluded(self):
        for count in range(1, 8):
            self.ingest(*(self.event("sample:" + str(i), command="check " + str(count),
                                    error=f"private-canary /tmp/file-{i}.py:{i+1}: failure") for i in range(count)))
        value = self.status()
        self.assertEqual(value["totals"]["groups"], 7)
        self.assertEqual([r["events"] for r in value["top_recurring_groups"]], [7, 6, 5, 4, 3])

    def test_completed_runs_select_one_latest_report_not_mtime_or_nested_copies(self):
        self.ingest(self.event("https://github.com/o/r/pull/1"), self.event("t3:project:p:thread:t:item:i"))
        self.lesson()
        output = self.cwd / "output"
        first = self.run_output(output)
        second = self.run_output(output)
        reports = [(json.loads(next((p / "measurements").glob("*.json")).read_text())["created"], p)
                   for p in (first, second)]
        latest_time, latest = max(reports, key=lambda row: (c.time_key(row[0]), row[1].name))
        os.utime(first / "run.json", (2000000000, 2000000000))
        interrupted = output / "runs" / "run-interrupted"
        interrupted.mkdir()
        (interrupted / "failures").mkdir()
        (interrupted / "failures" / "bad.json").write_text("private-canary")
        nested = output / "archive" / "runs" / "run-copy"
        nested.mkdir(parents=True)
        (nested / "run.json").write_text("private-canary")
        value = self.status(output)
        self.assertEqual(value["totals"], dict(events=3, groups=2, lessons=1, outcomes=1))
        self.assertEqual(value["scope"], "latest_completed_run")
        self.assertEqual(value["last_completed_run"], dict(time=latest_time, basis="measurement_report_created", status="INCOMPLETE"))
        direct = self.status(latest)
        self.assertEqual(direct["scope"], "direct_store")
        self.assertEqual(direct["totals"], value["totals"])
        self.cli("capture", "--command", "check", "--repo", "example", "--agent", "worker", root=output, stdin="private-canary")
        direct = self.status(output)
        self.assertEqual(direct["totals"]["events"], 1)
        self.assertIsNone(direct["last_completed_run"])

    def test_corrupt_store_and_completion_markers_fail_without_echo_or_writes(self):
        self.ingest(self.event("sample:one"), self.event("sample:two"))
        self.lesson()
        output = self.cwd / "output"
        attempt = self.run_output(output)
        marker = attempt / "run.json"
        original = marker.read_text()
        for bad in ("private-canary", original.replace('"events": 3', '"events": 999')):
            marker.write_text(bad)
            result = self.cli("status", "--json", root=output, code=2)
            self.assertEqual(json.loads(result.stdout), {"schema_version": 1, "exit_code": 2})
            self.assertEqual(result.stderr, "coldfix: cannot summarize invalid status store; left unchanged\n")
            self.assertEqual(marker.read_text(), bad)
        path = next((self.store / "failures").glob("*.json"))
        path.write_text('{"private-canary": 1}')
        before = self.manifest()
        self.cli("status", code=2)
        self.assertEqual(self.manifest(), before)

    def test_unreadable_transcript_ledger_is_a_fixed_coldfix_error(self):
        self.ingest(self.event("sample:one"))
        (self.store / "transcript-scan.sqlite").write_bytes(b"")
        unreadable = sqlite3.OperationalError("unable to open private-canary")
        with patch.object(status_summary.sqlite3, "connect", side_effect=unreadable):
            with self.assertRaisesRegex(c.ColdfixError, "^cannot read the transcript ledger; left unchanged$"):
                status_summary.status(self.store)

    def test_symlinks_are_rejected_and_no_lesson_lock_is_created(self):
        self.ingest(self.event("sample:one"))
        (self.cwd / "link").symlink_to(self.store, target_is_directory=True)
        self.cli("status", root=self.cwd / "link", code=2)
        value = self.status()
        self.assertEqual(value["totals"]["events"], 1)
        self.assertFalse((self.store / ".lessons.lock").exists())
