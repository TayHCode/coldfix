"""Verify FAIL-then-PASS pair importer proofs. Synthetic repository and logs; no network or credentials."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import coldfix as c
import failure_ingest as fi
import verify_pairs as vp

FAIL_LOG = "$ python3 -m unittest\nF\nFAIL: test_total (t.Totals.test_total)\nTraceback (most recent call last):\nAssertionError: 3 != 4\n\nRan 3 tests in 0.1s\n\nFAILED (failures=1)\n"
PASS_LOG = "$ python3 -m unittest\n...\nRan 3 tests in 0.1s\n\nOK\n"
FLEET_PASS = "$ /usr/bin/bash .fleet/verify.sh\n...\nRan 5 tests in 0.1s\n\nOK\nFLEET_TESTS=5\nPASS: real Linux verification on dev-box\n"
QUEUE_LOG = "queued behind 2; waiting for verify lock\nQUEUED/INCOMPLETE: verify queue wait limit reached\n"
INFRA_LOG = "$ python3 -m unittest\n...........Terminated                 python3 -B -m unittest discover -s tests\n"


class VerifyPairTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        self.projects = self.work / "projects"
        self.settle = self.work / "settle"
        self.settle.mkdir()
        self.repo = self.projects / "proj"
        self.repo.mkdir(parents=True)
        self.store = self.work / ".coldfix"
        fleet = mock.patch.dict(os.environ, {"COLDFIX_FLEET_CLI": "acme"})
        fleet.start()
        self.addCleanup(fleet.stop)
        self.git("init", "-q", "-b", "main")
        self.base = self.commit("base", "total = 0\n")

    def git(self, *args):
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
        return subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, text=True, check=True, env=env).stdout.strip()

    def commit(self, message, content, branch=None):
        if branch:
            self.git("checkout", "-q", "-B", branch, self.base if hasattr(self, "base") else "HEAD")
        (self.repo / "f.txt").write_text(content)
        self.git("add", "f.txt")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def loop_log(self, head, text, tag="aaaaaaaa"):
        folder = self.repo / ".git" / "verify-loop"
        folder.mkdir(exist_ok=True)
        (folder / f"{head[:12]}-{self.base[:12]}-{tag}.log").write_text(text)

    def settle_out(self, name, head, verdict, underlying):
        (self.settle / f"settle-{name}.out").write_text(
            f"start 2026-10-31T10:00:00+00:00\npr: OPEN {head}\n{verdict} tests=3 head={head[:12]} pr_head={head[:12]} base=main@{self.base[:12]}\n")
        (self.settle / f"settle-{name}-verify.log").write_text(underlying)

    def prs(self, *rows):
        common = os.path.realpath(self.repo / ".git")
        return vp.Repos([self.repo], prs={common: [dict(number=n, headRefOid=commits[-1], commits=[dict(oid=o) for o in commits]) for n, commits in rows]})

    def build(self):
        self.git("checkout", "-q", "-b", "pr1")
        fail1 = self.commit("pr1 broken", "total = 3\n")
        fix1 = self.commit("pr1 fix", "total = 4\n")
        self.git("checkout", "-q", "main")
        flaky = self.commit("pr2 flaky", "total = 5\n", branch="pr2")
        self.git("checkout", "-q", "main")
        queued = self.commit("pr3 queued", "total = 6\n", branch="pr3")
        infra = self.commit("pr3 infra", "total = 7\n")
        orphan = self.commit("no pr", "total = 8\n", branch="orphan")
        fail4 = self.commit("pr4 broken", "total = 9\n", branch="pr4")
        fix4 = self.commit("pr4 fix", "total = 10\n")
        self.loop_log(fail1, FAIL_LOG)
        self.loop_log(fix1, PASS_LOG)
        self.loop_log(flaky, FAIL_LOG, "bbbbbbbb")
        self.loop_log(flaky, PASS_LOG, "cccccccc")
        self.loop_log(queued, QUEUE_LOG)
        self.loop_log(infra, INFRA_LOG)
        self.loop_log(orphan, FAIL_LOG)
        self.loop_log("deadbeefdead", FAIL_LOG)
        self.loop_log(self.base, "nothing useful here\n", "dddddddd")
        self.settle_out("pr4", fail4, "FAIL:", FAIL_LOG)
        self.loop_log(fix4, FLEET_PASS)
        return dict(fail1=fail1, fix1=fix1, fail4=fail4, fix4=fix4), self.prs(
            (1, [fail1, fix1]), (2, [flaky]), (3, [queued, infra]), (4, [fail4, fix4]))

    def test_valid_pairs_become_evidence_and_each_reject_reason_is_counted(self):
        heads, repos = self.build()
        result = vp.run(self.store, self.projects, self.settle, repos=repos)
        self.assertEqual(result["pairs"], 2)
        self.assertEqual(result["ingested"], 2)
        self.assertEqual(result["rejected"], {"queue-timeout": 1, "infra-terminated": 1, "no-verdict": 1,
                                              "missing-commits": 1, "no-pr-join": 1, "same-head": 1})
        self.assertEqual(sorted(result["sources"]), sorted([
            f"verify-pair:proj:1:{heads['fail1'][:12]}:{heads['fix1'][:12]}",
            f"verify-pair:proj:4:{heads['fail4'][:12]}:{heads['fix4'][:12]}"]))
        rows = {row["source_ref"]: json.loads(row["error"]) for row in c.load_store(self.store)[1]["failures"].values()}
        pair = rows[f"verify-pair:proj:1:{heads['fail1'][:12]}:{heads['fix1'][:12]}"]
        self.assertEqual(pair["failing_check"]["head"], heads["fail1"])
        self.assertIn("AssertionError: 3 != 4", pair["failing_check"]["excerpt"])
        self.assertEqual(pair["passing_check"]["head"], heads["fix1"])
        self.assertIn("OK", pair["passing_check"]["excerpt"])
        self.assertIn("-total = 3", pair["diff"]["patch"])
        self.assertIn("+total = 4", pair["diff"]["patch"])
        settled = rows[f"verify-pair:proj:4:{heads['fail4'][:12]}:{heads['fix4'][:12]}"]
        self.assertIn("PASS: real Linux verification", settled["passing_check"]["excerpt"])

    def test_rerun_is_idempotent(self):
        _, repos = self.build()
        vp.run(self.store, self.projects, self.settle, repos=repos)
        again = vp.run(self.store, self.projects, self.settle, repos=repos)
        self.assertEqual((again["pairs"], again["ingested"], again["already_present"]), (2, 0, 2))
        self.assertEqual(len(c.load_store(self.store)[1]["failures"]), 2)

    def test_imported_pairs_do_not_break_grouping(self):
        _, repos = self.build()
        vp.run(self.store, self.projects, self.settle, repos=repos)
        rows = list(c.load_store(self.store)[1]["failures"].values())
        self.assertEqual(len(rows), 2)
        for row in rows:
            command, content, exact = fi.grouping_content(row)
            self.assertEqual(command, "acme verify")
            self.assertFalse(exact)
            self.assertIn("AssertionError", content)
            self.assertTrue(c.signature(row))

    def test_rerun_survives_an_earlier_sorting_log_for_an_imported_head(self):
        heads, repos = self.build()
        vp.run(self.store, self.projects, self.settle, repos=repos)
        self.loop_log(heads["fail1"], FAIL_LOG, "00000000")
        again = vp.run(self.store, self.projects, self.settle, repos=repos)
        self.assertEqual((again["pairs"], again["ingested"], again["already_present"]), (2, 0, 2))

    def test_a_pass_that_predates_the_failure_is_not_a_fix(self):
        fail = self.commit("broken", "total = 3\n", branch="old")
        fix = self.commit("fixed", "total = 4\n")
        self.loop_log(fail, FAIL_LOG)
        self.loop_log(fix, PASS_LOG)
        folder = self.repo / ".git" / "verify-loop"
        os.utime(folder / f"{fix[:12]}-{self.base[:12]}-aaaaaaaa.log", (1_700_000_000, 1_700_000_000))
        result = vp.run(self.store, self.projects, self.settle, repos=self.prs((1, [fail, fix])))
        self.assertEqual((result["pairs"], result["rejected"]), (0, {"no-later-pass": 1}))

    def test_classification_uses_underlying_output_not_only_the_summary_line(self):
        head = "a" * 12
        summary = f"FAIL: tests=3 head={head} pr_head={head} base=main@{'b' * 12}\n"
        self.assertEqual(vp.classify(QUEUE_LOG + summary, head)[:2], ("REJECT", "queue-timeout"))
        self.assertEqual(vp.classify(INFRA_LOG + summary, head)[:2], ("REJECT", "infra-terminated"))
        self.assertEqual(vp.classify(FAIL_LOG + summary, head)[0], "FAIL")
        fixture = f"FAIL: tests=0 head={'a' * 12} base=main@{'b' * 12}\n"
        self.assertEqual(vp.classify(PASS_LOG + fixture, "c" * 12)[0], "PASS")

    def test_settle_failure_without_a_verdict_line_is_a_fail_when_cold_verify_exited_nonzero(self):
        self.git("checkout", "-q", "-b", "pr9")
        fail = self.commit("pr9 broken", "total = 8\n")
        fix = self.commit("pr9 fix", "total = 9\n")
        (self.settle / "settle-pr9.out").write_text(
            f"start 2026-10-31T10:00:00+00:00 pr9 proj#9 {fail}\npr: OPEN {fail}\n"
            "FAIL: test_total (t.Totals.test_total)\nFAILED (failures=1)\nFAIL: real verification command failed\n"
            "verify failed\nacme verify at head exit 1\nverify failed/incomplete at head\n")
        self.loop_log(fix, PASS_LOG)
        result = vp.run(self.store, self.projects, self.settle, repos=self.prs((9, [fail, fix])))
        self.assertEqual((result["pairs"], result["ingested"]), (1, 1))
        row = next(iter(c.load_store(c.ordinary_path(self.store))[1]["failures"].values()))
        self.assertIn("FAILED (failures=1)", row["error"])

    def test_runner_settle_output_names_its_head_only_on_the_start_line(self):
        self.git("checkout", "-q", "-b", "pr7")
        fail = self.commit("pr7 broken", "total = 11\n")
        fix = self.commit("pr7 fix", "total = 12\n")
        self.loop_log(fail, FAIL_LOG)
        os.utime(self.repo / ".git" / "verify-loop" / f"{fail[:12]}-{self.base[:12]}-aaaaaaaa.log", (1_700_000_000, 1_700_000_000))
        (self.settle / "settle-pr7.out").write_text(
            f"start 2026-10-31T11:00:00+00:00 pr7 proj#7 {fix} on runner-a\n"
            "acme verify exit 0\nPASS: real Linux verification on runner-a\nverify_loop exit 2\n")
        result = vp.run(self.store, self.projects, self.settle, repos=self.prs((7, [fail, fix])))
        self.assertEqual((result["pairs"], result["rejected"]), (1, {}))

    def test_settle_output_with_a_zero_exit_and_no_verdict_is_still_rejected(self):
        self.git("checkout", "-q", "-b", "pr8")
        head = self.commit("pr8", "total = 10\n")
        (self.settle / "settle-pr8.out").write_text(f"start 2026-10-31T10:00:00+00:00\npr: OPEN {head}\nacme verify at head exit 0\n")
        self.loop_log(head, PASS_LOG)
        result = vp.run(self.store, self.projects, self.settle, repos=self.prs((8, [head])))
        self.assertEqual((result["pairs"], result["rejected"].get("no-verdict")), (0, 1))

    def test_fail_that_is_not_an_ancestor_of_the_pass_is_rejected(self):
        self.git("checkout", "-q", "-b", "left")
        left = self.commit("left", "left\n")
        self.git("checkout", "-q", "-b", "right", self.base)
        right = self.commit("right", "right\n")
        self.loop_log(left, FAIL_LOG)
        self.loop_log(right, PASS_LOG)
        result = vp.run(self.store, self.projects, self.settle, repos=self.prs((1, [left, right])))
        self.assertEqual((result["pairs"], result["rejected"]), (0, {"not-ancestor": 1}))
        self.assertFalse(self.store.exists())


if __name__ == "__main__":
    unittest.main()
