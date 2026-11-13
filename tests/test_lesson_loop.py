import json
import subprocess
import unittest

import test_cli


class LessonLoopTests(unittest.TestCase):
    setUp = test_cli.CliTests.setUp
    run_cli = test_cli.CliTests.run_cli
    event = test_cli.CliTests.event
    def loop(self, events=(), fixes=None, code=0, timeout=None):
        args = ["ingest-and-propose"]
        if timeout is not None:
            args += ["--timeout", str(timeout)]
        if fixes is not None:
            path = self.cwd / "fixes.json"
            path.write_text(json.dumps(fixes))
            args += ["--fixes", str(path)]
        self.run_cli(*args, stdin="\n".join(json.dumps(e) for e in events), code=code)
        if code == 0:
            return json.loads((self.store / "pending.json").read_text())["pending"]

    def test_normalized_recurrence_and_retry(self):
        rows = self.loop([self.event(error="line 10 failed"), self.event(error="line 20 failed"),
                          self.event(error="different failure")])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "test")
        self.assertEqual(rows[0]["replay"]["status"], "INCOMPLETE")
        self.assertEqual(self.loop()[0]["proposal_id"], rows[0]["proposal_id"])
        self.assertEqual(len(list((self.store / "proposals").glob("*.json"))), 1)
        self.assertEqual(len(json.loads((self.store / "groups.json").read_text())["groups"]), 2)
        self.assertFalse((self.cwd / "exports").exists())

    def test_reingesting_the_same_event_is_not_a_recurrence(self):
        event = self.event()
        self.assertEqual(self.loop([event]), [])
        self.loop([event])
        self.assertEqual(len(list((self.store / "failures").glob("*.json"))), 1)
        self.assertFalse((self.store / "proposals").exists())
        result = json.loads(self.run_cli("ingest-and-propose", stdin=json.dumps(event)).stdout)
        self.assertEqual((result["imported_records"], result["proposals"]), (0, 0))
        skipped = json.loads(self.run_cli("ingest", stdin=json.dumps(event)).stdout)
        self.assertEqual((skipped["imported_records"], skipped["skipped_duplicates"], skipped["failure_ids"]), (0, 1, []))
        rows = self.loop([self.event(source_ref="thread:another-occurrence")])
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(list((self.store / "failures").glob("*.json"))), 2)

    def test_replay_and_existing_lesson(self):
        first = self.event()
        self.run_cli("ingest", stdin=json.dumps(first) + "\n" + json.dumps(first))
        group = json.loads(self.run_cli("propose").stdout)["groups"][0]["group_id"]
        self.run_cli("propose", "--group", group, "--kind", "test", "--title", "Existing rule",
                     "--rule", "Keep this rule.", "--check-command", "python3 -m unittest")
        rows = self.loop([self.event(repo="another-repo", source_ref="second")])
        repo = self.cwd / "repo"
        repo.mkdir()
        def git(*args):
            return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()
        git("init", "-q")
        git("config", "user.name", "Fixture")
        git("config", "user.email", "fixture@example.invalid")
        target = repo / "value"
        target.write_text("bad")
        git("add", ".")
        git("commit", "-qm", "before")
        before = git("rev-parse", "HEAD")
        target.write_text("good")
        git("commit", "-qam", "after")
        after = git("rev-parse", "HEAD")
        check = self.cwd / "check.py"
        check.write_text('import json,sys\nfrom pathlib import Path\nok=Path("value").read_text()=="good"\n'
                         'print(json.dumps({"status":"PASS"} if ok else {"status":"FAIL","failure":"value"}))\n'
                         'sys.exit(0 if ok else 1)\n')
        fix = dict(repo=str(repo), before=before, after=after, check_file=str(check),
                   expected_failure="value", kind="test")
        proven = self.loop(fixes={rows[0]["group_id"]: fix})[0]
        self.assertEqual(proven["replay"]["status"], "PASS")
        self.assertEqual(proven["replay"]["verification"], "proven")
        lesson = json.loads((self.store / "lessons" / (proven["replay"]["id"] + ".json")).read_text())
        self.assertEqual(lesson["status"], "active")
        self.assertEqual(lesson["scope"], "repo:" + git("rev-list", "--max-parents=0", "HEAD"))
        self.assertEqual(len(lesson["evidence"]), 3)
        self.assertEqual(lesson["rule"], "Keep this rule.")
        self.assertEqual(self.loop()[0]["replay"], proven["replay"])
        (self.store / "pending.json").unlink()
        self.assertEqual(self.loop()[0]["replay"], proven["replay"])
        self.assertEqual(len(list((self.store / "proposals").glob("*.json"))), 1)
        self.run_cli("propose", "--grouping", "normalized", "--group", rows[0]["group_id"],
                     "--kind", "test", "--title", "Different proposal", "--rule", "Different rule",
                     "--check-command", "python3 -m unittest")
        self.assertEqual(self.loop()[0]["proposal_id"], rows[0]["proposal_id"])
        (self.store / "lessons" / (proven["replay"]["id"] + ".json")).unlink()
        (self.store / "receipts" / (proven["replay"]["receipt_id"] + ".json")).unlink()
        unchanged = self.loop([self.event(repo="third-repo")], fixes={rows[0]["group_id"]: fix})[0]
        retained_path = self.store / "lessons" / (unchanged["replay"]["id"] + ".json")
        refreshed = json.loads(retained_path.read_text())
        self.assertEqual(len(refreshed["evidence"]), 4)
        retained_path.unlink()
        (self.store / "lessons" / (refreshed["previous"] + ".json")).unlink()
        (self.store / "receipts" / (unchanged["replay"]["receipt_id"] + ".json")).unlink()
        check.write_text(check.read_text() + "\n# Changed authorized check\n")
        resumed = self.loop(fixes={rows[0]["group_id"]: fix})[0]
        self.assertEqual(resumed["replay"]["status"], "PASS")
        retained = json.loads((self.store / "lessons" / (resumed["replay"]["id"] + ".json")).read_text())
        self.assertEqual(retained["check_script"], check.read_text())
        self.assertEqual(len(retained["evidence"]), 4)
        self.assertEqual(resumed["proposal_id"], rows[0]["proposal_id"])
        (self.store / "lessons" / (resumed["replay"]["id"] + ".json")).unlink()
        (self.store / "lessons" / (retained["previous"] + ".json")).unlink()
        (self.store / "receipts" / (resumed["replay"]["receipt_id"] + ".json")).unlink()
        check.write_text("import time\ntime.sleep(60)\n")
        timed = self.loop(fixes={rows[0]["group_id"]: fix}, timeout=0.01)[0]
        self.assertEqual(timed["replay"]["status"], "INCOMPLETE")
        receipt = json.loads((self.store / "receipts" / (timed["replay"]["receipt_id"] + ".json")).read_text())
        self.assertIn("Check timed out", receipt["before"]["stderr"])
        self.assertFalse((self.cwd / "exports").exists())

    def test_invalid_fix_prevents_ingestion(self):
        self.loop([self.event()], fixes={"bad": {}}, code=2)
        self.assertFalse((self.store / "failures").exists())

    def test_replay_failure_is_reported(self):
        # Existing replay tests cover wrong-check and unavailable revision classification.
        rows = self.loop([self.event(), self.event()])
        check = self.cwd / "check.py"
        check.write_text('print("unrecognized output")')
        row = self.loop(fixes={rows[0]["group_id"]: dict(repo=str(self.cwd), before="missing",
            after="missing", check_file=str(check), expected_failure="value", kind="lint")})[0]
        self.assertEqual(row["replay"]["status"], "INCOMPLETE")

    def test_invalid_check_prevents_ingestion(self):
        check = self.cwd / "empty.py"
        for content in ("", "\x00"):
            check.write_text(content)
            self.loop([self.event()], fixes={"a" * 64: dict(repo=str(self.cwd),
                before="before", after="after", check_file=str(check), expected_failure="value",
                kind="test")}, code=2)
            self.assertFalse((self.store / "failures").exists())

    def test_invalid_replay_path_prevents_ingestion(self):
        check = self.cwd / "check.py"
        check.write_text('print("unused")')
        self.loop([self.event(), self.event()], fixes={"a" * 64: dict(repo="../repo",
            before="before", after="after", check_file=str(check), expected_failure="value",
            kind="test")}, code=2)
        self.assertFalse((self.store / "failures").exists())

    def test_publicly_authored_pending_lesson_remains_visible(self):
        proposal = self.loop([self.event(), self.event()])[0]
        check = self.cwd / "check.py"
        check.write_text('print("unused")')
        authored = json.loads(self.run_cli("lesson", "--from-proposal", proposal["proposal_id"],
            "--level", "test", "--check-command", "python3 {check}", "--check-file", str(check),
            "--expected-failure", "value").stdout)
        row = self.loop()[0]
        self.assertIsNone(row["proposal_id"])
        self.assertEqual(row["replay"]["id"], authored["id"])
        self.assertEqual(row["replay"]["status"], "INCOMPLETE")

    def test_reuses_existing_exact_proposal(self):
        self.run_cli("capture", "--command", "python3 -m unittest", "--repo", "example",
                     "--agent", "worker", "--error", "AssertionError: expected 2")
        captured = json.loads(self.run_cli("capture", "--command", "python3 -m unittest",
            "--repo", "example", "--agent", "worker", "--error", "AssertionError: expected 2").stdout)
        proposal = json.loads(self.run_cli("propose", "--group", captured["group_id"],
            "--kind", "lint", "--title", "Existing authored proposal", "--rule", "Keep the authored rule.",
            "--check-command", "python3 custom_check.py").stdout)
        row = self.loop()[0]
        self.assertEqual(row["proposal_id"], proposal["proposal_id"])
        self.assertEqual(row["rule"], "Keep the authored rule.")
        self.assertEqual(row["kind"], "lint")
        self.assertEqual(row["check_command"], "python3 custom_check.py")
        self.assertEqual(len(list((self.store / "proposals").glob("*.json"))), 1)

    def test_credential_collision_does_not_block_other_groups(self):
        rows = self.loop([self.event(error="token=test"), self.event(error="token=test"),
                          self.event(error="ordinary failure"), self.event(error="ordinary failure")])
        self.assertEqual(len(rows), 2)
        blocked = next(row for row in rows if row["proposal_id"] is None)
        self.assertEqual(blocked["replay"]["status"], "INCOMPLETE")
        self.assertIn("Proposal validation blocked", blocked["replay"]["reason"])
        self.assertEqual(len(list((self.store / "proposals").glob("*.json"))), 1)

    def test_reused_rule_privacy_conflict_does_not_starve_batch(self):
        self.run_cli("ingest", stdin="\n".join(json.dumps(self.event()) for _ in range(2)))
        group = json.loads(self.run_cli("propose").stdout)["groups"][0]["group_id"]
        self.run_cli("propose", "--group", group, "--kind", "test", "--title", "Sensitiveword rule",
                     "--rule", "Check Sensitiveword.", "--check-command", "python3 -m unittest")
        rows = self.loop([self.event(source_ref="token=Sensitiveword"),
                          self.event(error="unrelated"), self.event(error="unrelated")])
        key = next(row["group_id"] for row in rows if row["title"] == "Sensitiveword rule")
        check = self.cwd / "check.py"
        check.write_text('print("unused")')
        rows = self.loop(fixes={key: dict(repo=str(self.cwd), before="before", after="after",
            check_file=str(check), expected_failure="value", kind="test")})
        self.assertEqual(len(rows), 2)
        blocked = next(row for row in rows if row["title"] == "Sensitiveword rule")
        self.assertEqual(blocked["replay"]["status"], "INCOMPLETE")
        self.assertIn("Lesson validation blocked", blocked["replay"]["reason"])

    def test_invalid_timeout_prevents_ingestion(self):
        self.loop([self.event()], timeout=0, code=2)
        self.assertFalse((self.store / "failures").exists())
