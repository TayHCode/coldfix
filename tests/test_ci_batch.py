from datetime import datetime, timedelta, timezone
import json
import fcntl
import io
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
from unittest import mock
from urllib.parse import parse_qs, urlsplit
import unittest

import ci_batch

NOW = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)


def run(number, sha, created, branch="feature", workflow=7):
    return dict(id=number, run_attempt=1, created_at=created, head_sha=sha * 40, head_branch=branch, workflow_id=workflow)


class FakeGitHub:
    def __init__(self, failed, green, compare=None):
        self.failed, self.green, self.calls = failed, green, []
        self.compare = compare or {}

    def __call__(self, endpoint, missing=None):
        self.calls.append(endpoint)
        url = urlsplit(endpoint)
        query = {key: values[0] for key, values in parse_qs(url.query).items()}
        repo = "/".join(url.path.split("/")[1:3])
        if "/compare/" in url.path:
            return self.compare.get(url.path.split("/")[-1], dict(status="ahead", files=[dict(filename="pkg/core.py")]))
        if "/workflows/" in url.path:
            low, high = query["created"].split("..")
            rows = [row for row in self.green.get(repo, []) if row["head_branch"] == query["branch"]
                    and str(row["workflow_id"]) == url.path.split("/")[5] and low <= row["created_at"] <= high]
            rows.sort(key=lambda row: row["created_at"], reverse=True)
            page = int(query.get("page", 1))
            return dict(total_count=len(rows), workflow_runs=rows[(page - 1) * 100:page * 100])
        if "page" not in query:
            number = int(url.path.split("/")[-1])
            return next(row for row in self.failed.get(repo, []) if row["id"] == number)
        page = int(query["page"])
        return dict(workflow_runs=self.failed.get(repo, [])[(page - 1) * 100:page * 100])


class SelectTests(unittest.TestCase):
    def test_pairs_each_failure_with_the_first_later_green_on_its_branch_and_workflow(self):
        github = FakeGitHub(
            failed={"o/a": [run(1, "a", "2026-01-01T00:00:00Z"), run(2, "b", "2026-01-02T00:00:00Z", branch="other")]},
            green={"o/a": [run(10, "a", "2026-01-01T01:00:00Z"),
                           run(11, "c", "2026-01-01T03:00:00Z", workflow=8),
                           run(12, "d", "2026-01-01T05:00:00Z"),
                           run(13, "e", "2026-01-01T09:00:00Z"),
                           run(14, "f", "2026-01-20T00:00:00Z", branch="other")]})
        ledger = dict(seen={}, pages={}, batches=[])
        picked = ci_batch.select(ledger, ["o/a"], 10, 1, github)[0]
        self.assertEqual([row["pair"] for row in picked],
                         [dict(repo="o/a", before="a" * 40, after="d" * 40, red_run=1, green_run=12)])
        self.assertEqual(ledger["seen"]["2"], dict(batch=1, pair=None))

    def test_later_batches_skip_seen_failures_and_page_further_back(self):
        failed = [run(index, "a", f"2026-01-01T00:{59 - index // 4:02d}:00Z") for index in range(150)]
        github = FakeGitHub(failed={"o/a": failed}, green={"o/a": [run(999, "b", "2026-01-02T00:00:00Z")]})
        ledger = dict(seen={}, pages={}, batches=[])
        first = ci_batch.select(ledger, ["o/a"], 100, 1, github)[0]
        second = ci_batch.select(ledger, ["o/a"], 100, 2, github)[0]
        self.assertEqual(len(first), 100)
        self.assertEqual(len(second), 50)
        self.assertFalse({row["id"] for row in first} & {row["id"] for row in second})
        self.assertEqual(ledger["pages"]["o/a"], dict(page=2, done=True))

    def test_batches_stop_after_the_failed_run_limit_even_without_green_matches(self):
        failed = [run(index, "a", f"2026-01-01T{23 - index // 60:02d}:{59 - index % 60:02d}:00Z", branch="dead")
                  for index in range(250)] + [run(900, "b", "2026-01-01T00:00:00Z")]
        github = FakeGitHub(failed={"o/a": failed}, green={"o/a": [run(999, "c", "2026-01-01T05:00:00Z")]})
        ledger = dict(seen={}, pages={}, batches=[])
        for batch in range(1, 27):
            before = len(github.calls)
            picked = ci_batch.select(ledger, ["o/a"], 10, batch, github)[0]
            self.assertEqual([row["id"] for row in picked], [900] if batch == 26 else [])
            self.assertLessEqual(sum(row["batch"] == batch for row in ledger["seen"].values()), 10)
            self.assertLessEqual(sum("/workflows/" in call for call in github.calls[before:]), 10)
        self.assertEqual(len(ledger["seen"]), 251)

    def test_unseen_older_history_is_examined_before_recent_unmatched_retries(self):
        recent = [run(number, "a", f"2026-01-06T00:0{number}:00Z") for number in range(4)]
        old = run(9, "b", "2026-01-01T00:00:00Z")
        github = FakeGitHub(failed={"o/a": recent + [old]},
                            green={"o/a": [run(99, "c", "2026-01-02T00:00:00Z")]})
        ledger = dict(seen={}, pages={}, batches=[])
        outcomes = []
        for batch in range(1, 4):
            picked = ci_batch.select(ledger, ["o/a"], 2, batch, github,
                                      now=ci_batch.c.time_key("2026-01-07T00:00:00Z"))[0]
            outcomes.append([row["id"] for row in picked])
        self.assertEqual(outcomes, [[], [], [9]])
        self.assertEqual(sum(row.get("pending", False) for row in ledger["seen"].values()), 4)

    def test_pending_retries_rotate_after_unseen_history_is_exhausted(self):
        recent = [run(number, "a", f"2026-01-06T00:0{number}:00Z") for number in range(4)]
        github = FakeGitHub(failed={"o/a": recent}, green={})
        ledger = dict(seen={}, pages={}, batches=[])
        for batch in range(1, 5):
            picked, considered = ci_batch.select(ledger, ["o/a"], 2, batch, github,
                                                 now=ci_batch.c.time_key("2026-01-07T00:00:00Z"))
            self.assertEqual((picked, considered), ([], 2))
        self.assertEqual(sorted(row["batch"] for row in ledger["seen"].values()), [3, 3, 4, 4])

    def test_a_recent_failure_with_no_green_yet_is_considered_again_by_later_batches(self):
        github = FakeGitHub(failed={"o/a": [run(1, "a", "2026-01-01T00:00:00Z")]}, green={"o/a": []})
        ledger = dict(seen={}, pages={}, batches=[])
        now = ci_batch.c.time_key("2026-01-02T00:00:00Z")
        self.assertEqual(ci_batch.select(ledger, ["o/a"], 10, 1, github, now=now)[0], [])
        self.assertTrue(ledger["seen"]["1"]["pending"])
        github.green["o/a"].append(run(9, "c", "2026-01-03T00:00:00Z"))
        later = ci_batch.c.time_key("2026-01-09T00:00:00Z")
        self.assertEqual([row["id"] for row in ci_batch.select(ledger, ["o/a"], 10, 2, github, now=later)[0]], [1])
        self.assertEqual(ci_batch.select(ledger, ["o/a"], 10, 3, github, now=later)[0], [])

    def test_failures_created_after_history_was_fully_listed_are_still_found(self):
        github = FakeGitHub(failed={"o/a": [run(1, "a", "2026-01-01T00:00:00Z")]},
                            green={"o/a": [run(9, "c", "2026-01-01T05:00:00Z"), run(19, "e", "2026-01-03T05:00:00Z")]})
        ledger = dict(seen={}, pages={}, batches=[])
        self.assertEqual([row["id"] for row in ci_batch.select(ledger, ["o/a"], 10, 1, github)[0]], [1])
        self.assertEqual(ci_batch.select(ledger, ["o/a"], 10, 2, github)[0], [])
        github.failed["o/a"].insert(0, run(2, "d", "2026-01-03T00:00:00Z"))
        self.assertEqual([row["id"] for row in ci_batch.select(ledger, ["o/a"], 10, 3, github)[0]], [2])
        self.assertEqual(sorted(row["id"] for row in ledger["listed"]["o/a"]), [1, 2])

    def test_only_a_proof_from_the_batch_failures_counts_for_that_batch(self):
        store = dict(failures={"f-old": dict(source_ref="run/1"), "f-new": dict(source_ref="run/2")},
                     lessons=dict(old=dict(evidence=["f-old", "f-new"], admission=dict(source_ref="run/1")),
                                  new=dict(evidence=["f-new"], admission=dict(source_ref="run/2"))))
        old = dict(replay=dict(status="proven", proof=dict(id="old")))
        new = dict(replay=dict(status="delivered", proof=dict(id="new")))
        self.assertFalse(ci_batch.proved_here(store, old, {"f-new"}))
        self.assertTrue(ci_batch.proved_here(store, new, {"f-new"}))
        self.assertFalse(ci_batch.proved_here(store, dict(replay=dict(status="rejected")), {"f-new"}))

    def test_reads_the_earliest_green_from_the_last_page_of_a_busy_branch(self):
        green = [run(100 + index, "b", f"2026-01-01T{1 + index // 60:02d}:{index % 60:02d}:00Z") for index in range(150)]
        github = FakeGitHub(failed={}, green={"o/a": green})
        found = ci_batch.later_green("o/a", run(1, "a", "2026-01-01T00:00:00Z"), github)
        self.assertEqual(found["id"], 100)


    def test_skips_greens_at_the_failed_commit_on_later_pages_of_a_busy_branch(self):
        green = [run(100 + index, "a" if index >= 100 else "b", f"2026-01-01T{1 + index // 60:02d}:{index % 60:02d}:00Z")
                 for index in range(150)]
        github = FakeGitHub(failed={}, green={"o/a": green})
        found = ci_batch.later_green("o/a", run(1, "a", "2026-01-01T00:00:00Z"), github)
        self.assertEqual(found["id"], 100)

def finished(batch, proven=0):
    return dict(batch=batch, ids=["f%d" % batch], learned=True, ingested=1, fix_pairs=1, drafted=1, draft_pass=1,
                proven=proven, rejected=1 - proven, reject_reasons={} if proven else {"red PASS": 1})


class RunTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def ledger(self, **values):
        (self.root / ci_batch.LEDGER).write_text(json.dumps(dict(dict(seen={}, pages={}, batches=[]), **values)))

    def saved(self):
        return json.loads((self.root / ci_batch.LEDGER).read_text())

    def run_batch(self, outcome, **options):
        def learned(root, entry, *rest):
            entry.update(finished(entry["batch"], outcome.pop(0)))
        fresh = lambda root, ledger, *rest: dict(batch=len(ledger["batches"]) + 1, ids=[], fix_pairs=1, ingested=1, learned=False)
        with mock.patch.object(ci_batch, "ingest", side_effect=fresh), mock.patch.object(ci_batch, "learn", learned):
            return ci_batch.run(self.root, "o", 100, **options)

    def test_an_unfinished_batch_is_learned_again_without_selecting_new_failures(self):
        unfinished = dict(batch=1, ids=["f1"], learned=False)
        self.ledger(batches=[unfinished])
        learned = lambda root, entry, *rest: entry.update(finished(1, proven=1))
        with mock.patch.object(ci_batch, "ingest") as ingest, mock.patch.object(ci_batch, "learn", learned):
            lines = ci_batch.run(self.root, "o", 100)
        ingest.assert_not_called()
        self.assertEqual(lines, ['ci-backlog batch 1: considered 1 failures, imported 1 of 1 fix pairs, screened 0 {}, drafted 1 (review PASS 1), proven 1, '
                                 'rejected 0, undrafted 0, reasons {}'])
        self.assertEqual(self.saved()["batches"], [finished(1, proven=1)])

    def test_an_exhausted_backlog_records_no_batch_and_makes_no_model_call(self):
        dead = [run(number, "a", f"2026-01-01T00:0{number}:00Z", branch="dead") for number in range(4)]
        screened = [run(number, "b", f"2026-01-01T00:0{number}:00Z") for number in (4, 5)]
        github = FakeGitHub(failed={"o/a": dead + screened}, green={"o/a": [run(9, "c", "2026-01-01T05:00:00Z")]})

        def fetch(repo, number, attempt):
            row = dict(CI_RUN, repo="o/a", run_id=number, head_sha="b" * 40, log_tail="error: build broke")
            return ci_batch.failure_ingest.record(ci_batch.failure_ingest.ci_event(row))

        root = self.root / ".coldfix"
        root.mkdir(mode=0o700)
        asked = []
        with mock.patch.object(ci_batch.failure_ingest, "gh", return_value=[dict(nameWithOwner="o/a")]):
            lines = [line for _ in range(4)
                     for line in ci_batch.run(root, "o", 2, api=github, fetch=fetch, ask=lambda *call: asked.append(call))]
        self.assertEqual(lines, ['ci-backlog: no new fix pairs to learn from; considered 2 failures, 2 fix pairs, '
                                 'screened {"no recorded failing command": 2}']
                         + ["ci-backlog: no new fix pairs to learn from; considered 2 failures, 0 fix pairs, screened {}"] * 2
                         + ["ci-backlog: no new fix pairs left in GitHub CI history"])
        self.assertEqual(asked, [])
        saved = json.loads((root / ci_batch.LEDGER).read_text())
        self.assertEqual(saved["batches"], [])
        self.assertEqual(len(saved["seen"]), 6)
        self.assertNotIn("paused", saved)

    def test_a_failed_learn_keeps_the_ingested_batch_for_the_next_run(self):
        fresh = dict(batch=1, ids=["f1"], fix_pairs=1, ingested=1, learned=False)
        boom = mock.Mock(side_effect=ci_batch.c.ColdfixError("drafting failed"))
        with mock.patch.object(ci_batch, "ingest", return_value=fresh), mock.patch.object(ci_batch, "learn", boom):
            with self.assertRaises(ci_batch.c.ColdfixError):
                ci_batch.run(self.root, "o", 100)
        self.assertEqual(self.saved()["batches"], [fresh])

    def test_replay_clones_left_by_a_killed_run_are_removed_before_the_next_batch(self):
        for name in (".backlog-sources-abc/projects", ".replay-xyz/repo"):
            (self.root / name).mkdir(parents=True)
        (self.root / "failures").mkdir()
        self.run_batch([0])
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["backlog-batches.json", "failures"])

    def test_zero_proof_batches_keep_running_and_report_the_streak(self):
        self.ledger(batches=[finished(1, proven=1), finished(2)])
        self.assertEqual(len(self.run_batch([0])), 1)
        lines = self.run_batch([0])
        self.assertEqual(lines[-1], "ci-backlog: 3 batches in a row proved 0 (batches 2-4); still running")
        lines = self.run_batch([0])
        self.assertEqual(lines[-1], "ci-backlog: 4 batches in a row proved 0 (batches 2-5); still running")
        self.assertNotIn("paused", self.saved())
        self.assertEqual(len(self.saved()["batches"]), 5)
        self.ledger(batches=self.saved()["batches"], paused="runner offline")
        with mock.patch.object(ci_batch, "ingest") as ingest, mock.patch.object(ci_batch, "learn") as learn:
            self.assertEqual(ci_batch.run(self.root, "o", 100, now=NOW - timedelta(hours=12)),
                             ["ci-backlog paused until 2026-11-08T12:00:00+00:00: runner offline"])
        ingest.assert_not_called()
        learn.assert_not_called()

    def test_a_paused_ledger_gets_a_resume_time_and_runs_again_after_it(self):
        self.ledger(batches=[finished(1)], paused="3 batches in a row proved 0 (batches 2, 3, 4)")
        with mock.patch.object(ci_batch, "ingest") as ingest:
            lines = ci_batch.run(self.root, "o", 100, now=NOW - timedelta(hours=12))
            self.assertEqual(ci_batch.run(self.root, "o", 100, now=NOW - timedelta(minutes=1)), lines)
        ingest.assert_not_called()
        self.assertEqual(lines, ["ci-backlog paused until 2026-11-08T12:00:00+00:00: 3 batches in a row proved 0 (batches 2, 3, 4)"])
        self.assertEqual(self.saved()["resume_after"], "2026-11-08T12:00:00+00:00")
        fresh = lambda root, ledger, *rest: dict(batch=2, ids=[], fix_pairs=1, ingested=1, learned=False)
        learned = lambda root, entry, *rest: entry.update(finished(2, proven=1))
        with mock.patch.object(ci_batch, "ingest", side_effect=fresh), mock.patch.object(ci_batch, "learn", learned):
            lines = ci_batch.run(self.root, "o", 100, now=NOW)
        self.assertTrue(lines[0].startswith("ci-backlog batch 2:"), lines)
        self.assertNotIn("paused", self.saved())
        self.assertNotIn("resume_after", self.saved())

    def test_a_resume_time_left_after_the_pause_was_cleared_by_hand_does_not_shorten_the_next_hold(self):
        self.ledger(batches=[finished(1)], resume_after="2026-11-01T00:00:00+00:00")
        boom = mock.Mock(side_effect=ci_batch.c.ColdfixError("GitHub unavailable"))
        with mock.patch.object(ci_batch, "ingest", boom), self.assertRaises(ci_batch.c.ColdfixError):
            ci_batch.run(self.root, "o", 100)
        self.assertNotIn("resume_after", self.saved())
        self.ledger(batches=self.saved()["batches"], paused="runner offline")
        with mock.patch.object(ci_batch, "ingest") as ingest:
            lines = ci_batch.run(self.root, "o", 100, now=NOW - timedelta(hours=12))
        ingest.assert_not_called()
        self.assertEqual(lines, ["ci-backlog paused until 2026-11-08T12:00:00+00:00: runner offline"])

    def test_a_proven_batch_resets_the_pause_count(self):
        self.ledger(batches=[finished(1), finished(2)])
        self.assertEqual(len(self.run_batch([1])), 1)
        self.assertEqual(len(self.run_batch([0])), 1)
        self.assertNotIn("paused", self.saved())


    def test_oss_pauses_on_the_tenth_consecutive_zero_proof_batch(self):
        self.ledger(batches=[finished(1), finished(2, proven=1), *map(finished, range(3, 11))])
        self.run_batch([0], public=["o/public"])
        self.assertNotIn("reason", self.saved()["oss_pause"])
        reason = "10 consecutive batches proved 0 lessons (batches 3-12)"
        self.assertEqual(self.run_batch([0], public=["o/public"])[-1], f"ci-backlog paused: {reason}")
        self.assertEqual(self.saved()["oss_pause"]["reason"], reason)
        with mock.patch.object(ci_batch, "ingest") as ingest:
            self.assertEqual(ci_batch.run(self.root, None, 100, public=["o/public"]), [f"ci-backlog paused: {reason}"])
        ingest.assert_not_called()

    def test_a_refreshed_oss_repo_list_starts_a_new_streak(self):
        self.ledger(batches=list(map(finished, range(1, 11))))
        self.assertIn("ci-backlog paused:", ci_batch.run(self.root, None, 100, public=["o/public"])[0])
        lines = self.run_batch([0], public=["o/public", "o/new"])
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("ci-backlog batch 11:"))
        self.assertNotIn("reason", self.saved()["oss_pause"])
        self.assertEqual(len(self.saved()["batches"]), 11)

    def test_oss_pauses_an_existing_zero_proof_streak_before_ingesting(self):
        self.ledger(batches=list(map(finished, range(1, 11))))
        with mock.patch.object(ci_batch, "ingest") as ingest:
            self.assertEqual(ci_batch.run(self.root, None, 100, public=["o/public"]),
                             ["ci-backlog paused: 10 consecutive batches proved 0 lessons (batches 1-10)"])
        ingest.assert_not_called()


class DedupeTests(unittest.TestCase):
    def test_runs_the_live_collector_already_holds_are_never_selected(self):
        failed = [run(1, "a", "2026-01-01T00:00:00Z"), run(2, "b", "2026-01-01T00:10:00Z")]
        github = FakeGitHub(failed={"o/a": failed}, green={"o/a": [run(9, "c", "2026-01-01T05:00:00Z")]})
        ledger = dict(seen={}, pages={}, batches=[])
        picked = ci_batch.select(ledger, ["o/a"], 10, 1, github, known=frozenset({"2"}))[0]
        self.assertEqual([row["id"] for row in picked], [1])
        self.assertEqual(ci_batch.select(ledger, ["o/a"], 10, 2, github)[0], [])

    def test_a_run_listed_twice_after_history_shifts_is_selected_once(self):
        red = run(1, "a", "2026-01-01T00:00:00Z")
        github = FakeGitHub(failed={"o/a": [red]}, green={"o/a": [run(9, "c", "2026-01-01T05:00:00Z")]})
        listed = dict(red, attempt=1)
        listed.pop("run_attempt")
        ledger = dict(seen={}, pages={"o/a": dict(page=1, done=True)}, batches=[], listed={"o/a": [listed, listed]})
        self.assertEqual([row["id"] for row in ci_batch.select(ledger, ["o/a"], 10, 1, github)[0]], [1])


class LearnTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def learn(self, replies):
        def drafting(root, day, ask, reserved_rules=None):
            path = root / ci_batch.learn_daily.LEDGER
            ledger = json.loads(path.read_text()) if path.exists() else dict(drafts={}, runs=[])
            drafted, left = replies.pop(0)
            ledger["drafts"].update({f"{day}-{index}": {} for index in range(drafted)})
            ledger["runs"].append(dict(day=day))
            path.write_text(json.dumps(ledger))
            return dict(day=day, candidates=left)
        entry = dict(batch=1, ids=[])
        with mock.patch.object(ci_batch.learn_daily, "run", side_effect=drafting), \
                mock.patch.object(ci_batch.learn_replay, "run_in_workspace", return_value={}), \
                mock.patch.object(ci_batch, "measure", return_value={}), \
                mock.patch.object(ci_batch, "merge_evidence"):
            ci_batch.learn(self.root, entry, "o", None, 1)
        return entry

    def test_an_empty_model_reply_is_retried_before_the_batch_is_marked_learned(self):
        entry = self.learn([(0, 3), (2, 3), (0, 0)])
        self.assertEqual(entry["undrafted"], 0)

    def test_candidates_still_undrafted_after_three_empty_replies_are_reported(self):
        entry = self.learn([(1, 3), (0, 2), (0, 2), (0, 2)])
        self.assertEqual(entry["undrafted"], 2)
        self.assertIn("undrafted 2", ci_batch.journal(dict(entry, ingested=0, fix_pairs=0, drafted=1, draft_pass=0,
                                                              proven=0, rejected=0, reject_reasons={})))

    def test_new_failures_join_an_unproven_draft_and_reopen_it_but_never_credit_an_old_proof(self):
        drafts = dict(rejected=dict(evidence=["old"], replay=dict(status="rejected")),
                      proven=dict(evidence=["p"], replay=dict(status="proven")))
        (self.root / ci_batch.learn_daily.LEDGER).write_text(json.dumps(dict(drafts=drafts, runs=[])))
        groups = [dict(fingerprint="rejected", evidence=["new", "old", "other"]),
                  dict(fingerprint="proven", evidence=["new2", "p"])]
        with mock.patch.object(ci_batch.learn_candidates, "candidates", return_value=groups), \
                mock.patch.object(ci_batch.c, "load_store", return_value=(None, {})):
            ci_batch.merge_evidence(self.root, {"new", "new2"})
        saved = json.loads((self.root / ci_batch.learn_daily.LEDGER).read_text())["drafts"]
        self.assertEqual(saved["rejected"], dict(evidence=["old", "new"]))
        self.assertEqual(saved["proven"], dict(evidence=["p"], replay=dict(status="proven")))

    def test_drafting_refuses_a_model_that_is_not_a_bulk_model(self):
        ask = mock.Mock(return_value="reply")
        guarded = ci_batch.cheap_only(ask)
        with mock.patch.object(ci_batch.learn_daily, "cheap_models", return_value={"claude-haiku-5-5", "claude-opus-5-5"}):
            self.assertEqual(guarded(dict(model="claude-haiku-5-5"), "prompt", ci_batch.learn_daily.DRAFTS, self.root), "reply")
            with self.assertRaises(ci_batch.c.ColdfixError):
                guarded(dict(model="claude-opus-5-5"), "prompt", ci_batch.learn_daily.DRAFTS, self.root)
        ask.assert_called_once_with(dict(model="claude-haiku-5-5"), "prompt", ci_batch.learn_daily.DRAFTS, self.root)


    def test_fallback_drafting_on_luna_cannot_be_reviewed_by_luna(self):
        ask = mock.Mock(side_effect=[ci_batch.c.ColdfixError("Claude unavailable"), "drafted"])
        guarded = ci_batch.cheap_only(ask)
        with mock.patch.object(ci_batch.learn_daily, "cheap_models", return_value={"claude-haiku-5-5", "gpt-6-luna"}):
            with self.assertRaises(ci_batch.c.ColdfixError):
                guarded(dict(model="claude-haiku-5-5"), "prompt", ci_batch.learn_daily.DRAFTS, self.root)
            self.assertEqual(guarded(dict(model="gpt-6-luna"), "prompt", ci_batch.learn_daily.DRAFTS, self.root), "drafted")
            with self.assertRaisesRegex(ci_batch.c.ColdfixError, "reviewer must differ"):
                guarded(dict(model="gpt-6-luna"), "prompt", ci_batch.learn_daily.REVIEWS, self.root)
        self.assertEqual(ask.call_count, 2)


class SharedReservationTests(unittest.TestCase):
    def test_both_entrypoints_read_the_peer_under_the_same_live_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            live, backlog = Path(directory) / "live", Path(directory) / "backlog"
            live.mkdir()
            backlog.mkdir()
            for root, rule in ((live, "Live rule."), (backlog, "Backlog rule.")):
                (root / ci_batch.learn_daily.LEDGER).write_text(json.dumps(dict(
                    drafts={"passed": dict(verdict="PASS", draft=dict(rule=rule))}, runs=[])))
            observed = []
            original = ci_batch.learn_daily.peer_rules
            def peer_rules(path):
                with (live / ".learn-daily.lock").open("r+") as competing:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(competing, fcntl.LOCK_EX | fcntl.LOCK_NB)
                rules = original(path)
                observed.append(rules)
                return rules
            with mock.patch.object(ci_batch.learn_daily, "peer_rules", side_effect=peer_rules), \
                    mock.patch.object(ci_batch, "run", return_value=[]) as batch:
                self.assertEqual(ci_batch.main(["--store", str(backlog), "--skip-store", str(live),
                                               "--owner", "o"]), 0)
                self.assertEqual(batch.call_args.kwargs["reserved_rules"], {"live rule.": "draft passed"})
            def daily(root, day, **kwargs):
                self.assertEqual(kwargs["reserved_rules"], {"backlog rule.": "draft passed"})
                summary = dict(day=day)
                (root / ci_batch.learn_daily.LEDGER).write_text(json.dumps(dict(drafts={}, runs=[summary])))
                return summary
            with mock.patch.object(ci_batch.learn_daily, "peer_rules", side_effect=peer_rules), \
                    mock.patch.object(ci_batch.learn_daily, "run", side_effect=daily), \
                    mock.patch.object(ci_batch.learn_daily.c, "on_replay_host", return_value=False), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(ci_batch.learn_daily.main(["--store", str(live), "--peer-store", str(backlog)]), 0)
            self.assertEqual(observed, [{"live rule.": "draft passed"}, {"backlog rule.": "draft passed"}])


class FetchRetryTests(unittest.TestCase):
    def test_a_run_rerun_after_listing_is_fetched_at_its_current_attempt(self):
        red = run(1, "a", "2026-01-01T00:00:00Z")
        github = FakeGitHub(failed={"o/a": [red]}, green={"o/a": [run(9, "c", "2026-01-01T05:00:00Z")]})
        ledger = dict(seen={}, pages={}, batches=[])
        ci_batch.pool(ledger, ["o/a"], github)
        red["run_attempt"] = 2
        fetched = []
        def fetch(repo, number, attempt):
            fetched.append(attempt)
            return dict(id="f1")
        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.object(ci_batch.failure_ingest, "gh", return_value=[dict(nameWithOwner="o/a")]), \
                mock.patch.object(ci_batch.failure_ingest, "import_records"):
            ci_batch.ingest(Path(temporary), ledger, "o", 10, github, fetch)
        self.assertEqual(fetched, [2])


    def test_a_run_whose_log_cannot_be_fetched_is_retried_up_to_three_times(self):
        failed = [run(number, "a", f"2026-01-01T00:{59 - number:02d}:00Z") for number in range(1, 6)]
        github = FakeGitHub(failed={"o/a": failed}, green={"o/a": [run(9, "c", "2026-01-01T05:00:00Z")]})
        def fetch(repo, number, attempt):
            if number in (1, 5):
                raise ci_batch.c.ColdfixError("incomplete source")
            return dict(id="f%d" % number, error="FAIL: test_core")
        ledger = dict(seen={}, pages={}, batches=[])
        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.object(ci_batch.failure_ingest, "gh", return_value=[dict(nameWithOwner="o/a")]), \
                mock.patch.object(ci_batch.failure_ingest, "import_records"):
            batches = [ci_batch.ingest(Path(temporary), ledger, "o", 2, github, fetch) for _ in range(4)]
        self.assertEqual([batch["ids"] for batch in batches], [["f2"], ["f3"], ["f4"], []])
        self.assertEqual([batch.get("screened") for batch in batches], [{}, {}, {}, {}])
        self.assertEqual(ledger["fetch_failures"]["1"], 3)
        self.assertIn("1", ledger["seen"])
        self.assertEqual(ledger["fetch_failures"]["5"], 1)
        self.assertNotIn("5", ledger["seen"])

    def test_a_batch_where_every_fetch_fails_still_saves_its_retry_counts(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(ci_batch.shutil.rmtree, root)
        github = FakeGitHub(failed={"o/a": [run(1, "a", "2026-01-01T00:00:00Z")]},
                            green={"o/a": [run(9, "c", "2026-01-01T05:00:00Z")]})
        def fetch(repo, number, attempt):
            raise ci_batch.c.ColdfixError("incomplete source")
        with mock.patch.object(ci_batch.failure_ingest, "gh", return_value=[dict(nameWithOwner="o/a")]):
            for _ in range(3):
                with self.assertRaises(ci_batch.c.ColdfixError):
                    ci_batch.run(root, "o", 10, api=github, fetch=fetch)
            self.assertEqual(ci_batch.run(root, "o", 10, api=github, fetch=fetch),
                             ["ci-backlog: no new fix pairs left in GitHub CI history"])
        saved = json.loads((root / ci_batch.LEDGER).read_text())
        self.assertEqual(saved["fetch_failures"], {"1": 3})
        self.assertEqual(saved["batches"], [])

    def test_a_screened_pair_does_not_hide_a_batch_whose_other_fetches_all_failed(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(ci_batch.shutil.rmtree, root)
        github = FakeGitHub(failed={"o/a": [run(1, "a", "2026-01-01T00:00:00Z"), run(2, "b", "2026-01-01T00:01:00Z")]},
                            green={"o/a": [run(9, "c", "2026-01-01T05:00:00Z")]},
                            compare={"a" * 40 + "..." + "c" * 40: dict(status="diverged", files=[])})
        def fetch(repo, number, attempt):
            raise ci_batch.c.ColdfixError("incomplete source")
        with mock.patch.object(ci_batch.failure_ingest, "gh", return_value=[dict(nameWithOwner="o/a")]), \
                self.assertRaises(ci_batch.c.ColdfixError):
            ci_batch.run(root, "o", 10, api=github, fetch=fetch)
        saved = json.loads((root / ci_batch.LEDGER).read_text())
        self.assertEqual(saved["seen"].get("1", {}).get("screened"), "fix is not a descendant of the recorded failure")
        self.assertEqual(saved["fetch_failures"], {"2": 1})
        self.assertEqual(saved["batches"], [])

    def test_a_github_error_mid_selection_saves_nothing_so_the_next_run_starts_over(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(ci_batch.shutil.rmtree, root)
        github = FakeGitHub(failed={"o/a": [run(1, "a", "2026-01-01T00:00:00Z")]}, green={})
        def failing(endpoint):
            if "/workflows/" in endpoint:
                raise ci_batch.c.ColdfixError("API rate limit")
            return github(endpoint)
        with mock.patch.object(ci_batch.failure_ingest, "gh", return_value=[dict(nameWithOwner="o/a")]):
            with self.assertRaises(ci_batch.c.ColdfixError):
                ci_batch.run(root, "o", 10, api=failing)
        self.assertFalse((root / ci_batch.LEDGER).exists())


CI_RUN = json.loads((Path(__file__).resolve().parent / "fixtures" / "failed-ci-run.json").read_text())


class ScreenTests(unittest.TestCase):
    def test_fix_pairs_replay_would_reject_are_screened_before_fetch_import_and_drafting(self):
        logs = {1: "error: build broke NOCMD", 2: "error: build broke NOCMD", 3: "error: build broke NOCMD",
                4: "FAIL: test_split DIVERGED", 5: "FAIL: test_only TESTONLY", 6: "FAIL: test_kept KEPT"}
        shas = dict(zip(logs, "abdef1"))
        red = [run(number, shas[number], f"2026-01-01T00:0{number}:00Z") for number in logs]
        green = "c" * 40
        github = FakeGitHub(failed={"o/a": red}, green={"o/a": [run(9, "c", "2026-01-01T05:00:00Z")]},
                            compare={"e" * 40 + "..." + green: dict(status="diverged", files=[dict(filename="pkg/core.py")]),
                                     "f" * 40 + "..." + green: dict(status="ahead", files=[dict(filename="pkg/test_core.py")])})
        fetched = []

        def fetch(repo, number, attempt):
            fetched.append(number)
            row = dict(CI_RUN, repo="o/a", run_id=number, head_sha=shas[number] * 40, log_tail=logs[number])
            return ci_batch.failure_ingest.record(ci_batch.failure_ingest.ci_event(row))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / ".coldfix"
            root.mkdir(mode=0o700)
            with mock.patch.object(ci_batch.failure_ingest, "gh", return_value=[dict(nameWithOwner="o/a")]), \
                    mock.patch.object(ci_batch, "learn",
                                      side_effect=lambda root, entry, *rest: entry.update(finished(entry["batch"]))) as learn:
                lines = ci_batch.run(root, "o", 10, api=github, fetch=fetch)
            self.assertNotIn(4, fetched)
            entry = learn.call_args[0][1]
            self.assertEqual(entry["screened"], {"no recorded failing command": 3,
                                                 "fix is not a descendant of the recorded failure": 1,
                                                 "no change between the replay revisions for the requested paths": 1})
            self.assertIn('screened 5 {"fix is not a descendant of the recorded failure": 1, '
                          '"no change between the replay revisions for the requested paths": 1, '
                          '"no recorded failing command": 3}, drafted 1', lines[0])
            seen = json.loads((root / ci_batch.LEDGER).read_text())["seen"]
            self.assertEqual(seen["4"]["screened"], "fix is not a descendant of the recorded failure")
            pairs = json.loads((root / ci_batch.PAIRS).read_text())["results"]
            self.assertEqual([pair["red_run"] for pair in pairs], [6])
            _, store = ci_batch.c.load_store(root)
            self.assertEqual(len(store["failures"]), 1)

            prompts = []
            def ask(model, prompt, shape, work):
                prompts.append(prompt)
                return dict(drafts=[]) if shape is ci_batch.learn_daily.DRAFTS else dict(reviews=[])
            routes = iter([dict(provider="codex", model="drafter"), dict(provider="codex", model="reviewer")])
            with mock.patch.object(ci_batch.learn_daily, "compare", return_value=""):
                ci_batch.learn_daily.run(root, "day", route=lambda role: next(routes), ask=ask)
            self.assertEqual(len(prompts), 1)
            self.assertIn("KEPT", prompts[0])
            for marker in ("NOCMD", "DIVERGED", "TESTONLY"):
                self.assertNotIn(marker, prompts[0])

    def test_only_a_change_outside_the_test_the_replay_excludes_survives(self):
        def diff(*names, status="ahead"):
            return dict(status=status, files=[dict(filename=name) for name in names])
        named = dict(error="FAIL: test_core")
        unnamed = dict(error="error: build broke")
        no_change = "no change between the replay revisions for the requested paths"
        screen = getattr(ci_batch, "screen", None)
        self.assertTrue(callable(screen), "ci_batch has no pre-draft screen")
        self.assertEqual(screen(diff("pkg/core.py", status="behind")), "fix is not a descendant of the recorded failure")
        self.assertIsNone(screen({}, named))
        self.assertEqual(screen(diff(), named), no_change)
        self.assertEqual(screen(diff("tests/test_core.py"), named), no_change)
        self.assertIsNone(screen(diff("tests/test_core.py", "tests/conftest.py"), named))
        self.assertIsNone(screen(diff("pkg/core.py"), named))
        renamed = dict(status="ahead", files=[dict(filename="tests/test_core.py", previous_filename="pkg/core.py")])
        self.assertIsNone(screen(renamed, named))
        self.assertEqual(screen(diff("pkg/core.py"), unnamed), "no recorded failing command")
        with mock.patch.object(ci_batch.failure_ingest, "recorded_command", return_value="./.fleet/verify.sh"):
            self.assertEqual(screen(diff("tests/test_a.py", "tests/helpers.py"), unnamed), no_change)
            self.assertIsNone(screen(diff("tests/test_a.py", "pkg/core.py"), unnamed))
            self.assertIsNone(screen(diff(*[f"tests/test_{n}.py" for n in range(300)]), unnamed))


TEST_PATCH = "@@ -1,2 +1,4 @@\n class T(unittest.TestCase):\n+    def test_rejects_empty(self):\n+        self.fail()\n"


def pull(number, head, author="writer", merged=True):
    return dict(number=number, merged_at="2026-01-02T00:00:00Z" if merged else None,
                head=dict(sha=head * 40), user=dict(login=author))


def comment(number, before, login="reviewer", kind="User", path="pkg/core.py", reply=None):
    return dict(id=number, original_commit_id=before * 40, user=dict(login=login, type=kind), path=path,
                in_reply_to_id=reply, body=f"Finding {number}: this drops empty input",
                html_url=f"https://github.com/o/a/pull/1#discussion_r{number}", created_at="2026-01-01T00:00:00Z")


class OpenSourceGitHub:
    def __init__(self, pulls, comments, compare, visibility=None):
        self.pulls, self.comments, self.compare, self.visibility = pulls, comments, compare, visibility or {}
        self.calls = []

    def __call__(self, endpoint, missing=None):
        self.calls.append(endpoint)
        url = urlsplit(endpoint)
        parts = url.path.split("/")
        repo = "/".join(parts[1:3])
        gone = self.visibility.get(repo) == "gone" or (len(parts) > 5 and self.comments.get(int(parts[4]), []) is None)
        if gone:
            if missing == "HTTP 404":
                return None
            raise ci_batch.c.ColdfixError("gh: Not Found (HTTP 404)")
        if len(parts) == 3:
            visibility = self.visibility.get(repo, "public")
            return dict(full_name=repo, private=visibility != "public", visibility=visibility, archived=False)
        if parts[3] == "compare":
            if parts[4] not in self.compare:
                if missing == "HTTP 404":
                    return None
                raise ci_batch.c.ColdfixError("gh: Not Found (HTTP 404)")
            return self.compare[parts[4]]
        if parts[3] == "pulls" and len(parts) == 4:
            page = int(parse_qs(url.query)["page"][0])
            return self.pulls.get(repo, [])[(page - 1) * 50:page * 50]
        if parts[3] == "pulls":
            page = int(parse_qs(url.query).get("page", ["1"])[0])
            return self.comments.get(int(parts[4]), [])[(page - 1) * 100:page * 100]
        return dict(workflow_runs=[])


def fixed(files, commits=(("f", "2026-01-01T12:00:00Z"),)):
    return dict(status="ahead", files=files,
                commits=[dict(sha=sha * 40, commit=dict(committer=dict(date=date))) for sha, date in commits])


CODE = dict(filename="pkg/core.py", patch="@@ -1 +1 @@\n-x\n+y\n")
TEST = dict(filename="tests/test_core.py", patch=TEST_PATCH)


class OpenSourceTests(unittest.TestCase):
    def feature(self, name):
        self.assertTrue(hasattr(ci_batch, name), "ci_batch cannot learn from open-source repositories")
        return getattr(ci_batch, name)

    def test_only_repositories_github_reports_public_are_learned_from(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "repos.json"
            path.write_text(json.dumps(dict(repos=[dict(repo="o/a"), dict(repo="o/b"), dict(repo="o/c"), dict(repo="o/d")])))
            github = OpenSourceGitHub({}, {}, {}, visibility={"o/b": "private", "o/c": "internal", "o/d": "gone"})
            self.assertEqual(self.feature("public_repos")(path, github), ["o/a"])

    def test_repositories_in_the_coldpass_opt_out_register_are_never_learned_from(self):
        with tempfile.TemporaryDirectory() as directory:
            path, optout = Path(directory) / "repos.json", Path(directory) / "optout.json"
            path.write_text(json.dumps(dict(repos=[dict(repo=name) for name in ("o/a", "o/b", "o/c", "o/old")])))
            optout.write_text(json.dumps(dict(schema_version=1, repos=[
                dict(repo="O/B", date="2026-11-15"), dict(repo="HTTPS://GitHub.com/o/C.GIT", date="2026-11-15"),
                dict(repo="o/renamed", date="2026-11-15")])))
            github = OpenSourceGitHub({}, {}, {})

            def renamed(endpoint, **options):
                meta = github(endpoint, **options)
                return dict(meta, full_name="o/renamed") if endpoint == "repos/o/old" else meta
            self.assertEqual(ci_batch.public_repos(path, renamed, optout=optout), ["o/a"])
            for register in ("{}", "{", json.dumps(dict(schema_version=1, repos=[dict(repo="", date="2026-11-15")])),
                             json.dumps(dict(schema_version=1, repos=[dict(repo="o/b")])),
                             json.dumps(dict(schema_version=1, repos=[dict(repo="o/b", date="soon")]))):
                with self.subTest(register=register):
                    optout.write_text(register)
                    with self.assertRaises(ci_batch.c.ColdfixError):
                        ci_batch.public_repos(path, github, optout=optout)
            with self.assertRaises(ci_batch.c.ColdfixError):
                ci_batch.public_repos(path, github, optout=Path(directory) / "missing.json")
            with self.assertRaises(ci_batch.c.ColdfixError):
                ci_batch.main(["--store", str(Path(directory) / ".coldfix"), "--repos", str(path)])

    def test_a_human_finding_whose_file_a_later_pr_commit_fixed_with_a_new_test_becomes_a_pair(self):
        github = OpenSourceGitHub(
            pulls={"o/a": [pull(1, "f"), pull(2, "e", merged=False)]},
            comments={1: [comment(10, "a"), comment(11, "a", reply=10), comment(12, "a", login="ci", kind="Bot"),
                          comment(13, "a", login="writer"), comment(14, "f"), comment(15, "b", path="pkg/other.py"),
                          comment(16, "c")],
                      2: [comment(20, "a")]},
            compare={"a" * 40 + "..." + "f" * 40: fixed([CODE, TEST]),
                     "b" * 40 + "..." + "f" * 40: fixed([CODE, TEST]),
                     "c" * 40 + "..." + "f" * 40: fixed([CODE, dict(TEST, patch="@@ -1 +1 @@\n-a\n+b\n")])})
        ledger = dict(seen={}, pages={}, batches=[])
        pairs, events, examined = self.feature("review_findings")(ledger, ["o/a"], 10, github)
        self.assertEqual(pairs, [dict(repo="o/a", before="a" * 40, after="f" * 40,
                                      finding_url="https://github.com/o/a/pull/1#discussion_r10")])
        self.assertEqual([(row["kind"], row["source_ref"]) for row in events],
                         [("review", "https://github.com/o/a/pull/1#discussion_r10")])
        self.assertEqual(examined, 1)
        self.assertEqual(ledger["pulls_seen"], ["o/a#1"])
        self.assertEqual(self.feature("review_findings")(ledger, ["o/a"], 10, github)[2], 0)

    def test_a_finding_whose_fix_changes_only_its_test_file_is_not_a_pair(self):
        github = OpenSourceGitHub({"o/a": [pull(1, "f")]}, {1: [comment(10, "a", path=TEST["filename"])]},
                                  {"a" * 40 + "..." + "f" * 40: fixed([TEST])})
        self.assertEqual(self.feature("review_findings")(dict(seen={}, pages={}, batches=[]), ["o/a"], 10, github)[0], [])

    def test_review_pairs_keep_original_right_side_lines_only_on_the_reviewed_commit(self):
        github = OpenSourceGitHub(
            pulls={"o/a": [pull(1, "f")]},
            comments={1: [dict(comment(10, "a"), side="RIGHT", start_side="RIGHT",
                              original_start_line=4, original_line=6, line=10),
                          dict(comment(11, "b"), side="RIGHT", original_line=7),
                          dict(comment(12, "a"), side="LEFT", original_line=9),
                          dict(comment(13, "a"), side="RIGHT", original_line=11)]},
            compare={"a" * 40 + "..." + "f" * 40: fixed([CODE, TEST]),
                     "b" * 40 + "..." + "f" * 40: fixed([CODE, TEST], commits=[
                         ("e", "2025-12-31T00:00:00Z"), ("f", "2026-01-01T12:00:00Z")]),
                     "e" * 40 + "..." + "f" * 40: fixed([CODE, TEST])})
        pairs, _, _ = ci_batch.review_findings({}, ["o/a"], 10, github)
        self.assertEqual([(pair["finding_url"].rsplit("_r", 1)[1], pair["before"], pair.get("path"),
                           pair.get("start_line"), pair.get("end_line")) for pair in pairs],
                         [("10", "a" * 40, "pkg/core.py", 4, 6),
                          ("11", "b" * 40, "pkg/core.py", 7, 7),
                          ("12", "a" * 40, None, None, None),
                          ("13", "a" * 40, "pkg/core.py", 11, 11)])

    def test_a_stored_review_pair_without_flagged_lines_is_refetched_once_and_replays_again(self):
        url = "https://github.com/datarobot-oss/datarobot-genai/pull/772#discussion_r4605753840"
        before, after = "171b645c33bb65d62450e21508a74a62bcfc55ac", "29596a24732a312d2f97c7405bd5e58d4595cda9"
        stored = dict(repo="datarobot-oss/datarobot-genai", before=before, after=after, finding_url=url)
        endpoint = "repos/datarobot-oss/datarobot-genai/pulls/comments/4605753840"
        recorded = dict(path="src/datarobot_genai/core/auth/workload_identity/source.py", original_line=96,
                        original_start_line=None, side="RIGHT", start_side=None, original_commit_id=before)
        github = OpenSourceGitHub({}, {}, {})
        api = lambda call, missing=None: (github.calls.append(call) or recorded) if call == endpoint else github(call, missing)
        failure = dict(id="f", kind="review", source_ref=url, repo="datarobot-oss/datarobot-genai", error="Finding")
        inputs = lambda rows: ci_batch.learn_replay.pair_inputs(dict(failures=dict(f=failure)), dict(evidence=["f"]), rows)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / ".coldfix"
            root.mkdir(mode=0o700)
            (root / ci_batch.PAIRS).write_text(json.dumps(dict(results=[stored])))
            settled = inputs([stored])
            outcome = dict(learned=True, drafted=0, draft_pass=0, proven=0, rejected=0, reject_reasons={})
            with mock.patch.object(ci_batch, "learn", side_effect=lambda root, entry, *rest: entry.update(outcome)) as learn, \
                    mock.patch.object(ci_batch.failure_ingest, "gh", side_effect=AssertionError("owner discovery")):
                for _ in range(2):
                    ci_batch.run(root, None, 10, api=api, public=["datarobot-oss/datarobot-genai"])
            pairs = json.loads((root / ci_batch.PAIRS).read_text())["results"]
        self.assertEqual(learn.call_count, 1)
        self.assertEqual(pairs, [dict(stored, path=recorded["path"], start_line=96, end_line=96)])
        self.assertEqual(github.calls.count(endpoint), 1)
        self.assertNotEqual(inputs(pairs), settled)

    def test_a_stored_pair_moved_off_its_reviewed_commit_is_paired_on_that_commit_once(self):
        def stored(number, before):
            return dict(repo="o/a", before=before * 40, after="f" * 40, path=None,
                        finding_url=f"https://github.com/o/a/pull/1#discussion_r{number}")
        recorded = {number: dict(comment(number, reviewed), side="RIGHT", original_line=7)
                    for number, reviewed in ((10, "a"), (11, "b"), (12, "c"))}
        github = OpenSourceGitHub({}, {}, {"a" * 40 + "..." + "f" * 40: fixed([CODE, TEST]),
                                           "b" * 40 + "..." + "f" * 40: dict(fixed([CODE, TEST]), status="diverged")})

        def api(endpoint, missing=None):
            number = int(endpoint.rsplit("/", 1)[1]) if "/pulls/comments/" in endpoint else None
            return (github.calls.append(endpoint) or recorded[number]) if number else github(endpoint, missing)
        pairs = [stored(10, "e"), stored(11, "e"), stored(12, "c"), dict(stored(13, "e"), path="pkg/core.py"),
                 dict(stored(14, "e"), repo="o/out", finding_url="https://github.com/o/out/pull/1#discussion_r14")]
        for _ in range(2):
            ci_batch.refetch_flagged_lines(pairs, api, ["O/A"])
        self.assertEqual([(row["before"][0], row["path"], row.get("start_line"), row.get("reviewed_commit", "")[:1])
                          for row in pairs],
                         [("a", "pkg/core.py", 7, ""), ("e", None, None, "b"),
                          ("c", "pkg/core.py", 7, ""), ("e", "pkg/core.py", None, ""), ("e", None, None, "")])
        self.assertEqual(sum("/pulls/comments/" in call for call in github.calls), 3)

    def test_each_batch_examines_a_bounded_share_of_merged_prs_per_repository(self):
        pulls = {repo: [pull(number, "f") for number in range(1, 120)] for repo in ("o/a", "o/b")}
        github = OpenSourceGitHub(pulls, {}, {})
        ledger = dict(seen={}, pages={}, batches=[])
        self.assertEqual(self.feature("review_findings")(ledger, ["o/a", "o/b"], 20, github)[2], 20)
        self.assertEqual(sum("/comments" in call for call in github.calls), 20)
        self.assertEqual(self.feature("review_findings")(ledger, ["o/a", "o/b"], 20, github)[2], 20)
        self.assertEqual(len(set(ledger["pulls_seen"])), 40)

    def test_an_open_source_batch_imports_review_findings_and_their_fix_pairs_for_drafting(self):
        self.feature("review_findings")
        github = OpenSourceGitHub({"o/a": [pull(1, "f")]}, {1: [comment(10, "a")]},
                                  {"a" * 40 + "..." + "f" * 40: fixed([CODE, TEST])})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / ".coldfix"
            root.mkdir(mode=0o700)
            outcome = dict(learned=True, drafted=0, draft_pass=0, proven=0, rejected=0, reject_reasons={})
            with mock.patch.object(ci_batch, "learn", side_effect=lambda root, entry, *rest: entry.update(outcome)) as learn, \
                    mock.patch.object(ci_batch.failure_ingest, "gh", side_effect=AssertionError("owner discovery")):
                ci_batch.run(root, None, 10, api=github, public=["o/a"])
            entry = learn.call_args[0][1]
            self.assertEqual(learn.call_args[0][-1], ["o/a"])
            self.assertEqual((entry["review_pairs"], entry["pulls_examined"], len(entry["ids"])), (1, 1, 1))
            _, store = ci_batch.c.load_store(root)
            self.assertEqual(store["failures"][entry["ids"][0]]["source_ref"],
                             "https://github.com/o/a/pull/1#discussion_r10")
            pairs = json.loads((root / ci_batch.PAIRS).read_text())["results"]
            self.assertEqual([row.get("finding_url") for row in pairs], ["https://github.com/o/a/pull/1#discussion_r10"])
            self.assertTrue(ci_batch.learn_candidates.candidates(store, pairs=pairs)[0]["replayable"])

    def test_retained_evidence_from_a_repository_no_longer_allowed_is_never_drafted(self):
        self.feature("review_findings")
        github = OpenSourceGitHub({"o/a": [pull(1, "f")]}, {1: [comment(10, "a")]},
                                  {"a" * 40 + "..." + "f" * 40: fixed([CODE, TEST])})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / ".coldfix"
            root.mkdir(mode=0o700)
            outcome = dict(learned=True, drafted=0, draft_pass=0, proven=0, rejected=0, reject_reasons={})
            with mock.patch.object(ci_batch, "learn", side_effect=lambda root, entry, *rest: entry.update(outcome)), \
                    mock.patch.object(ci_batch.failure_ingest, "gh", side_effect=AssertionError("owner discovery")):
                ci_batch.run(root, None, 10, api=github, public=["o/a"])
            seen = []

            def drafting(root, day, ask, reserved_rules=None):
                pairs = json.loads((root / ci_batch.PAIRS).read_text())["results"]
                store = ci_batch.c.load_store(root)[1]
                seen.append([row["repos"] for row in ci_batch.learn_candidates.candidates(store, pairs=pairs)])
                return dict(day=day, candidates=0)
            with mock.patch.object(ci_batch.learn_daily, "run", side_effect=drafting), \
                    mock.patch.object(ci_batch.learn_replay, "run_in_workspace", return_value={}), \
                    mock.patch.object(ci_batch, "measure", return_value={}), \
                    mock.patch.object(ci_batch, "merge_evidence"):
                for public in (["O/A"], []):
                    ci_batch.learn(root, dict(batch=1, ids=[]), None, None, 1, None, public)
            self.assertEqual(seen, [[["o/a"]], []])
            self.assertEqual(len(ci_batch.c.load_store(root)[1]["failures"]), 1)

    def test_a_finding_is_paired_on_the_commit_it_reviewed_even_when_a_merged_mainline_commit_predates_it(self):
        github = OpenSourceGitHub(
            pulls={"o/a": [pull(1, "f")]},
            comments={1: [comment(10, "a"), comment(11, "b")]},
            compare={"a" * 40 + "..." + "f" * 40: fixed([CODE, TEST], commits=[("f", "2025-12-31T00:00:00Z")]),
                     "b" * 40 + "..." + "f" * 40: fixed([CODE, TEST], commits=[("e", "2025-12-31T00:00:00Z"),
                                                                                ("f", "2026-01-01T12:00:00Z")]),
                     "e" * 40 + "..." + "f" * 40: fixed([CODE, TEST])})
        ledger = dict(seen={}, pages={}, batches=[])
        pairs = self.feature("review_findings")(ledger, ["o/a"], 10, github)[0]
        self.assertEqual([(row["finding_url"][-3:], row["before"]) for row in pairs],
                         [("r10", "a" * 40), ("r11", "b" * 40)])

    def test_a_repository_or_pr_github_no_longer_serves_is_skipped_without_stopping_the_batch(self):
        github = OpenSourceGitHub({"o/a": [pull(1, "f"), pull(2, "f")]}, {1: None, 2: [comment(10, "a")]},
                                  {"a" * 40 + "..." + "f" * 40: fixed([CODE, TEST])}, visibility={"o/gone": "gone"})
        ledger = dict(seen={}, pages={}, batches=[])
        pairs, _, examined = self.feature("review_findings")(ledger, ["o/gone", "o/a"], 10, github)
        self.assertEqual(([row["finding_url"][-3:] for row in pairs], examined), (["r10"], 2))

    def test_batches_smaller_than_the_repository_list_take_turns_across_repositories(self):
        repos = ["o/a", "o/b", "o/c"]
        github = OpenSourceGitHub({repo: [pull(number, "f") for number in range(1, 10)] for repo in repos}, {}, {})
        ledger = dict(seen={}, pages={}, batches=[])
        for _ in range(3):
            self.feature("review_findings")(ledger, repos, 2, github)
        self.assertEqual({key.split("#")[0] for key in ledger["pulls_seen"]}, set(repos))

    def test_later_batches_examine_the_rest_of_a_page_before_paging_further_back(self):
        github = OpenSourceGitHub({"o/a": [pull(number, "f") for number in range(1, 120)]}, {}, {})
        ledger = dict(seen={}, pages={}, batches=[])
        for _ in range(2):
            self.feature("review_findings")(ledger, ["o/a"], 10, github)
        self.assertEqual(sorted(ledger["pulls_seen"]), sorted(f"o/a#{number}" for number in range(1, 21)))

    def test_a_batch_never_examines_more_prs_than_its_size(self):
        pulls = {repo: [pull(number, "f") for number in range(1, 10)] for repo in ("o/a", "o/b", "o/c")}
        github = OpenSourceGitHub(pulls, {}, {})
        ledger = dict(seen={}, pages={}, batches=[])
        self.assertEqual(self.feature("review_findings")(ledger, ["o/a", "o/b", "o/c"], 2, github)[2], 2)
        self.assertEqual(sum("/comments" in call for call in github.calls), 2)

    def test_pending_retries_never_query_a_repository_that_is_no_longer_listed(self):
        github = FakeGitHub(failed={"o/a": [], "o/b": [run(1, "a", "2026-01-01T00:00:00Z")]}, green={})
        ledger = dict(seen={}, pages={}, batches=[])
        now = ci_batch.c.time_key("2026-01-02T00:00:00Z")
        ci_batch.select(ledger, ["o/a", "o/b"], 10, 1, github, now=now)
        self.assertTrue(ledger["seen"]["1"]["pending"])
        before = len(github.calls)
        ci_batch.select(ledger, ["o/a"], 10, 2, github, now=now)
        self.assertEqual([call for call in github.calls[before:] if "/o/b/" in call], [])

    def test_review_findings_are_still_learned_when_every_ci_log_fetch_fails(self):
        review = OpenSourceGitHub({"o/a": [pull(1, "f")]}, {1: [comment(10, "a")]},
                                  {"a" * 40 + "..." + "f" * 40: fixed([CODE, TEST])})
        ci = FakeGitHub(failed={"o/a": [run(1, "a", "2026-01-01T00:00:00Z")]},
                        green={"o/a": [run(9, "c", "2026-01-01T05:00:00Z")]})
        github = lambda endpoint, missing=None: ci(endpoint) if "/actions/" in endpoint else review(endpoint, missing)
        def fetch(repo, number, attempt):
            raise ci_batch.c.ColdfixError("incomplete source")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / ".coldfix"
            root.mkdir(mode=0o700)
            outcome = dict(learned=True, drafted=0, draft_pass=0, proven=0, rejected=0, reject_reasons={})
            with mock.patch.object(ci_batch, "learn", side_effect=lambda root, entry, *rest: entry.update(outcome)) as learn:
                ci_batch.run(root, None, 10, api=github, fetch=fetch, public=["o/a"])
            entry = learn.call_args[0][1]
            self.assertEqual((entry["fix_pairs"], entry["review_pairs"], len(entry["ids"])), (1, 1, 1))


    def test_a_finding_on_a_commit_github_no_longer_has_is_skipped_without_stopping_the_batch(self):
        github = OpenSourceGitHub({"o/a": [pull(1, "f")]}, {1: [comment(10, "d"), comment(11, "a")]},
                                  {"a" * 40 + "..." + "f" * 40: fixed([CODE, TEST])})
        ledger = dict(seen={}, pages={}, batches=[])
        pairs = self.feature("review_findings")(ledger, ["o/a"], 10, github)[0]
        self.assertEqual([row["finding_url"] for row in pairs], ["https://github.com/o/a/pull/1#discussion_r11"])

    def test_a_batch_fetches_a_bounded_number_of_pr_pages_per_repository(self):
        github = OpenSourceGitHub({"o/a": [pull(number, "f", merged=False) for number in range(1, 1000)]}, {}, {})
        ledger = dict(seen={}, pages={}, batches=[])
        self.feature("review_findings")(ledger, ["o/a"], 10, github)
        first = len(github.calls)
        self.assertLessEqual(first, 4)
        self.feature("review_findings")(ledger, ["o/a"], 10, github)
        self.assertIn(f"page={first + 1}", github.calls[first])

    def test_findings_on_later_pages_of_review_comments_are_read(self):
        comments = [comment(number, "a", login="writer") for number in range(100, 200)] + [comment(10, "a")]
        github = OpenSourceGitHub({"o/a": [pull(1, "f")]}, {1: comments},
                                  {"a" * 40 + "..." + "f" * 40: fixed([CODE, TEST])})
        ledger = dict(seen={}, pages={}, batches=[])
        pairs = self.feature("review_findings")(ledger, ["o/a"], 10, github)[0]
        self.assertEqual([row["finding_url"] for row in pairs], ["https://github.com/o/a/pull/1#discussion_r10"])


    def test_a_github_outage_during_a_compare_stops_the_batch_instead_of_dropping_the_finding(self):
        review = OpenSourceGitHub({"o/a": [pull(1, "f")]}, {1: [comment(10, "a")]}, {})
        def github(endpoint, missing=None):
            if "/compare/" in endpoint:
                raise ci_batch.c.ColdfixError("gh: Bad Gateway (HTTP 502)")
            return review(endpoint, missing)
        with self.assertRaises(ci_batch.c.ColdfixError):
            self.feature("review_findings")(dict(seen={}, pages={}, batches=[]), ["o/a"], 10, github)


if __name__ == "__main__":
    unittest.main()
