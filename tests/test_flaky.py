import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import coldfix as c
import test_cli
import test_draft
import test_lesson_delivery
from test_failure_ingest import fixture


ROOT = Path(__file__).resolve().parents[1]


class FlakyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.cwd = Path(temporary.name)
        self.store = self.cwd / "store"

    def ingest(self, rows, code=0):
        path = self.cwd / "attempts.json"
        path.write_text(json.dumps(rows))
        result = subprocess.run([sys.executable, str(ROOT / "failure_ingest.py"), "--store", str(self.store),
                                 "json", "--input", str(path)], text=True, capture_output=True)
        self.assertEqual(result.returncode, code, result.stderr)
        return result

    def flaky(self):
        result = subprocess.run([sys.executable, str(test_cli.CLI), "--store", str(self.store), "flaky"],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def passed(self, **changes):
        return dict(fixture(), attempt=2, conclusion="success", log_tail="", **changes)

    def test_detects_pass_and_fail_at_the_same_sha_and_job(self):
        self.ingest([fixture(), self.passed()])
        row, = self.flaky()
        self.assertEqual((row["repo"], row["sha"], row["job"], row["check"]),
                         ("example/project", "a" * 40, "unit tests", None))
        self.assertEqual([item["attempt"] for item in row["failed_attempts"]], [1])
        self.assertEqual([item["attempt"] for item in row["passed_attempts"]], [2])
        store = c.load_store(self.store)[1]
        self.assertEqual(len(store["failures"]), 1)
        self.assertEqual(len(store["passes"]), 1)

    def test_pass_at_a_different_sha_is_not_flaky(self):
        self.ingest([fixture(), self.passed(head_sha="b" * 40)])
        self.assertEqual(self.flaky(), [])

    def test_repo_job_and_check_must_match(self):
        for index, change in enumerate((dict(repo="example/other"), dict(job="other tests"), dict(check="other check"))):
            with self.subTest(change=change):
                self.store = self.cwd / str(index)
                self.ingest([dict(fixture(), check="test"), self.passed(**{"check": "test", **change})])
                self.assertEqual(self.flaky(), [])

    def test_flaky_cli_lists_attempts_without_lessons_or_writes(self):
        self.ingest([fixture(), self.passed()])
        before = {path: path.read_bytes() for path in self.store.rglob("*") if path.is_file()}
        self.assertEqual(self.flaky(), [dict(repo="example/project", sha="a" * 40, job="unit tests", check=None,
            failed_attempts=[dict(run_id=412, attempt=1, source_ref="https://github.com/example/project/actions/runs/412#attempt-1")],
            passed_attempts=[dict(run_id=412, attempt=2, source_ref="https://github.com/example/project/actions/runs/412#attempt-2")])])
        self.assertEqual(before, {path: path.read_bytes() for path in self.store.rglob("*") if path.is_file()})

    def test_pass_before_failure_and_repeated_ingest_are_idempotent(self):
        self.ingest(self.passed())
        self.ingest(fixture())
        before = {path: path.read_bytes() for path in self.store.rglob("*.json")}
        self.assertEqual(json.loads(self.ingest([fixture(), self.passed()]).stdout), dict(imported=0, skipped=2))
        self.assertEqual(before, {path: path.read_bytes() for path in self.store.rglob("*.json")})
        self.assertEqual(len(self.flaky()), 1)
        self.assertEqual(self.store.stat().st_mode & 0o777, 0o700)
        for path in before:
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_matching_named_check_is_flaky(self):
        self.ingest([dict(fixture(), check="unit"), self.passed(check="unit")])
        row, = self.flaky()
        self.assertEqual(row["check"], "unit")

    def test_individual_jobs_in_an_aggregate_run_match(self):
        failed_jobs = [dict(id=index, name=name, runner="runner", duration_seconds=1, conclusion="failure")
                       for index, name in enumerate(("lint", "unit tests"), 1)]
        passed_jobs = [dict(failed_jobs[1], id=3, conclusion="success")]
        self.ingest([dict(fixture(), job="lint; unit tests", jobs=failed_jobs), self.passed(jobs=passed_jobs)])
        row, = self.flaky()
        self.assertEqual(row["job"], "unit tests")

    def test_conflicting_conclusions_for_one_attempt_are_rejected(self):
        self.ingest(fixture())
        before = {path: path.read_bytes() for path in self.store.rglob("*.json")}
        self.ingest(dict(fixture(), conclusion="success", log_tail=""), code=2)
        self.assertEqual(before, {path: path.read_bytes() for path in self.store.rglob("*.json")})
        self.assertEqual(self.flaky(), [])

    def test_caller_reported_flake_is_listed_without_a_pass(self):
        self.ingest(dict(fixture("t3-blocker.json"), blocker_category="flake"))
        row, = self.flaky()
        self.assertEqual(row["passed_attempts"], [])
        self.assertEqual(len(row["failed_attempts"]), 1)
        self.assertIsNone(row["sha"])

    def test_empty_flaky_list_does_not_create_a_store(self):
        self.assertEqual(self.flaky(), [])
        self.assertFalse(self.store.exists())

    def test_classified_legacy_metadata_needs_no_ci_identity(self):
        row = dict(command="verify", error=json.dumps(dict(classification=dict(category="flake"),
                   job=[], sha={}, check=[])), repo="example/project", agent="fixture", kind="test",
                   source_ref="legacy:flake", occurred="2026-10-20T12:00:00Z")
        result = subprocess.run([sys.executable, str(test_cli.CLI), "--store", str(self.store), "ingest"],
                                input=json.dumps(row), text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        flaky, = self.flaky()
        self.assertEqual(flaky["job"], "verify")
        self.assertIsNone(flaky["sha"])
        self.assertIsNone(flaky["check"])


class FlakyGateTests(unittest.TestCase):
    ingest = FlakyTests.ingest
    passed = FlakyTests.passed
    event = test_cli.CliTests.event
    git = test_draft.DraftTests.git
    public_inputs = test_cli.CliTests.public_inputs

    def setUp(self):
        test_draft.DraftTests.setUp(self)
        self.store = self.cwd / "flake-store"
        self.reply.write_text(json.dumps(self.response))
        self.inputs = self.cwd / "empty.jsonl"
        self.inputs.write_text("")

    def run_cli(self, *args, **kwargs):
        return test_cli.CliTests.run_cli(self, "--store", str(self.store), *args, **kwargs)

    def failures(self, classified=False):
        rows = [dict(fixture(), head_sha=self.before, run_id=run_id) for run_id in (412, 413)]
        if classified:
            for row in rows[:1]:
                row["retry_evidence"] = dict(attempt=2, head_sha=self.before, conclusion="success",
                    url=f"https://github.com/example/project/actions/runs/{row['run_id']}/attempts/2")
        self.ingest(rows)
        failure = next(row for row in c.load_store(self.store)[1]["failures"].values() if row["source"] == "failure-ingest")
        return c.signature(failure)

    def assert_learn_skips(self, extra=()):
        result = self.run_cli("learn", "--repo", str(self.repo), "--before", self.before,
            "--after", self.after, "--input", str(self.inputs), *extra)
        self.assertEqual(json.loads(result.stdout), dict(ingested=0, drafted=0, proven=0, delivered=0, skipped_flaky=1))
        self.assertFalse(self.calls.exists())
        for collection in ("lessons", "proposals", "receipts"):
            self.assertFalse(list((self.store / collection).glob("*.json")))

    def test_learn_skips_and_counts_a_ledger_detected_flaky_group(self):
        self.failures()
        self.ingest(self.passed(head_sha=self.before))
        self.assert_learn_skips()

    def test_learn_skips_and_counts_a_classified_flaky_group(self):
        self.failures(classified=True)
        self.assert_learn_skips()

    def test_learn_skips_a_caller_reported_flaky_group(self):
        self.ingest([dict(fixture("t3-blocker.json"), thread_id=thread, blocker_category="flake")
                     for thread in ("first", "second")])
        self.assert_learn_skips()

    def test_draft_refuses_a_classified_flaky_group_before_calling_model(self):
        group = self.failures(classified=True)
        result = self.run_cli("draft", "--group", group, "--repo", str(self.repo), "--before", self.before,
                              "--after", self.after, "--output", str(self.output), code=2)
        self.assertIn("flaky", result.stderr)
        self.assertFalse(self.calls.exists())
        self.assertFalse(self.output.exists())

    def test_draft_refuses_a_ledger_detected_flaky_group(self):
        group = self.failures()
        self.ingest(self.passed(head_sha=self.before))
        result = self.run_cli("draft", "--group", group, "--repo", str(self.repo), "--before", self.before,
                              "--after", self.after, "--output", str(self.output), code=2)
        self.assertIn("flaky", result.stderr)
        self.assertFalse(self.calls.exists())

    def author(self, group):
        check = self.cwd / "check.py"
        check.write_text(self.script)
        authored = json.loads(self.run_cli("lesson", "--group", group, "--kind", "test", "--level", "test",
            "--title", "Check value", "--rule", self.response["rule"], "--failure", self.response["failure"],
            "--fix", self.response["fix"], "--check-command", "python3 {check}", "--check-file", str(check),
            "--expected-failure", "value").stdout)
        return json.loads(self.run_cli("replay", "--lesson", authored["id"], "--repo", str(self.repo),
            "--before", self.before, "--after", self.after, "--status", "active").stdout)["id"]

    def assert_export_refuses(self, revision):
        output = self.cwd / "rule.md"
        result = self.run_cli("export", "--lesson", revision, "--output", str(output), code=2)
        self.assertIn("flaky", result.stderr)
        self.assertIn(revision, result.stderr)
        self.assertFalse(output.exists())

    def test_export_refuses_an_already_authored_classified_flaky_lesson(self):
        revision = self.author(self.failures(classified=True))
        self.assert_export_refuses(revision)

    def test_export_refuses_a_lesson_after_a_matching_pass_arrives(self):
        revision = self.author(self.failures())
        self.ingest(self.passed(head_sha=self.before))
        self.assert_export_refuses(revision)

    def test_supplied_fix_cannot_bypass_flaky_filter(self):
        group = self.failures(classified=True)
        check = self.cwd / "check.py"
        check.write_text(self.script)
        self.output.write_text(json.dumps({group: dict(repo=str(self.repo), before=self.before, after=self.after,
            check_file=str(check), expected_failure="value", kind="test", failure=self.response["failure"],
            fix=self.response["fix"], rule=self.response["rule"])}))
        self.assert_learn_skips(("--fixes", str(self.output)))

    def test_later_flaky_failure_in_the_group_blocks_an_existing_lesson(self):
        group = self.failures()
        revision = self.author(group)
        other = "c" * 40
        self.ingest([dict(fixture(), head_sha=other, run_id=414)])
        self.ingest(self.passed(head_sha=other))
        check = self.cwd / "check.py"
        self.output.write_text(json.dumps({group: dict(repo=str(self.repo), before=self.before, after=self.after,
            check_file=str(check), expected_failure="value", kind="test", failure="changed failure",
            fix=self.response["fix"], rule=self.response["rule"])}))
        before = sorted(path.name for path in (self.store / "lessons").glob("*.json"))
        result = self.run_cli("learn", "--repo", str(self.repo), "--before", self.before, "--after", self.after,
            "--input", str(self.inputs), "--fixes", str(self.output))
        self.assertEqual(json.loads(result.stdout)["skipped_flaky"], 1)
        self.assertEqual(sorted(path.name for path in (self.store / "lessons").glob("*.json")), before)
        self.assertEqual(c.latest_lessons(c.load_store(self.store)[1])[c.load_store(self.store)[1]["lessons"][revision]["lesson_id"]]["id"], revision)

    def assert_retired_flaky_lesson_is_withdrawn(self, action):
        args, inputs, _, _, _, _ = test_lesson_delivery.AutoDeliveryTests.prepare(self)
        self.public_inputs("pass")
        options = args[args.index("--agentlocker"):]
        options[options.index("--target") + 1] = str(self.repo)
        revision = self.author(self.failures())
        self.run_cli("ingest-and-propose", *options, *inputs, stdin="")
        lesson = c.load_store(self.store)[1]["lessons"][revision]
        rule = self.repo / ".coldlocker" / "rules" / ("lesson-" + lesson["lesson_id"] + ".md")
        self.assertTrue(rule.exists())
        self.ingest(self.passed(head_sha=self.before))
        self.run_cli("lesson", "--revise", revision, "--status", "retired")
        if action == "learn":
            self.run_cli(action, *options, *inputs, "--fixes", str(self.cwd / "fixes.json"), "--repo", str(self.repo),
                         "--before", self.before, "--after", self.after, "--input", str(self.inputs), code=2)
        else:
            self.run_cli(action, *options, *inputs, stdin="")
        self.assertFalse(rule.exists())
        states = [row["state"] for row in json.loads((self.store / "deliveries.json").read_text()).values()
                  if row["lesson_id"] == lesson["lesson_id"]]
        self.assertEqual(states, ["withdrawn"])

    def test_ingest_and_propose_withdraws_a_retired_flaky_lesson(self):
        self.assert_retired_flaky_lesson_is_withdrawn("ingest-and-propose")

    def test_learn_withdraws_a_retired_flaky_lesson(self):
        self.assert_retired_flaky_lesson_is_withdrawn("learn")
