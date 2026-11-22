import json
from pathlib import Path
import tempfile
import unittest

from test_cli import run_main


def receipt(**changes):
    return dict(dict(schema="coldbox.receipt/v1", id="cbx-1700000000-a1b2c3", mode="runner",
                     repo="example/app", sha="a" * 40, verdict="fail", started_at="2026-11-01T12:00:00Z",
                     checks=[dict(check="unit", command="python3 -m unittest", status="fail", exit=1)],
                     github=dict(run_id="41", run_attempt="1", workflow="verify", job="tests")), **changes)


class ColdboxIngestTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        self.store = self.work / "store"
        self.input = self.work / "receipt.json"

    def cli(self, *args, code=0, stdin=None):
        result = run_main(("--store", str(self.store), *args), self.work, stdin=stdin)
        self.assertEqual(result.returncode, code, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return result

    def ingest(self, row, code=0):
        self.input.write_text(json.dumps(row), encoding="utf-8")
        return self.cli("ingest", "--format", "coldbox", "--input", str(self.input), code=code)

    def records(self):
        return [json.loads(path.read_text()) for path in sorted((self.store / "failures").glob("*.json"))]

    def snapshot(self):
        return {str(path.relative_to(self.store)): path.read_bytes()
                for path in self.store.rglob("*") if path.is_file() and path.name != ".lessons.lock"}

    def test_runner_failure_keeps_only_failed_checks_and_metadata(self):
        row = receipt()
        row["checks"] += [dict(check="lint", command="lint", status="pass", exit=0),
                          dict(check="build", command="build", status="skipped", exit=None),
                          dict(check="verify", command="verify", status="not_run", exit=None)]
        result = json.loads(self.ingest(row).stdout)
        self.assertEqual((result["imported_records"], result["skipped_duplicates"]), (1, 0))
        record, = self.records()
        self.assertEqual((record["kind"], record["repo"], record["command"], record["agent"], record["occurred"]),
                         ("ci", "example/app", "python3 -m unittest", "unknown", "2026-11-01T12:00:00Z"))
        metadata = json.loads(record["error"])
        self.assertEqual({key: metadata[key] for key in ("check", "exit", "sha", "id", "mode", "github", "log_tail")},
                         dict(check="unit", exit=1, sha="a" * 40, id="cbx-1700000000-a1b2c3", mode="runner",
                              github=dict(run_id="41", run_attempt="1", workflow="verify", job="tests"), log_tail=""))

    def test_failed_setup_imports_setup_and_not_the_checks_that_did_not_run(self):
        row = receipt(cause="setup", checks=[dict(check="setup", command="make deps", status="fail", exit=2),
                                             dict(check="unit", command="make test", status="not_run", exit=None)])
        self.assertEqual(json.loads(self.ingest(row).stdout)["imported_records"], 1)
        record, = self.records()
        self.assertEqual((record["command"], json.loads(record["error"])["check"]), ("make deps", "setup"))
        row["checks"][1]["exit"] = 0
        self.assertIn("invalid coldbox receipt; nothing imported", self.ingest(row, code=2).stderr)

    def test_failed_junit_identities_are_replay_diagnostics(self):
        row = receipt(tests=dict(results=[
            dict(suite="unit", classname="pkg.TestCount", name="test_count", status="failed"),
            dict(suite="unit", classname="pkg.TestCount", name="test_error", status="error"),
            dict(suite="unit", classname="pkg.TestCount", name="test_ok", status="passed"),
        ]))
        self.ingest(row)
        record, = self.records()
        metadata = json.loads(record["error"])
        self.assertIn("failed_tests", metadata)
        self.assertEqual(metadata["failed_tests"], [
            dict(suite="unit", classname="pkg.TestCount", name="test_count", status="failed"),
            dict(suite="unit", classname="pkg.TestCount", name="test_error", status="error"),
        ])
        self.assertIn("FAIL: test_count (pkg.TestCount)", record["error"])
        self.assertIn("ERROR: test_error (pkg.TestCount)", record["error"])
        self.assertNotIn("test_ok", record["error"])

    def test_job_failure_without_failed_checks_uses_workflow_and_job(self):
        self.assertEqual(json.loads(self.ingest(receipt(checks=[], job_result="Failed", exit=1)).stdout)["imported_records"], 1)
        record, = self.records()
        self.assertEqual((record["command"], record["repo"], record["kind"]), ("verify/tests", "example/app", "ci"))
        self.assertEqual(json.loads(record["error"])["check"], "coldbox job")
        self.assertEqual(json.loads(record["error"])["exit"], 1)

    def test_job_failure_falls_back_without_workflow_and_job(self):
        self.ingest(receipt(checks=[dict(check="unit", command="unit", status="pass", exit=0)], github=None))
        record, = self.records()
        self.assertEqual(record["command"], "coldbox job")
        self.assertIsNone(json.loads(record["error"])["exit"])

    def test_nonfailure_verdicts_import_nothing(self):
        for verdict in ("pass", "incomplete", "idle", "refused", "canceled"):
            with self.subTest(verdict=verdict):
                result = json.loads(self.ingest(receipt(verdict=verdict)).stdout)
                self.assertEqual((result["imported_records"], result["skipped_duplicates"]), (0, 0))
                self.assertEqual(self.records(), [])

    def test_null_sha_nonfailure_receipts_import_nothing(self):
        for verdict in ("incomplete", "idle", "refused"):
            with self.subTest(verdict=verdict):
                result = self.ingest(receipt(sha=None, verdict=verdict, checks=[]))
                self.assertEqual(result.stderr, "")
                self.assertEqual(json.loads(result.stdout)["imported_records"], 0)
                self.assertEqual(self.records(), [])

    def test_host_tagged_receipt_ids_import(self):
        for index, host in enumerate(("edge-a1", "edge-b2", "edge-c3", "h")):
            identifier = f"cbx-{1794474472 + index}-c9b0b6-{host}"
            with self.subTest(host=host):
                (self.work / (identifier + ".log")).write_text("AssertionError: count mismatch\n")
                self.assertEqual(json.loads(self.ingest(receipt(id=identifier, host=host)).stdout)["imported_records"], 1)
                tails = {json.loads(record["error"])["id"]: json.loads(record["error"])["log_tail"] for record in self.records()}
                self.assertEqual(tails[identifier], "AssertionError: count mismatch\n")

    def test_invalid_sha_receipts_are_rejected(self):
        for sha, verdict in ((None, "fail"), (None, "pass"), ("bad", "incomplete"), (None, "canceled")):
            with self.subTest(sha=sha, verdict=verdict):
                result = self.ingest(receipt(sha=sha, verdict=verdict), code=2)
                self.assertIn("invalid coldbox receipt; nothing imported", result.stderr)
                self.assertEqual(self.records(), [])

    def test_check_and_command_deduplicate_together_and_reimport_skips(self):
        row = receipt()
        row["checks"] *= 2
        row["checks"].append(dict(check="unit", command="python3 check.py", status="fail", exit=2))
        first = json.loads(self.ingest(row).stdout)
        self.assertEqual((first["imported_records"], first["skipped_duplicates"]), (2, 0))
        self.assertEqual({record["command"] for record in self.records()}, {"python3 -m unittest", "python3 check.py"})
        self.assertEqual(len({record["source_ref"] for record in self.records()}), 2)
        before = self.snapshot()
        row["checks"].reverse()
        second = json.loads(self.ingest(row).stdout)
        self.assertEqual((second["imported_records"], second["skipped_duplicates"]), (0, 2))
        self.assertEqual(self.snapshot(), before)

    def test_different_runs_share_one_recurring_normalized_group(self):
        for index, sha in enumerate(("a" * 40, "b" * 40)):
            row = receipt(id=f"cbx-{1700000000 + index}-abc{index}", sha=sha,
                          started_at=f"2026-11-0{index + 1}T12:00:00Z", seconds=10 + index,
                          vm=dict(name=f"cbx-{1700000000 + index}-abc{index}"),
                          github=dict(run_id=str(41 + index), run_attempt=str(1 + index), workflow="verify", job="tests"))
            (self.work / (row["id"] + ".log")).write_text(f"run {41 + index} at {sha} took {10 + index} seconds\n")
            self.assertEqual(json.loads(self.ingest(row).stdout)["imported_records"], 1)
        groups = json.loads(self.cli("propose", "--grouping", "normalized").stdout)["groups"]
        group, = groups
        self.assertEqual((group["count"], group["recurring"]), (2, True))
        self.assertEqual(set(group["evidence"]), {record["id"] for record in self.records()})

    def test_mixed_hex_commit_shas_in_logs_share_one_group(self):
        for index, sha in enumerate(("1a2b3c4d5e6f" * 3 + "7a8b", "9f8e7d6c5b4a" * 3 + "3f2e")):
            row = receipt(id=f"cbx-{1700000000 + index}-abc{index}")
            (self.work / (row["id"] + ".log")).write_text(f"checkout {sha} failed at 2026-11-0{index + 1}T12:00:00Z\n")
            self.assertEqual(json.loads(self.ingest(row).stdout)["imported_records"], 1)
        group, = json.loads(self.cli("propose", "--grouping", "normalized").stdout)["groups"]
        self.assertEqual((group["count"], group["recurring"]), (2, True))

    def test_distinct_checks_and_commands_stay_in_separate_groups(self):
        row = receipt()
        row["checks"] += [dict(check="other", command="python3 -m unittest", status="fail", exit=1),
                          dict(check="unit", command="python3 check.py", status="fail", exit=1)]
        self.ingest(row)
        groups = json.loads(self.cli("propose", "--grouping", "normalized").stdout)["groups"]
        self.assertEqual([(group["count"], group["recurring"]) for group in groups], [(1, False)] * 3)

    def test_repository_and_command_paths_stay_in_normalized_identity(self):
        rows = [receipt(id="cbx-1700000000-abc0", repo="sample-org/one",
                        checks=[dict(check="unit", command="pytest tests/unit", status="fail", exit=1)]),
                receipt(id="cbx-1700000001-abc1", repo="other/two",
                        checks=[dict(check="unit", command="pytest tests/integration", status="fail", exit=1)]),
                receipt(id="cbx-1700000002-abc2", repo="sample-org/one",
                        checks=[dict(check="unit", command="pytest tests/integration", status="fail", exit=1)])]
        for row in rows:
            self.ingest(row)
        groups = json.loads(self.cli("propose", "--grouping", "normalized").stdout)["groups"]
        self.assertEqual([(group["count"], group["recurring"]) for group in groups], [(1, False)] * 3)

    def test_different_diagnostics_for_one_check_stay_in_separate_groups(self):
        for index, diagnostic in enumerate(("AssertionError: count mismatch", "ModuleNotFoundError: No module named 'yaml'")):
            row = receipt(id=f"cbx-{1700000000 + index}-abc{index}")
            (self.work / (row["id"] + ".log")).write_text(diagnostic + "\n")
            self.ingest(row)
        groups = json.loads(self.cli("propose", "--grouping", "normalized").stdout)["groups"]
        self.assertEqual([(group["count"], group["recurring"]) for group in groups], [(1, False)] * 2)

    def test_reimport_after_log_arrives_adds_the_diagnostic(self):
        row = receipt()
        first = json.loads(self.ingest(row).stdout)
        (self.work / (row["id"] + ".log")).write_text("AssertionError: count mismatch\n")
        second = json.loads(self.ingest(row).stdout)
        record, = self.records()
        self.assertEqual(second["failure_ids"], first["failure_ids"])
        self.assertIn("AssertionError: count mismatch", json.loads(record["error"])["log_tail"])
        self.assertEqual(json.loads(self.ingest(row).stdout)["imported_records"], 0)

    def test_late_log_leaves_evidence_a_proposal_references_unchanged(self):
        rows = [receipt(), receipt(id="cbx-1700000001-d4e5f6")]
        for row in rows:
            self.ingest(row)
        group, = (row["group_id"] for row in json.loads(self.cli("propose", "--grouping", "normalized").stdout)["groups"])
        self.cli("propose", "--group", group, "--grouping", "normalized", "--kind", "test", "--title", "Count check",
                 "--rule", "Keep the count stable", "--check-command", "python3 {check}")
        before = self.snapshot()
        (self.work / (rows[0]["id"] + ".log")).write_text("AssertionError: count mismatch\n")
        self.assertEqual(json.loads(self.ingest(rows[0]).stdout)["imported_records"], 0)
        self.assertEqual(self.snapshot(), before)
        self.cli("propose", "--grouping", "normalized")

    def test_late_log_with_conflicting_receipt_fields_does_not_overwrite(self):
        self.ingest(receipt())
        before = self.snapshot()
        changed = receipt(sha="b" * 40, checks=[dict(check="unit", command="python3 -m unittest", status="fail", exit=2)])
        (self.work / (changed["id"] + ".log")).write_text("AssertionError: count mismatch\n")
        self.assertEqual(json.loads(self.ingest(changed).stdout)["imported_records"], 0)
        self.assertEqual(self.snapshot(), before)

    def test_invalid_identity_and_later_check_leave_store_unchanged(self):
        self.ingest(receipt())
        before = self.snapshot()
        invalid = [receipt(repo="net"), receipt(schema="other/v1"), receipt(sha="bad"),
                   receipt(id="../../outside"), receipt(id="cbx-1700000000-a1b2c3-"),
                   receipt(id="cbx-1700000000-a1b2c3-Edge-b2"), receipt(id="cbx-1700000000-a1b2c3-edge/b2"),
                   receipt(id="cbx-1700000000-a1b2c3-" + "a" * 21), receipt(id="cbx-1700000000-a1b2c3--ops"),
                   receipt(verdict="unknown"), receipt(verdict="cancelled"), receipt(verdict="Canceled"), receipt(started_at="2026-11-01"),
                   receipt(started_at="2026-02-30T12:00:00Z"), receipt(checks={}),
                   receipt(checks=[receipt()["checks"][0], dict(check="bad", command=3, status="fail", exit=1)]),
                   receipt(checks=[dict(check="unit", command="unit", status="fail", exit=True)]),
                   receipt(checks=[dict(check="unit", command="unit", status="unknown", exit=1)]),
                   receipt(github=dict(run_id=[])), receipt(mode=[])]
        for row in invalid:
            with self.subTest(row=row):
                result = self.ingest(row, code=2)
                message = ("coldbox receipt repo must be owner/repo; nothing imported" if row["repo"] == "net"
                           else "invalid coldbox receipt; nothing imported")
                self.assertIn(message, result.stderr)
                self.assertEqual(self.snapshot(), before)

    def test_malformed_json_leaves_no_records(self):
        for content in ("{", "[]", '{"schema":1,"schema":2}', "[" * 2000):
            with self.subTest(content=content[:30]):
                self.input.write_text(content)
                result = self.cli("ingest", "--format", "coldbox", "--input", str(self.input), code=2)
                self.assertIn("invalid coldbox receipt; nothing imported", result.stderr)
                self.assertEqual(self.records(), [])

    def test_log_tail_uses_receipt_directory_and_ignores_log_field(self):
        row = receipt(log=str(self.work / "unrelated.log"))
        (self.work / "unrelated.log").write_text("unrelated text")
        log_dir = self.work / "receipts"
        log_dir.mkdir()
        self.input = log_dir / "renamed.json"
        (log_dir / (row["id"] + ".log")).write_text("discarded prefix\n" + "\u03bb" * 9000 + "\nAssertionError: count mismatch\n", encoding="utf-8")
        self.ingest(row)
        record, = self.records()
        tail = json.loads(record["error"])["log_tail"]
        self.assertEqual(len(tail), 8000)
        self.assertEqual(tail, ("\u03bb" * 9000 + "\nAssertionError: count mismatch\n")[-8000:])

    def test_missing_log_is_tolerated_and_unknown_fields_are_ignored(self):
        self.ingest(receipt(log="/ignored.log", checks_verdict=[], extra={"ignored": True},
                            github=dict(repo=[], sha=False, run_id="41", run_attempt="1", workflow="verify", job="tests")))
        record, = self.records()
        self.assertEqual(json.loads(record["error"])["log_tail"], "")
        self.assertEqual(record["repo"], "example/app")

    def test_log_symlink_is_rejected_without_importing(self):
        unrelated = self.work / "unrelated.log"
        unrelated.write_text("unrelated text")
        (self.work / "cbx-1700000000-a1b2c3.log").symlink_to(unrelated)
        result = self.ingest(receipt(), code=2)
        self.assertIn("invalid coldbox receipt; nothing imported", result.stderr)
        self.assertEqual(self.records(), [])

    def test_stdin_is_rejected(self):
        result = self.cli("ingest", "--format", "coldbox", stdin=json.dumps(receipt()), code=2)
        self.assertIn("coldbox format requires an evidence file", result.stderr)
        self.assertEqual(self.records(), [])

    def test_ingest_and_propose_accepts_coldbox(self):
        self.input.write_text(json.dumps(receipt()))
        result = json.loads(self.cli("ingest-and-propose", "--format", "coldbox", "--input", str(self.input)).stdout)
        self.assertEqual((result["imported_records"], result["groups"]), (1, 1))
        record, = self.records()
        self.assertEqual(record["command"], "python3 -m unittest")

    def test_learn_accepts_coldbox(self):
        self.input.write_text(json.dumps(receipt()))
        result = json.loads(self.cli("learn", "--format", "coldbox", "--input", str(self.input),
                                     "--repo", str(self.work), "--before", "a" * 40, "--after", "b" * 40).stdout)
        self.assertEqual(result, dict(ingested=1, drafted=0, proven=0, delivered=0, skipped_flaky=0))
        record, = self.records()
        self.assertEqual(record["repo"], "example/app")


if __name__ == "__main__":
    unittest.main()
