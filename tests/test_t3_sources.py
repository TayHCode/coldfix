"""Local synthetic T3 projections. No provider, network, or model calls."""

from contextlib import closing, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import coldfix as c
import t3_sources as sources


DATE = "2026-10-20T12:00:00Z"
END = "2026-10-20T12:05:00Z"


FIXTURE_TABLES = dict(sources.TABLES, messages=(
    "orchestration_v2_projection_messages",
    "message_id thread_id run_id role streaming created_at updated_at payload_json"))


class T3SourcesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.db = self.root / "state.sqlite"
        self.store = self.root / ".coldfix"
        with closing(sqlite3.connect(self.db)) as db, db:
            for table, columns in FIXTURE_TABLES.values():
                db.execute("CREATE TABLE " + table + " (" + ",".join(
                    name + (" INTEGER" if name == "streaming" else " TEXT") for name in columns.split()) + ")")
        self.insert("projects", project_id="project", title="private-canary-title", workspace_root="example/project")
        self.insert("threads", thread_id="thread", project_id="project",
                    payload_json=json.dumps(dict(id="thread", projectId="project")))
        self.run_row("failed", "failed")
        self.run_row("error", "completed")
        self.item("diagnostic", "error", "failed", "error", "Expected count 2, received 1")
        self.run_row("blocked", "completed")
        self.message("ending", "blocked", "Status: BLOCKED. Awaiting missing input.")

    def insert(self, name, **row):
        table = FIXTURE_TABLES[name][0]
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO " + table + " (" + ",".join(row) + ") VALUES ("
                       + ",".join("?" for _ in row) + ")", tuple(row.values()))

    def run_row(self, ref, status, start=DATE, end=END, thread="thread"):
        self.insert("runs", run_id=ref, thread_id=thread, status=status, requested_at=start,
                    completed_at=end, provider="fixture-provider",
                    payload_json=json.dumps(dict(id=ref, threadId=thread, status=status)))

    def item(self, ref, typ, status, run, diagnostic, when=END):
        self.insert("items", turn_item_id=ref, thread_id="thread", run_id=run, type=typ, status=status,
                    updated_at=when, payload_json=json.dumps(dict(id=ref, threadId="thread", runId=run,
                    type=typ, status=status, failure=dict(message=diagnostic))))

    def message(self, ref, run, text, role="assistant", streaming=False, created=DATE, updated=END):
        self.insert("messages", message_id=ref, thread_id="thread", run_id=run, role=role,
                    streaming=int(streaming), created_at=created, updated_at=updated,
                    payload_json=json.dumps(dict(id=ref, threadId="thread", runId=run, role=role,
                                                 streaming=streaming, text=text)))

    def cli(self, *args, code=0):
        result = subprocess.run([sys.executable, sources.__file__, "--db", str(self.db),
                                 "--project", "project", "--thread", "thread", "--store", str(self.store), *args],
                                cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, code, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("private-canary", result.stdout + result.stderr)
        self.assertNotIn(str(self.root), result.stdout + result.stderr)
        return result

    def events(self, name="t3-events.jsonl"):
        return [json.loads(line) for line in (self.store / name).read_text().splitlines()]

    def receipt(self, name="t3-events.jsonl"):
        return json.loads((self.store / (name + ".receipt.json")).read_text())

    def update(self, name, field, value, where="1=1", params=()):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE " + FIXTURE_TABLES[name][0] + " SET " + field + "=? WHERE " + where, (value, *params))

    def test_cli_extracts_explicit_events_and_private_receipt_read_only(self):
        self.db.chmod(0o400)
        before = (self.db.read_bytes(), self.db.stat().st_mtime_ns, self.db.stat().st_mode)
        result = self.cli()
        self.assertEqual(json.loads(result.stdout), dict(events=1, truncated=False, runs_seen=3, items_seen=1))
        self.assertEqual(result.stderr, "")
        events = self.events()
        self.assertEqual([row["command"] for row in events], ["T3 error"])
        self.assertEqual(events[0], dict(kind="stuck", command="T3 error", error="Expected count 2, received 1",
                         repo="example/project", agent="fixture-provider", thread="thread", occurred=END,
                         source_ref="t3:project:project:thread:thread:item:diagnostic"))
        self.assertEqual((self.db.read_bytes(), self.db.stat().st_mtime_ns, self.db.stat().st_mode), before)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), [".coldfix", "state.sqlite"])
        receipt = self.receipt()
        self.assertEqual(receipt["counts"], dict(runs=dict(total=3, retained=3), items=dict(total=1, retained=1)))
        self.assertTrue(receipt["snapshot_complete"])
        self.assertFalse(receipt["effectiveness_coverage_complete"])
        self.assertEqual(receipt["event_time_fields"][events[0]["source_ref"]], "turn_items.updated_at")
        self.assertEqual(receipt["batch_sha256"], c.digest((self.store / "t3-events.jsonl").read_text()))
        for path in [self.store, *self.store.iterdir()]:
            self.assertEqual(path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600)

    def test_connection_is_mode_ro_query_only_and_single_transaction(self):
        original = sqlite3.connect
        statements = []
        def connect(*args, **kwargs):
            self.assertTrue(args[0].endswith("?mode=ro"))
            self.assertTrue(kwargs["uri"])
            db = original(*args, **kwargs)
            db.set_trace_callback(statements.append)
            def authorize(action, *_):
                if action == sqlite3.SQLITE_READ:
                    self.assertTrue(db.in_transaction)
                    self.assertEqual(db.execute("PRAGMA query_only").fetchone()[0], 1)
                return sqlite3.SQLITE_OK
            db.set_authorizer(authorize)
            return db
        out = io.StringIO()
        with patch.object(sources.sqlite3, "connect", side_effect=connect), redirect_stdout(out):
            self.assertEqual(sources.main(["--db", str(self.db), "--project", "project", "--thread", "thread",
                                           "--store", str(self.store)]), 0)
        self.assertEqual(statements[0:2], ["PRAGMA query_only=ON", "BEGIN"])
        self.assertEqual(statements[-1], "ROLLBACK")
        self.assertEqual(json.loads(out.getvalue())["events"], 1)

    def test_excludes_users_quotes_instructions_nonterminal_messages_and_stopped_runs(self):
        rejected = [
            ("user", "Status: BLOCKED", "user", False, "completed"),
            ("lazyquote", "> Example\nStatus: BLOCKED", "assistant", False, "completed"),
            ("separated-instruction", "Write this final line:\n\nStatus: BLOCKED", "assistant", False, "completed"),
            ("separated-example", "Example:\n\nStatus: BLOCKED", "assistant", False, "completed"),
            ("instructions", "Use this ending:\nStatus: BLOCKED\n\n", "assistant", False, "completed"),
            ("example", "Example output:\nStatus: BLOCKED", "assistant", False, "completed"),
            ("nestedcode", "````text\n```\nStatus: BLOCKED", "assistant", False, "completed"),
            ("quote", "> Status: BLOCKED", "assistant", False, "completed"),
            ("code", "```text\nStatus: BLOCKED\n```", "assistant", False, "completed"),
            ("indent", "    Status: BLOCKED", "assistant", False, "completed"),
            ("instruction", "Always finish with Status: BLOCKED", "assistant", False, "completed"),
            ("mention", "The example was blocked, and the fix is done.", "assistant", False, "completed"),
            ("nonterminal", "Status: BLOCKED\nWork resumed successfully.", "assistant", False, "completed"),
            ("stream", "Status: BLOCKED", "assistant", True, "completed"),
            ("cancel", "Status: BLOCKED", "assistant", False, "cancelled"),
            ("interrupt", "Status: BLOCKED", "assistant", False, "interrupted"),
            ("idle", "Status: BLOCKED", "assistant", False, "running"),
            ("stoppedbody", "The user cancelled this task.\n\nStatus: BLOCKED", "assistant", False, "completed"),
            ("stoppedtext", "Status: BLOCKED. User cancelled this task.", "assistant", False, "completed"),
        ]
        for ref, content, role, streaming, status in rejected:
            self.run_row(ref, status)
            self.message(ref, ref, content, role, streaming)
        self.run_row("ambiguous", "completed")
        self.message("zz-tied-blocked", "ambiguous", "Status: BLOCKED")
        self.message("aa-tied-done", "ambiguous", "Status: DONE")
        self.message("later-done", "nonterminal", "Status: DONE", created=END)
        self.item("cancel-error", "error", "failed", "cancel", "Example provider error")
        self.item("interrupt-error", "error", "failed", "interrupt", "Example provider error")
        self.item("command-failure", "command_execution", "failed", "error", "Expected failure in a test")
        self.run_row("failed-cancel", "failed")
        self.item("failed-cancel", "error", "failed", "failed-cancel", "Run cancelled by user")
        self.item("cancel-notice", "error", "failed", "error", "Run cancelled by user")
        self.run_row("cancelled-ending", "completed")
        self.message("cancelled-ending", "cancelled-ending", "Status: BLOCKED")
        self.item("cancelled-ending", "error", "failed", "cancelled-ending", "Run cancelled by user")
        self.run_row("stuck", "completed")
        self.message("stuck-ending", "stuck", "**Status: STUCK**")
        self.cli()
        self.assertEqual({row["source_ref"].rsplit(":", 1)[-1] for row in self.events()},
                         {"diagnostic"})
        self.assertEqual({row["kind"] for row in self.events()}, {"stuck"})

    def test_selection_skips_unrelated_projects_and_source_text_never_executes(self):
        self.insert("projects", project_id="other", title="Other", workspace_root="other/project")
        self.insert("threads", thread_id="unrelated", project_id="other", payload_json="malformed-private-canary")
        self.run_row("unrelated-run", "failed", thread="unrelated")
        self.update("runs", "payload_json", "private-canary-invalid", "thread_id=?", ("unrelated",))
        marker = self.root / "must-not-execute"
        self.item("text-as-data", "error", "failed", "error", "$(touch " + str(marker) + ") private-canary")
        self.cli()
        self.assertEqual(len(self.events()), 2)
        self.assertIn("$(touch", next(row["error"] for row in self.events() if row["source_ref"].endswith(":text-as-data")))
        self.assertFalse(marker.exists())
        self.assertEqual(self.receipt()["counts"]["runs"], dict(total=3, retained=3))
        self.cli("--thread", "unrelated", "--output", "cross-project.jsonl", code=2)
        self.assertFalse((self.store / "cross-project.jsonl").exists())

    def test_failed_run_with_error_has_one_source_occurrence_and_stable_repeated_refs(self):
        self.item("same-failure", "error", "failed", "failed", "Specific failure")
        self.cli("--thread", "thread")
        self.cli("--output", "second.jsonl")
        self.assertEqual(self.events(), self.events("second.jsonl"))
        refs = [row["source_ref"] for row in self.events()]
        self.assertEqual(len(refs), len(set(refs)))
        self.assertIn("t3:project:project:thread:thread:item:same-failure", refs)
        self.assertNotIn("t3:project:project:thread:thread:run:failed", refs)
        self.assertEqual(self.receipt()["threads"], ["thread"])
        seed = self.root / "seed"
        for name, count in (("t3-events.jsonl", 2), ("second.jsonl", 0)):
            result = self.coldfix(seed, "ingest", "--input", str(self.store / name))
            self.assertEqual(json.loads(result.stdout)["imported_records"], count)
        _, imported = c.load_store(seed)
        self.assertEqual(len(imported["failures"]), 2)
        self.assertEqual(len({r["source_ref"] for r in imported["failures"].values()}), 2)

    def test_error_items_in_active_runs_are_not_failures(self):
        for state in ("queued", "running"):
            self.run_row(state, state, end=None)
            self.item("item-" + state, "error", "failed", state, "Temporary provider failure")
        self.cli()
        self.assertEqual(len(self.events()), 1)
        self.assertFalse(any("Temporary provider" in row["error"] for row in self.events()))

    def test_truncated_items_cannot_hide_cancellation_behind_generic_failure(self):
        self.run_row("aaa-failed", "failed")
        self.item("zzz-cancel", "error", "failed", "aaa-failed", "User stopped the request")
        self.cli("--limit", "1")
        self.assertTrue(self.receipt()["truncated"]["items"])
        self.assertEqual(self.events(), [])

    def test_truncated_items_cannot_hide_cancellation_behind_blocked_ending(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            for name in ("runs", "messages", "items"):
                db.execute("DELETE FROM " + FIXTURE_TABLES[name][0])
        self.run_row("blocked", "completed")
        self.message("ending", "blocked", "Status: BLOCKED")
        self.item("aaa-tool", "tool", "completed", "blocked", "")
        self.item("zzz-cancel", "error", "failed", "blocked", "User stopped the request")
        self.cli("--limit", "1")
        self.assertEqual(self.receipt()["truncated"], dict(runs=False, items=True))
        self.assertEqual(self.events(), [])

    def test_example_introductions_do_not_create_blocked_events(self):
        for index, intro in enumerate(("For example:", "E.g.:")):
            ref = "example-" + str(index)
            self.run_row(ref, "completed")
            self.message(ref, ref, intro + "\n\nStatus: BLOCKED")
        self.cli()
        self.assertEqual(len(self.events()), 1)

    def test_status_without_diagnostic_does_not_invent_a_recurrence(self):
        self.run_row("unrelated-failure", "failed")
        self.cli()
        self.assertFalse(any(row["command"] == "T3 run" for row in self.events()))

    def test_descriptive_example_introduction_is_not_a_failure(self):
        self.run_row("example-prose", "completed")
        self.message("example-prose", "example-prose", "For example, the final output is:\n\nStatus: BLOCKED")
        self.cli()
        self.assertFalse(any(row["source_ref"].endswith(":example-prose") for row in self.events()))

    def test_real_occurrence_times_since_and_excerpt_limits(self):
        when = "2026-10-20T12:04:00+01:00"
        self.update("items", "updated_at", when)
        self.item("long", "error", "failed", "error", "private-canary-" + "x" * 2000, when="2026-10-21T01:00:00Z")
        self.cli("--since", "2026-10-20T13:00:00+01:00")
        self.assertEqual(len(self.events()), 1)
        long, = [r for r in self.events() if r["source_ref"].endswith(":long")]
        self.assertEqual((long["occurred"], len(long["error"])), ("2026-10-21T01:00:00Z", 1000))
        self.assertEqual(self.receipt()["excerpts"]["truncated"], 1)
        self.cli("--output", "older.jsonl", "--since", "2026-10-20")
        self.assertEqual(next(r for r in self.events("older.jsonl") if r["command"] == "T3 error")["occurred"], when)

    def test_bounds_record_gaps_and_omit_items_with_missing_runs(self):
        self.run_row("a-failed", "failed")
        self.cli("--limit", "1")
        receipt = self.receipt()
        self.assertEqual(receipt["counts"]["runs"], dict(total=4, retained=1))
        self.assertTrue(receipt["truncated"]["runs"])
        self.assertFalse(receipt["snapshot_complete"])
        self.assertFalse(receipt["effectiveness_coverage_complete"])
        self.assertEqual(self.events(), [])

    def test_schema_malformed_json_and_projection_mismatch_fail_before_any_publish(self):
        for name, field, value in [("items", "payload_json", "private-canary-invalid"),
                                   ("threads", "project_id", "private-canary-wrong"),
                                   ("runs", "completed_at", None),
                                   ("runs", "completed_at", "2026-02-30T00:00:00Z"),
                                   ("items", "updated_at", "2026-10-20")]:
            with self.subTest(name=name, field=field):
                backup = self.db.read_bytes()
                self.update(name, field, value)
                self.cli(code=2)
                self.assertFalse(self.store.exists())
                self.db.write_bytes(backup)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("ALTER TABLE orchestration_v2_projection_turn_items RENAME COLUMN type TO unsupported")
        self.cli(code=2)
        self.assertFalse(self.store.exists())

    def test_refuses_unsafe_inputs_selection_overwrites_and_public_permissions(self):
        for args in (("--project", "missing-private-canary"), ("--thread", "missing-private-canary"),
                     ("--output", "../outside.jsonl"), ("--limit", "0"), ("--since", "2026-10-20T00:00:00"),
                     ("--store", str(self.root / "public")), ("--db", str(self.root / "missing.sqlite"))):
            self.cli(*args, code=2)
            self.assertFalse(self.store.exists())
        self.assertFalse((self.root / "missing.sqlite").exists())
        self.cli()
        original = {p.name: p.read_bytes() for p in self.store.iterdir()}
        self.cli(code=2)
        self.assertEqual({p.name: p.read_bytes() for p in self.store.iterdir()}, original)
        (self.store / "symlink.jsonl").symlink_to(self.root / "outside.jsonl")
        self.cli("--output", "symlink.jsonl", code=2)
        self.assertFalse((self.root / "outside.jsonl").exists())
        self.store.chmod(0o755)
        self.cli("--output", "public.jsonl", code=2)
        self.assertFalse((self.store / "public.jsonl").exists())

    def test_interrupted_publication_preserves_receipt_and_refuses_retry_overwrite(self):
        original = c.atomic_create
        def publish(path, content, **kwargs):
            if path.name.endswith(".jsonl"):
                raise OSError("private-canary-write-error")
            return original(path, content, **kwargs)
        stderr = io.StringIO()
        with patch.object(c, "atomic_create", side_effect=publish), redirect_stderr(stderr):
            self.assertEqual(sources.main(["--db", str(self.db), "--project", "project", "--thread", "thread",
                                           "--store", str(self.store)]), 2)
        self.assertNotIn("private-canary", stderr.getvalue())
        self.assertFalse((self.store / "t3-events.jsonl").exists())
        receipt_path = self.store / "t3-events.jsonl.receipt.json"
        before = receipt_path.read_bytes()
        self.assertEqual(json.loads(before)["events"], 1)
        self.cli(code=2)
        self.assertEqual(receipt_path.read_bytes(), before)
        self.cli("--output", "retry.jsonl")
        self.assertEqual(len(self.events("retry.jsonl")), 1)

    def coldfix(self, store, *args, code=0):
        result = subprocess.run([sys.executable, c.__file__, "--store", str(store), *args],
                                cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, code, result.stderr)
        return result

    def test_adapter_events_run_end_to_end_with_authored_lesson_and_outcome(self):
        self.run_row("repeat", "completed")
        self.item("repeat-error", "error", "failed", "repeat", "Expected count 2, received 1")
        self.cli()
        seed = self.root / "lesson-store"
        self.coldfix(seed, "ingest", "--input", str(self.store / "t3-events.jsonl"))
        _, store = c.load_store(seed)
        diagnostic = next(row for row in store["failures"].values() if row["command"] == "T3 error")
        check = self.root / "check.py"
        marker = self.root / "must-not-execute"
        check.write_text("from pathlib import Path\nPath(" + repr(str(marker)) + ").touch()\n")
        lesson = json.loads(self.coldfix(seed, "lesson", "--group", c.signature(diagnostic),
                          "--kind", "test", "--level", "test", "--title", "Check counts",
                          "--rule", "Assert the expected count.", "--check-command", "python3 {check}",
                          "--check-file", str(check), "--expected-failure", "count").stdout)
        manifest = self.store / "private-run.json"
        manifest.write_text(json.dumps(dict(version=1, lesson_store=str(seed),
                             sources=[dict(format="jsonl", input="t3-events.jsonl")])))
        result = self.coldfix(self.store, "run", "--input", str(manifest), code=2)
        summary = json.loads(result.stdout)
        self.assertEqual((summary["events"], summary["lessons"], summary["outcomes"], summary["model_calls"], summary["status"]),
                         (4, 1, 1, 0, "INCOMPLETE"))
        attempt, = (self.store / "runs").glob("run-*")
        _, retained = c.load_store(attempt)
        retained_lesson, = c.latest_lessons(retained).values()
        outcome, = retained["outcomes"].values()
        self.assertEqual(retained_lesson["id"], lesson["id"])
        self.assertEqual((retained_lesson["verification"], outcome["status"]), ("unproven", "INCOMPLETE"))
        self.assertFalse(marker.exists())
        self.assertEqual({r["source_ref"] for r in retained["failures"].values()},
                         {r["source_ref"] for r in self.events()})
        self.assertEqual(json.loads((attempt / "run.json").read_text())["summary"], summary)


if __name__ == "__main__":
    unittest.main()
