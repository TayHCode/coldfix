import fcntl
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

import learn_status

ROOT = Path(__file__).resolve().parents[1]


def lesson(key, status, verification):
    return dict(id=key, lesson_id=key, previous="", created="2026-11-01T00:00:00+00:00", status=status,
                verification=verification)


class LearnStatusTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / ".coldfix"
        self.folder = self.tmp / "lessons"
        self.ledger = self.tmp / "transcripts.sqlite"
        self.root.mkdir(mode=0o700)
        self.folder.mkdir()

    def write(self, name, value):
        (self.root / name).write_text(json.dumps(value))

    def lesson_file(self, name, **meta):
        head = "".join(f"{key}: {value}\n" for key, value in meta.items())
        (self.folder / name).write_text(f"---\n{head}---\nbody\n")

    def test_each_north_star_field_comes_from_its_record(self):
        db = sqlite3.connect(self.ledger)
        db.execute("CREATE TABLE scans (path TEXT PRIMARY KEY, digest TEXT NOT NULL, status TEXT NOT NULL, score REAL)")
        db.executemany("INSERT INTO scans VALUES (?, 'd', ?, NULL)", [("a", "flagged"), ("b", "scored"), ("c", "scored")])
        db.commit()
        db.close()
        self.write("learn-drafts.json", dict(drafts={"d1": {}, "d2": {}}, runs=[
            dict(day="2026-11-11"), dict(day="2026-11-12", error="boom"), dict(day="2026-11-12", skipped="ran")]))
        lead = lambda status, batch="b1": dict(status=status, batch=batch)
        self.write("transcript-lessons.json", dict(
            leads=dict(t1=lead("proven"), t2=lead("no_lesson"), t3=lead("rejected"), t4=lead("needs_approval"),
                       t5=lead("needs_approval"), t6=lead("retry")),
            batches=[dict(errors=0, started="2026-11-11T00:00:00Z"), dict(errors=2, started="2026-11-12T00:00:00Z")]))
        self.write("transcript-deliveries.json", dict(leads=dict(
            t1=dict(status="delivered", approved_repos=["o/approved"]), t5=dict(status="delivered"), t3=dict(status="vetoed", approved_repos=["o/vetoed"]))))
        self.write("check-deliveries.json", dict(
            A=dict(state="delivered", route="bot-pr", repo="o/bot", at="2026-11-10T00:00:00+00:00"),
            B=dict(state="delivered", route="existing-test", repo="o/test", at="2026-11-10T00:00:00+00:00"),
            C=dict(state="waiting", repo="o/waiting")))
        self.lesson_file("L1-a.md", id="L1", status="active")
        self.lesson_file("L2-b.md", id="L2", status="enforced")
        self.lesson_file("L3-c.md", id="L3", status="proposed")
        self.lesson_file("L4-d.md", id="L4", status="retired", check="python3 check.py")
        self.lesson_file("L5-e.md", id="L5", status="retired")
        self.lesson_file("L10-f.md", id="L10", status="conditional")
        self.lesson_file("L11-g.md", id="L11", status="proposed", source="coldfix-transcript:t7")
        store = dict(lessons={row["id"]: row for row in [
            lesson("X", "active", "proven"), lesson("Y", "proposed", "unproven"), lesson("Z", "retired", "proven")]})
        fd = os.open(self.root / ".learn-daily.lock", os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        collect = os.open(self.root / ".lessons.lock", os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, collect)
        fcntl.flock(collect, fcntl.LOCK_EX)
        (self.root / ".check-delivery.lock").touch()

        value = learn_status.summary(self.root, store, self.folder, self.ledger)

        self.assertEqual(value["transcripts_analyzed"], dict(total=3, by_status=dict(flagged=1, scored=2)))
        self.assertEqual(value["candidates"], dict(ci_and_review_drafts=2, transcript_leads=4))
        self.assertEqual(value["verified"], dict(ci_and_review=2, outside_repos=0, own_repo_ci_backlog=0, transcript=1))
        self.assertEqual(value["active"], dict(shared_lessons=["L1", "L2", "L10"], ci_and_review=1, outside_repos=0, own_repo_ci_backlog=0))
        self.assertEqual(value["repos_receiving"], ["o/approved", "o/bot", "o/test"])
        self.assertEqual(value["prevented_or_fixed"], dict(enforced=["L2"], retired_because_fixed=["L4"],
                                                           checks_added_by_coldfix=1, already_enforced_by_fix=1))
        self.assertEqual(value["running"], ["daily or backlog", "lesson store write"])
        self.assertEqual(value["failing"], ["daily run 2026-11-12", "transcript batch 2026-11-12T00:00:00Z"])
        self.assertEqual(value["awaiting_approval"], ["t4", "L11"])

        self.write("transcript-lessons.json", dict(leads={}, batches=[dict(started="2026-11-12T01:00:00Z", picked=0, calls=0)]))
        self.assertEqual(learn_status.summary(self.root, store, self.folder, self.ledger)["failing"], ["daily run 2026-11-12"])

    def test_the_command_answers_an_empty_store_with_zeros(self):
        result = subprocess.run([sys.executable, str(ROOT / "learn_status.py"), "--store", str(self.root),
                                 "--lessons", str(self.folder), "--scan-ledger", str(self.ledger)],
                                cwd=ROOT, text=True, capture_output=True, check=True, timeout=60)
        self.assertEqual(json.loads(result.stdout), dict(
            transcripts_analyzed=dict(total=0, by_status={}),
            candidates=dict(ci_and_review_drafts=0, transcript_leads=0),
            verified=dict(ci_and_review=0, outside_repos=0, own_repo_ci_backlog=0, transcript=0),
            active=dict(shared_lessons=[], ci_and_review=0, outside_repos=0, own_repo_ci_backlog=0), repos_receiving=[],
            prevented_or_fixed=dict(enforced=[], retired_because_fixed=[], checks_added_by_coldfix=0,
                                    already_enforced_by_fix=0),
            running=[], failing=[], awaiting_approval=[],
            lessons=dict(found=0, proven=0, rejected={}, waiting=0, duplicate=0, retired=0, withdrawn=0), projects={}))
        self.assertFalse(self.ledger.exists())

    def test_each_lesson_lands_in_one_state_and_the_page_shows_the_counts(self):
        self.write("learn-drafts.json", dict(runs=[], drafts=dict(
            d1={}, d2=dict(replay=dict(status="proven")),
            d3=dict(replay=dict(status="rejected", reason="0431ee5d400c4a8291b8bee7622e331e: invalid source repository")),
            d4=dict(replay=dict(status="rejected", reason="no exact pair: REVIEWED_COMMIT_NOT_IN_PR")),
            d5=dict(replay=dict(status="rejected", reason="incident not admitted; flagged lines reverted PASS")),
            d6=dict(replay=dict(status="rejected", reason="cannot read secret-path/private-repo")),
            d7=dict(verdict="FAIL", reason="the rule is too vague"), d8=dict(verdict="FAIL", reason="duplicate of abc"),
            d9=dict(verdict="PASS"))))
        self.write("transcript-lessons.json", dict(batches=[], leads=dict(
            t1=dict(status="proven"), t2=dict(status="no_lesson"), t3=dict(status="rejected", reason="jev check failed: haiku rejected"),
            t4=dict(status="unprovable", reason="lead too long to read whole"), t5=dict(status="needs_approval", batch="b1"),
            t6=dict(status="retry"), t7=dict(status="duplicate", reason="transcript:t1"))))
        self.write("transcript-deliveries.json", dict(
            leads=dict(t1=dict(status="delivered"), t8=dict(status="vetoed")),
            proven=dict(P1=dict(status="delivered", lesson="L1"), P2=dict(status="withdrawn", lesson="L9"),
                        P3=dict(status="vetoed", lesson="L5"))))
        self.lesson_file("L1-a.md", id="L1", status="active", repos="o/a, o/<b>")
        self.lesson_file("L6-f.md", id="L6", status="conditional", repos="O/A")
        self.lesson_file("L2-b.md", id="L2", status="enforced", repos="o/a")
        self.lesson_file("L3-c.md", id="L3", status="proposed", repos="o/proposed")
        self.lesson_file("L4-d.md", id="L4", status="retired", check="python3 check.py", repos="o/retired")
        self.lesson_file("L5-e.md", id="L5", status="retired", retired_by="recheck", repos="o/withdrawn")
        self.lesson_file("L7-g.md", id="L7", status="superseded", repos="o/superseded")
        page = self.tmp / "out" / "status.html"
        oss = self.tmp / "oss"
        (oss / "lessons").mkdir(parents=True)
        (oss / "learn-drafts.json").write_text(json.dumps(dict(runs=[], drafts=dict(o1=dict(replay=dict(status="proven"))))))

        result = subprocess.run([sys.executable, str(ROOT / "learn_status.py"), "--store", str(self.root),
                                 "--lessons", str(self.folder), "--scan-ledger", str(self.ledger), "--page", str(page), "--oss-store", str(oss)],
                                cwd=ROOT, text=True, capture_output=True, timeout=60)

        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["lessons"], dict(
            found=16, proven=3, waiting=4, duplicate=2, retired=2, withdrawn=3,
            rejected={"invalid source repository": 1, "no exact pair": 1, "incident not admitted": 1, "other": 1,
                      "failed draft review": 1, "jev check failed": 1, "lead too long to read whole": 1}))
        self.assertEqual(value["projects"], {"o/<b>": 1, "o/a": 3})
        text = page.read_text()
        for row in ("<th scope=\"row\">Found</th><td>16</td>", "<th scope=\"row\">Proven</th><td>3</td>",
                    "<th scope=\"row\">Rejected</th><td>7</td>", "<th scope=\"row\">Waiting</th><td>4</td>",
                    "<th scope=\"row\">Retired</th><td>2</td>", "<th scope=\"row\">Withdrawn</th><td>3</td>",
                    "<th scope=\"row\">no exact pair</th><td>1</td>", "<th scope=\"row\">o/a</th><td>3</td>",
                    "<th scope=\"row\">o/&lt;b&gt;</th><td>1</td>"):
            self.assertIn(row, text)
        for private in ("REVIEWED_COMMIT_NOT_IN_PR", "secret-path", "too vague"):
            self.assertNotIn(private, text)
        self.assertNotIn("o/<b>", text)

    def test_outside_repo_lessons_from_the_oss_store_get_their_own_line(self):
        own = dict(lessons={row["id"]: row for row in [lesson("X", "active", "proven")]})
        oss = dict(lessons={row["id"]: row for row in [
            lesson("O1", "active", "proven"), lesson("O2", "active", "proven"), lesson("O3", "proposed", "unproven")]})
        value = learn_status.summary(self.root, own, self.folder, self.ledger, oss)
        self.assertEqual(value["verified"], dict(ci_and_review=1, outside_repos=2, own_repo_ci_backlog=0, transcript=0))
        self.assertEqual(value["active"], dict(shared_lessons=[], ci_and_review=1, outside_repos=2, own_repo_ci_backlog=0))

    def test_proven_ci_lessons_in_the_own_repo_backlog_store_are_counted(self):
        own = dict(lessons={row["id"]: row for row in [lesson("X", "active", "proven")]})
        backlog = dict(lessons={row["id"]: row for row in [
            lesson("B1", "active", "proven"), lesson("B2", "active", "proven"), lesson("B3", "proposed", "fail_before")]})
        value = learn_status.summary(self.root, own, self.folder, self.ledger, None, backlog)
        self.assertEqual(value["verified"], dict(ci_and_review=1, outside_repos=0, own_repo_ci_backlog=2, transcript=0))
        self.assertEqual(value["active"], dict(shared_lessons=[], ci_and_review=1, outside_repos=0, own_repo_ci_backlog=2))

    def test_the_command_reads_the_backlog_store_without_writing_it(self):
        backlog = self.tmp / "backlog"
        (backlog / "lessons").mkdir(parents=True)
        before = sorted(p.relative_to(backlog) for p in backlog.rglob("*"))
        result = subprocess.run([sys.executable, str(ROOT / "learn_status.py"), "--store", str(self.root),
                                 "--lessons", str(self.folder), "--scan-ledger", str(self.ledger), "--backlog-store", str(backlog)],
                                cwd=ROOT, text=True, capture_output=True, check=True, timeout=60)
        self.assertEqual(json.loads(result.stdout)["verified"]["own_repo_ci_backlog"], 0)
        self.assertEqual(sorted(p.relative_to(backlog) for p in backlog.rglob("*")), before)
        missing = subprocess.run([sys.executable, str(ROOT / "learn_status.py"), "--store", str(self.root),
                                  "--lessons", str(self.folder), "--scan-ledger", str(self.ledger),
                                  "--backlog-store", str(self.tmp / "absent")], cwd=ROOT, text=True, capture_output=True, timeout=60)
        self.assertEqual(missing.returncode, 2)
        self.assertIn("backlog store not found", missing.stderr)

    def test_the_command_reads_the_oss_store_without_writing_it(self):
        oss = self.tmp / "oss"
        (oss / "lessons").mkdir(parents=True)
        before = sorted(p.relative_to(oss) for p in oss.rglob("*"))
        result = subprocess.run([sys.executable, str(ROOT / "learn_status.py"), "--store", str(self.root),
                                 "--lessons", str(self.folder), "--scan-ledger", str(self.ledger), "--oss-store", str(oss)],
                                cwd=ROOT, text=True, capture_output=True, check=True, timeout=60)
        self.assertEqual(json.loads(result.stdout)["active"], dict(shared_lessons=[], ci_and_review=0, outside_repos=0, own_repo_ci_backlog=0))
        self.assertEqual(sorted(p.relative_to(oss) for p in oss.rglob("*")), before)
        missing = subprocess.run([sys.executable, str(ROOT / "learn_status.py"), "--store", str(self.root),
                                  "--lessons", str(self.folder), "--scan-ledger", str(self.ledger),
                                  "--oss-store", str(self.tmp / "absent")], cwd=ROOT, text=True, capture_output=True, timeout=60)
        self.assertEqual(missing.returncode, 2)
        self.assertIn("OSS store not found", missing.stderr)

    def test_a_missing_lessons_folder_is_an_error(self):
        result = subprocess.run([sys.executable, str(ROOT / "learn_status.py"), "--store", str(self.root),
                                 "--lessons", str(self.tmp / "missing"), "--scan-ledger", str(self.ledger)],
                                cwd=ROOT, text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 2)
        self.assertIn("lessons folder not found", result.stderr)


if __name__ == "__main__":
    unittest.main()
