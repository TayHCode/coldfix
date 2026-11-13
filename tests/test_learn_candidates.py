import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import coldfix as c
import failure_ingest as ingest
import learn_candidates as lc


ROOT = Path(__file__).resolve().parents[1]
RUN = json.loads((ROOT / "tests" / "fixtures" / "failed-ci-run.json").read_text())
REPRO = "verify\tRun verify\t2026-11-01T12:00:{:02d}.1234567Z Bug-fix repro gate: FAIL (evidence: /tmp/work-{}/.cold/verify-repro.json)"


class LearnCandidatesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Path(temporary.name) / ".coldfix"
        self.store.mkdir(mode=0o700)
        self.run_id = 1000

    def failure(self, repo, sha, log, retried=False, **extra):
        self.run_id += 1
        row = dict(RUN, repo=repo, head_sha=sha * 40, run_id=self.run_id, log_tail=log, **extra)
        if retried:
            row["retry_evidence"] = dict(attempt=2, head_sha=sha * 40, conclusion="success",
                                         url=f"https://github.com/{repo}/actions/runs/{self.run_id}/attempts/2")
        ingest.import_records(self.store, [ingest.record(ingest.ci_event(row))])

    def manual(self, error, repo="example/one"):
        row = dict(command="verify", error=error, repo=repo, agent="fixture", kind="test",
                   source_ref="manual:" + str(self.run_id), occurred="2026-10-20T12:00:00Z")
        self.run_id += 1
        result = subprocess.run([sys.executable, str(ROOT / "coldfix.py"), "--store", str(self.store), "ingest"],
                                input=json.dumps(row), text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def candidates(self, exclude=()):
        return lc.candidates(c.load_store(self.store)[1], exclude)

    def test_same_error_line_groups_despite_timestamps_paths_and_numbers(self):
        for index, sha in enumerate("abc"):
            self.failure("example/one", sha, "noise\n" + REPRO.format(index, index) + "\n##[error]Process completed with exit code 1.")
        [row] = self.candidates()
        self.assertEqual((row["commits"], row["repos"], len(row["evidence"])), (3, ["example/one"], 3))
        self.assertEqual(row["headline"], "Bug-fix repro gate: FAIL (evidence: <path>)")

    def test_threshold_is_three_commits_or_two_repositories(self):
        self.failure("example/one", "a", "AssertionError: one")
        self.failure("example/one", "b", "AssertionError: one")
        self.failure("example/one", "b", "AssertionError: one", attempt=2)
        self.assertEqual(self.candidates(), [])
        self.failure("example/two", "c", "AssertionError: one")
        [row] = self.candidates()
        self.assertEqual((row["commits"], row["repos"]), (3, ["example/one", "example/two"]))
        self.assertEqual(self.candidates(exclude={row["fingerprint"]}), [])

    def test_flaky_and_infrastructure_failures_never_become_candidates(self):
        for repo in ("example/one", "example/two"):
            self.failure(repo, "a", "Error: retried", retried=True)
            self.failure(repo, "b", "Error: could not resolve host example.com")
            self.failure(repo, "c", "Error: passed later")
            passed = dict(RUN, repo=repo, head_sha="c" * 40, run_id=self.run_id, attempt=2, conclusion="success", log_tail="")
            ingest.import_records(self.store, [ingest.record(ingest.ci_event(passed))])
        store = c.load_store(self.store)[1]
        categories = sorted(lc.flakiness.metadata(r)["classification"]["category"] for r in store["failures"].values())
        self.assertEqual(categories, ["env-infra", "env-infra", "flake", "flake", "unknown", "unknown"])
        self.assertEqual(self.candidates(), [])

    def test_commits_come_from_any_recorded_revision_and_unknown_revisions_count_once(self):
        for _ in range(3):
            self.manual(json.dumps(dict(head="d" * 40, status="fail")))
            self.manual("plain failure text")
        self.assertEqual(self.candidates(), [])
        self.manual(json.dumps(dict(head="e" * 40, status="fail")))
        self.manual(json.dumps(dict(head="f" * 40, status="fail")))
        [row] = self.candidates()
        self.assertEqual((row["commits"], len(row["evidence"])), (3, 5))

    def test_non_object_classification_is_not_a_category(self):
        for head in "abc":
            self.manual(json.dumps(dict(classification="error", head=head * 40)))
        [row] = self.candidates()
        self.assertEqual(row["commits"], 3)

    def test_unhashable_category_is_not_a_category(self):
        for head in "abc":
            self.manual(json.dumps(dict(classification=dict(category=[]), head=head * 40)))
        [row] = self.candidates()
        self.assertEqual(row["commits"], 3)

    def test_job_comes_from_the_log_line_and_tabs_inside_the_diagnostic_are_kept(self):
        line = "unit\tRun tests\t2026-11-01T12:00:00.1Z ERROR\ttest_case\texpected value"
        for sha, job in (("a", "unit"), ("b", "unit; lint"), ("c", "lint; unit")):
            self.failure("example/one", sha, "noise " + sha + "\n" + line, job=job)
        [row] = self.candidates()
        self.assertEqual((row["commits"], row["headline"]), (3, "ERROR test_case expected value"))

    def test_a_single_ci_failure_naming_a_unittest_is_a_candidate_without_recurring(self):
        self.failure("example/one", "a", "FAIL: test_value (tests.test_value.ValueTests.test_value)\nAssertionError: 1 != 2")
        self.failure("example/one", "b", "AssertionError: unrelated")
        rows = self.candidates()
        self.assertEqual([(row["commits"], row.get("replayable")) for row in rows], [(1, True)])

    def test_generic_summary_lines_do_not_merge_unrelated_failures(self):
        self.failure("example/one", "a", "FAILED (1): python tests\n##[error]Process completed with exit code 1.")
        self.failure("example/two", "b", "FAILED (2): unit\n##[error]Process completed with exit code 1.")
        self.failure("example/three", "c", "##[error]FAILED (1): tests\n##[error]Process completed with exit code 1.")
        self.failure("example/four", "d", "##[error]FAILED (2): tests\n##[error]Process completed with exit code 1.")
        self.assertEqual(self.candidates(), [])


if __name__ == "__main__":
    unittest.main()
