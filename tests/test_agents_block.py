from datetime import datetime, timezone
import json
import io
from contextlib import redirect_stdout
import subprocess
import unittest
from unittest import mock

import transcript_delivery as delivery
import learn_daily
from test_transcript_delivery import Fixture

import check_delivery
import coldfix as c
from test_cli import CliCase


class AgentsBlockTests(CliCase):
    def agents(self, repo):
        return json.loads(self.run_cli("agents", "--repo", str(repo)).stdout)

    def test_proven_lessons_reach_only_their_repositories_without_coldlocker(self):
        revision = self.author_lesson()
        repo, before, after = self.history()
        unrelated, _, _ = self.history("unrelated")
        (repo / "AGENTS.md").write_text("# Rules\n\nKeep this line.\n")
        (repo / "AGENTS.md").chmod(0o664)
        (repo / "CLAUDE.md").write_text("Claude notes.\n")
        (repo / "sub").mkdir()

        self.assertEqual(self.agents(repo)["lesson_count"], 0)
        self.assertEqual((repo / "AGENTS.md").read_text(), "# Rules\n\nKeep this line.\n")

        proven = json.loads(self.run_replay(revision, repo, before, after, "--status", "active").stdout)
        self.assertEqual(proven["verification"], "proven")
        result = self.agents(repo / "sub")
        self.assertEqual((result["lessons"], result["written"]), ([proven["lesson_id"]], ["AGENTS.md", "CLAUDE.md"]))
        ref = "coldfix:lesson:" + proven["lesson_id"] + ":" + proven["id"]
        for name, kept in (("AGENTS.md", "# Rules\n\nKeep this line.\n"), ("CLAUDE.md", "Claude notes.\n")):
            text = (repo / name).read_text()
            self.assertTrue(text.startswith(kept + "\n<!-- coldfix:lessons:begin -->\n"), text)
            self.assertTrue(text.endswith("<!-- coldfix:lessons:end -->\n"))
            self.assertIn("Require the expected count.", text)
            self.assertIn(ref, text)
            self.assertNotIn("private-evidence-canary", text)
        self.assertEqual((repo / "AGENTS.md").stat().st_mode & 0o777, 0o664)
        self.assertFalse((repo / "sub" / "AGENTS.md").exists())
        self.assertFalse((repo / ".coldlocker").exists())

        delivered = (repo / "AGENTS.md").read_bytes()
        self.assertEqual(self.agents(repo)["written"], [])
        self.assertEqual((repo / "AGENTS.md").read_bytes(), delivered)

        self.assertEqual(self.agents(unrelated)["lesson_count"], 0)
        self.assertFalse((unrelated / "AGENTS.md").exists())
        self.assertFalse((unrelated / "CLAUDE.md").exists())

        global_revision = self.prove_elsewhere(proven["id"])
        self.assertEqual(self.agents(unrelated)["written"], ["AGENTS.md"])
        self.assertIn("coldfix:lesson:" + proven["lesson_id"] + ":" + global_revision, (unrelated / "AGENTS.md").read_text())
        self.assertFalse((unrelated / "CLAUDE.md").exists())

        (self.store / "invalidations.json").write_text(json.dumps({proven["lesson_id"]: global_revision}))
        self.assertEqual(self.agents(repo)["lesson_count"], 0)
        self.assertEqual((repo / "AGENTS.md").read_text(), "# Rules\n\nKeep this line.\n")
        self.assertEqual((repo / "CLAUDE.md").read_text(), "Claude notes.\n")
        self.agents(unrelated)
        self.assertFalse((unrelated / "AGENTS.md").exists())

    def test_retirement_refreshes_both_routes_and_reports_disconnected_paths(self):
        repo, before, after = self.history()
        revision = self.author_lesson()
        proven = json.loads(self.run_replay(revision, repo, before, after, "--status", "active").stdout)
        fixture = Fixture(self.cwd)
        fixture.root = self.store
        (self.store / learn_daily.LEDGER).write_text(json.dumps(dict(drafts={
            "draft": dict(replay=dict(status="proven", proof=dict(id=proven["id"]), repo=str(repo)))})))
        checks = self.cwd / "checks.json"
        checks.write_text(json.dumps({proven["lesson_id"]: "example-count-check"}))
        offline = self.cwd / "offline"
        subprocess.run(["git", "clone", "--quiet", str(repo), str(offline)], check=True)
        (repo / "CLAUDE.md").write_text("Keep local notes.\n")
        with mock.patch.object(delivery, "LESSON_CHECKS", checks), mock.patch.object(delivery, "HOOK_READ_SECONDS", 0):
            fixture.deliver()
            ledger_path = self.store / delivery.DELIVERIES
            record = json.loads(ledger_path.read_text())["proven"][proven["lesson_id"]]
            lesson, source = record["lesson"], fixture.folder / record["file"]
            self.assertIn("Require the expected count.", fixture.file("briefing.md"))
            for checkout in (repo, offline):
                self.assertEqual(self.agents(checkout)["lessons"], [proven["lesson_id"]])
            moved = offline.rename(self.cwd / "unmounted")
            retire = ("retire", lesson, "--lessons", str(fixture.folder))
            result = json.loads(self.run_cli(*retire).stdout)
            self.assertNotIn("Require the expected count.", fixture.file("briefing.md"))
            self.assertFalse((repo / "AGENTS.md").exists())
            self.assertEqual((repo / "CLAUDE.md").read_text(), "Keep local notes.\n")
            self.assertEqual(result["pending_refresh"], [str(offline / "AGENTS.md")])
            files = (ledger_path, source, fixture.folder / "briefing.md", repo / "CLAUDE.md")
            before_retry = [(p.read_bytes(), p.stat().st_mtime_ns) for p in files]
            self.assertEqual(json.loads(self.run_cli(*retire).stdout), result)
            self.assertEqual([(p.read_bytes(), p.stat().st_mtime_ns) for p in files], before_retry)
            fixture.deliver()
            self.prove_elsewhere(proven["id"])
            exported = self.run_cli("export", "--format", "briefing", "--output", "after.md")
            self.assertEqual(json.loads(exported.stdout)["lesson_count"], 0)
            rejected = self.run_cli("export", "--lesson", proven["lesson_id"], "--output", "rule.md", code=2)
            self.assertIn("vetoed", rejected.stderr)
            self.assertNotIn("Require the expected count.", fixture.file("briefing.md"))
            moved.rename(offline)
            self.assertNotIn("pending_refresh", json.loads(self.run_cli(*retire).stdout))
            for checkout in (repo, offline):
                self.assertEqual(self.agents(checkout)["lessons"], [])
                self.assertFalse((checkout / "AGENTS.md").exists())

    def test_veto_command_refreshes_registered_copies_without_conferring_delivery_store_ownership(self):
        repo, before, after = self.history()
        proven = json.loads(self.run_replay(self.author_lesson(), repo, before, after, "--status", "active").stdout)
        target = repo / "AGENTS.md"
        self.agents(repo)
        fixture = Fixture(self.cwd)
        fixture.root = self.store
        rejected = self.run_cli("retire", "L1", "--lessons", str(fixture.folder), code=2)
        self.assertIn("no delivery ledger", rejected.stderr)
        self.assertIn("status: active", fixture.file("L1-docs-sync.md"))

        (self.store / learn_daily.LEDGER).write_text(json.dumps(dict(drafts={
            "draft": dict(replay=dict(status="proven", proof=dict(id=proven["id"]), repo=str(repo)))})))
        fixture.deliver()
        ledger = json.loads((self.store / delivery.DELIVERIES).read_text())
        lesson = ledger["proven"][proven["lesson_id"]]["lesson"]
        moved = repo.rename(self.cwd / "unmounted")
        output = io.StringIO()
        with mock.patch.object(delivery, "FOLDER", str(fixture.folder)), mock.patch.object(delivery, "TOOL", str(fixture.tool)), \
                mock.patch.object(delivery, "RULES", str(fixture.rules)), mock.patch.object(delivery, "HOOK_READ_SECONDS", 0), \
                redirect_stdout(output):
            self.assertEqual(delivery.main(["--store", str(self.store), "veto", lesson]), 0)
        self.assertEqual(json.loads(output.getvalue())["pending_refresh"], [str(target)])
        moved.rename(repo)
        self.run_cli("retire", lesson, "--lessons", str(fixture.folder))
        self.assertFalse(target.exists())

    def test_an_existing_test_delivery_carries_its_rule_into_the_repository_export(self):
        revision = self.author_lesson()
        repo, before, after = self.history()
        proven = json.loads(self.run_replay(revision, repo, before, after, "--status", "active").stdout)
        ledger = dict(drafts={"draft": dict(replay=dict(status="proven", proof=dict(id=proven["id"]), repo=str(repo)))})
        [lesson] = check_delivery.proven(self.store, c.load_store(self.store)[1], ledger)
        lesson["payload"] = dict(path="tests/test_count.py", diagnostic="FAIL: test_count_matches")
        info = dict(visibility="PRIVATE", isArchived=False, defaultBranchRef=dict(name="main"))
        with mock.patch.object(check_delivery, "repository", return_value=info), \
                mock.patch.object(check_delivery, "on_default_branch", return_value="c" * 40):
            check_delivery.deliver(self.store, [lesson], datetime.now(timezone.utc))
        row = check_delivery.records(self.store)[lesson["lesson_id"]]
        self.assertEqual((row["state"], row["route"]), ("delivered", "existing-test"))

        self.assertEqual(self.agents(repo)["lessons"], [lesson["lesson_id"]])
        self.assertIn("\n" + lesson["rule"] + "\n", (repo / "AGENTS.md").read_text())

    def test_block_is_replaced_in_place_and_private_rules_are_refused(self):
        revision = self.author_lesson()
        repo, before, after = self.history()
        self.run_replay(revision, repo, before, after, "--status", "active")
        (repo / "AGENTS.md").write_text("first\n<!-- coldfix:lessons:begin -->\nstale-entry\n<!-- coldfix:lessons:end -->\nsecond\n")
        self.agents(repo)
        text = (repo / "AGENTS.md").read_text()
        self.assertTrue(text.startswith("first\n<!-- coldfix:lessons:begin -->\n"), text)
        self.assertTrue(text.endswith("<!-- coldfix:lessons:end -->\nsecond\n"), text)
        self.assertIn("Require the expected count.", text)
        self.assertNotIn("stale-entry", text)

        denylist = self.cwd / "denylist.txt"
        denylist.write_text("expected count\n")
        blocked = self.run_cli("agents", "--repo", str(repo), "--denylist", str(denylist), code=2)
        self.assertIn("privacy screen blocked", blocked.stderr)
        self.assertNotIn("expected count", blocked.stderr)
        self.assertEqual((repo / "AGENTS.md").read_text(), text)

    def test_malformed_block_and_non_repository_are_left_unchanged(self):
        repo, _, _ = self.history()
        for broken in ("<!-- coldfix:lessons:begin -->\nno end marker\n",
                       "<!-- coldfix:lessons:end -->\nreversed\n<!-- coldfix:lessons:begin -->\n"):
            (repo / "AGENTS.md").write_text(broken)
            failed = self.run_cli("agents", "--repo", str(repo), code=2)
            self.assertIn("malformed", failed.stderr)
            self.assertEqual((repo / "AGENTS.md").read_text(), broken)
        (repo / "AGENTS.md").write_text("valid\n<!-- coldfix:lessons:begin -->\nstale-entry\n<!-- coldfix:lessons:end -->\n")
        (repo / "CLAUDE.md").write_text("<!-- coldfix:lessons:begin -->\nno end marker\n")
        self.run_cli("agents", "--repo", str(repo), code=2)
        self.assertIn("stale-entry", (repo / "AGENTS.md").read_text())
        (repo / "CLAUDE.md").unlink()
        (repo / "AGENTS.md").write_text("alpha<!-- coldfix:lessons:begin -->\nstale-entry\n<!-- coldfix:lessons:end -->\nbeta\n")
        self.agents(repo)
        self.assertEqual((repo / "AGENTS.md").read_text(), "alpha\nbeta\n")
        plain = self.cwd / "plain"
        plain.mkdir()
        self.assertIn("Git checkout", self.run_cli("agents", "--repo", str(plain), code=2).stderr)


if __name__ == "__main__":
    unittest.main()
