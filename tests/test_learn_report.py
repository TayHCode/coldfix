from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import coldfix as c
import failure_ingest as ingest
import learn_daily
import learn_report


ROOT = Path(__file__).resolve().parents[1]
RUN = json.loads((ROOT / "tests" / "fixtures" / "failed-ci-run.json").read_text())


NO_LESSONS = dict(delivered=dict(lessons=0, failures_before=0, failures_after=0),
                  undelivered=dict(lessons=0, failures_before=0, failures_after=0))


class LearnReportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Path(temporary.name) / ".coldfix"
        self.store.mkdir(mode=0o700)

    def test_cohort_cli_reports_a_missing_lessons_directory(self):
        process = subprocess.run(["python3", str(ROOT / "learn_report.py"), "--store", str(self.store),
                                  "--lessons", str(self.store / "missing")],
                                 capture_output=True, text=True)
        self.assertEqual(process.returncode, 2)
        self.assertIn("lessons folder not found", process.stderr)

    def test_counts_only_the_window_and_reads_runs_and_drafts_from_the_daily_ledger(self):
        for index in range(3):
            row = dict(RUN, head_sha=str(index) * 40, run_id=2000 + index, log_tail="AssertionError: same")
            ingest.import_records(self.store, [ingest.record(ingest.ci_event(row))])
        now = datetime.now(timezone.utc)
        today, old = now.date().isoformat(), (now - timedelta(days=9)).date().isoformat()
        ledger = dict(drafts={"a" * 32: dict(day=today, verdict="PASS"), "b" * 32: dict(day=old, verdict="FAIL")},
                      runs=[dict(day=old, candidates=1, calls=2, drafted=1, passed=0),
                            dict(day=today, candidates=1, calls=2, drafted=1, passed=1),
                            dict(day=today, skipped="already ran today")])
        (self.store / learn_daily.LEDGER).write_text(json.dumps(ledger))
        result = learn_report.report(self.store, now)
        self.assertEqual({key: value for key, value in result.items() if key != "window"},
                         dict(failures_collected=dict(ci=3), flaky_failures=0, candidates_waiting=1, daily_runs=1,
                              daily_runs_failed=0, model_calls=2, drafts=dict(PASS=1), lessons_proven=0,
                              lessons_delivered=dict(already_enforced_by_fix=0, added_by_coldfix=0, prose_only=0),
                              lessons_rejected={}, repeat_failures={}, ci_lesson_recurrence=[],
                              recurrence_comparison=NO_LESSONS))
        later = learn_report.report(self.store, now + timedelta(days=30))
        self.assertEqual((later["failures_collected"], later["model_calls"], later["drafts"]), ({}, 0, {}))

    def test_proven_ci_lessons_without_a_delivery_record_count_as_undelivered(self):
        (self.store / learn_daily.LEDGER).write_text(json.dumps(dict(drafts={}, runs=[])))
        (self.store / learn_report.check_delivery.RECORDS).write_text(json.dumps(dict(Y=dict(state="waiting"))))
        with mock.patch.object(learn_report.check_delivery, "proven", return_value=[dict(lesson_id="X"), dict(lesson_id="Y")]), \
                mock.patch.object(learn_report, "ci_recurrence", return_value=[]) as ci:
            learn_report.report(self.store, datetime.now(timezone.utc))
        self.assertEqual(ci.call_args.args[1], dict(X={}, Y=dict(state="waiting")))

    def test_failure_window_uses_collection_time_including_midnight_boundary(self):
        now = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)
        for index, created in enumerate(('2026-11-03T23:59:59Z', '2026-11-04T00:00:00Z')):
            row = ingest.record(ingest.ci_event(dict(RUN, run_id=4000 + index)))
            row['created'] = created
            ingest.import_records(self.store, [row])
        result = learn_report.report(self.store, now, days=7)
        self.assertEqual(result['failures_collected'], {'ci': 1})
        self.assertEqual(learn_report.report(self.store, now, days=8)['failures_collected'], {'ci': 2})

    def test_the_window_starts_at_midnight_utc_of_the_first_day_for_ledger_rows(self):
        now = datetime(2026, 11, 7, 16, 54, tzinfo=timezone.utc)
        ledger = dict(drafts={"a" * 32: dict(day="2026-11-01", verdict="PASS"), "b" * 32: dict(day="2026-10-31", verdict="PASS")},
                      runs=[dict(day="2026-11-01", candidates=1, calls=2, drafted=1, passed=1),
                            dict(day="2026-10-31", candidates=1, calls=2, drafted=1, passed=1)])
        (self.store / learn_daily.LEDGER).write_text(json.dumps(ledger))
        result = learn_report.report(self.store, now)
        self.assertEqual(result["window"]["start"], "2026-11-01T00:00:00+00:00")
        self.assertEqual((result["daily_runs"], result["model_calls"], result["drafts"]), (1, 2, dict(PASS=1)))

    def test_each_generated_proof_test_in_the_window_counts_as_one_model_call(self):
        now = datetime(2026, 11, 7, 16, 54, tzinfo=timezone.utc)
        ledger = dict(drafts={}, runs=[dict(day="2026-11-06", candidates=0, calls=0, drafted=0, passed=0)])
        (self.store / learn_daily.LEDGER).write_text(json.dumps(ledger))
        (self.store / "proof-tests.json").write_text(json.dumps({
            "a" * 64: dict(created="2026-11-06T10:00:00+00:00"), "b" * 64: dict(created="2026-10-01T10:00:00+00:00")}))
        self.assertEqual(learn_report.report(self.store, now)["model_calls"], 1)

    def test_each_generated_proof_test_call_counts_in_the_window_it_was_made_in(self):
        now = datetime(2026, 11, 7, 16, 54, tzinfo=timezone.utc)
        ledger = dict(drafts={}, runs=[dict(day="2026-11-06", candidates=0, calls=0, drafted=0, passed=0)])
        (self.store / learn_daily.LEDGER).write_text(json.dumps(ledger))
        inside, earlier = "2026-11-06T10:00:00+00:00", "2026-10-01T10:00:00+00:00"
        (self.store / "proof-tests.json").write_text(json.dumps({
            "a" * 64: dict(created=inside, asked=[inside, inside]),
            "b" * 64: dict(created=inside, asked=[earlier, inside]),
            "c" * 64: dict(created=inside)}))
        self.assertEqual(learn_report.report(self.store, now)["model_calls"], 4)

    def test_a_lesson_counts_once_and_only_when_first_proven_in_the_window(self):
        start = datetime(2026, 11, 1, tzinfo=timezone.utc)
        row = lambda revision, lesson, day, verification="proven": dict(
            id=revision, lesson_id=lesson, created=f"2026-{day}T12:00:00Z", verification=verification)
        lessons = {key: value for key, value in (
            ("a1", row("a1", "a", "11-02")), ("a2", row("a2", "a", "11-03")),
            ("b1", row("b1", "b", "10-20")), ("b2", row("b2", "b", "11-04")),
            ("c1", row("c1", "c", "11-05", "unproven")))}
        self.assertEqual(learn_report.newly_proven(lessons, start), 1)

    def test_reports_rejected_replays_by_reason_in_the_window(self):
        now = datetime(2026, 11, 7, 16, 54, tzinfo=timezone.utc)
        ledger = dict(drafts={
            "a": dict(day="2026-11-07", verdict="PASS", replay=dict(status="rejected", reason="missing fix")),
            "b": dict(day="2026-11-06", verdict="PASS", replay=dict(status="rejected", reason="missing fix")),
            "c": dict(day="2026-11-05", verdict="PASS", replay=dict(status="rejected", reason="test failed")),
            "d": dict(day="2026-10-01", verdict="PASS", replay=dict(status="rejected", reason="old")),
            "e": dict(day="2026-11-07", verdict="PASS", replay=dict(status="proven")),
        }, runs=[])
        (self.store / learn_daily.LEDGER).write_text(json.dumps(ledger))
        with mock.patch.object(learn_report.check_delivery, "proven", return_value=[]):
            self.assertEqual(learn_report.report(self.store, now)["lessons_rejected"],
                             {"ours": {"missing fix": 2, "test failed": 1}})

    def test_reports_held_review_pairs_without_counting_unpaired_findings(self):
        report = self.store / 'review.json'
        report.write_text(json.dumps(dict(results=[
            dict(before='before', after='after', reason='local tool unavailable'),
            dict(before='before', after=None, reason='NO_LATER_COMMIT'),
            dict(before='before', after='after', reason=None),
        ])))
        (self.store / 'learn-review-replay.json').write_text(json.dumps(dict(report=str(report))))
        result = learn_report.report(self.store, datetime.now(timezone.utc))
        self.assertEqual(result['lessons_rejected'], {'ours': {'local tool unavailable': 1}})

    def test_splits_joined_replay_reasons_into_buckets_without_ids(self):
        now = datetime(2026, 11, 7, 16, 54, tzinfo=timezone.utc)
        first, second = "1" * 32, "2" * 32
        attempts = [
            f"{first}: source repository checkout unavailable; {second}: no descendant contains a unittest "
            "matching the recorded failure",
            'incident cannot be exported; privacy screen blocked: {"credential": 1, "hostname": 12}',
            "incident not admitted; red FAIL, green FAIL, reversed-patch FAIL; expected FAIL, PASS, FAIL",
            "incident not admitted; red INCOMPLETE, green INCOMPLETE, reversed-patch INCOMPLETE; expected FAIL, PASS, FAIL",
            f"{first}: Command '['git', '-C', '/private/work/repo', 'fetch', '--quiet', 'origin', '{second}']' "
            "returned non-zero exit status 128.",
            f"{first}: something new broke",
        ]
        ledger = dict(drafts={"a": dict(day="2026-11-07", verdict="PASS", replay_attempts=[
            dict(day="2026-11-07", reason=reason) for reason in attempts])}, runs=[])
        (self.store / learn_daily.LEDGER).write_text(json.dumps(ledger))
        self.assertEqual(learn_report.report(self.store, now)["lessons_rejected"], dict(
            ours={"source repository checkout unavailable": 1,
                  "incident not admitted: red INCOMPLETE, green INCOMPLETE, reversed INCOMPLETE": 1,
                  "git fetch command failed": 1, "something new broke": 1},
            not_provable={"no descendant contains a unittest matching the recorded failure": 1,
                          "incident not admitted: red FAIL, green FAIL, reversed FAIL": 1},
            privacy_screen={"privacy screen: credential": 1, "privacy screen: hostname": 1}))

    def test_cli_prints_zeros_for_an_empty_store(self):
        result = subprocess.run(["python3", str(ROOT / "learn_report.py"), "--store", str(self.store)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual((output["model_calls"], output["candidates_waiting"], output["failures_collected"]), (0, 0, {}))

    def test_cli_with_a_lessons_folder_reports_each_lesson_before_and_after_delivery(self):
        folder = self.store.parent / "lessons"
        folder.mkdir()
        (folder / "L4-verify-the-right-thing.md").write_text("---\nid: L4\ndelivered: 2026-11-06T00:00:00+00:00\n---\n")
        keys = []
        for index, occurred in enumerate(("2026-11-01T00:00:00Z", "2026-11-08T00:00:00Z", "2026-11-20T00:00:00Z")):
            row = ingest.record(ingest.ci_event(dict(RUN, run_id=3000 + index, occurred=occurred)))
            ingest.import_records(self.store, [row])
            keys.append(row["id"])
        (self.store / "transcript-lessons.json").write_text(json.dumps(dict(leads={key: dict(
            status="duplicate", reason="coldlocker:lessons/L4-verify-the-right-thing", batch="2026-11-06T00:00:00+00:00")
            for key in keys})))
        command = ["python3", str(ROOT / "learn_report.py"), "--store", str(self.store), "--lessons", str(folder)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["lesson_recurrence"], [dict(
            lesson="L4", delivered="2026-11-06T00:00:00+00:00", delivered_by="lesson file",
            failures_before=1, failures_after=1, failures_since=2)])
        self.assertEqual(output["promote_to_check"], ["L4"])
        self.assertEqual(output["promote_to_check_untracked"], ["L4"])
        self.assertEqual((output["enforced"], output["retired_because_fixed"]), (0, 0))
        longer = json.loads(subprocess.run(command + ["--days", "30"], capture_output=True, text=True, check=True).stdout)
        self.assertEqual(longer["lesson_recurrence"], output["lesson_recurrence"])
        missing = subprocess.run(command[:-1] + [str(folder / "missing")], capture_output=True, text=True)
        self.assertEqual(missing.returncode, 2)
        self.assertIn("lessons folder not found", missing.stderr)

    def test_reports_ci_and_review_repeat_failures_per_repository_in_the_window(self):
        ci = json.dumps(dict(type="ci", conclusion="failure"))

        def row(key, source, repo, group, occurred, error=ci, kind="ci"):
            return dict(id=key, created="2026-11-11T00:00:00+00:00", occurred=occurred, source=source, repo=repo,
                        group_id=key, command="python3 -m unittest " + group, error=error, kind=kind)
        store = dict(failures={r["id"]: r for r in [
            row("1", "failure-ingest", "o/a", "g1", "2026-10-01T00:00:00Z"),
            row("2", "failure-ingest", "O/A", "g1", "2026-11-09T00:00:00Z"),
            row("3", "failure-ingest", "o/a", "g2", "2026-11-09T00:00:00Z"),
            row("4", "failure-ingest", "o/a", "g2", "2026-11-10T00:00:00Z"),
            row("5", "cold-review", "o/a", "g1", "2026-11-10T00:00:00Z", "finding", "review"),
            row("6", "github", "o/b", "g3", "2026-11-10T00:00:00Z", "finding", "review"),
            row("7", "failure-ingest", "o/b", "g4", "2026-11-10T00:00:00Z", json.dumps(dict(type="coldbox"))),
            row("8", "failure-ingest", "o/a", "g1", "2026-11-10T00:00:00Z", json.dumps(dict(type="ci", conclusion="success"))),
            row("9", "stdin", "o/a", "g1", "2026-11-10T00:00:00Z", json.dumps(dict(type="transcript_correction"))),
            row("10", "failure-ingest", "o/a", "g1", "2026-11-10T00:00:00Z", json.dumps(dict(type="coldpass"))),
            row("11", "github", "o/b", "g5", "2026-11-09T00:00:00Z", "failure"),
            row("12", "github", "o/b", "g5", "2026-11-10T00:00:00Z", "failure"),
            row("13", "github", "o/b", "g6", "2026-11-10T00:00:00Z", "fix", "issue"),
            row("14", "failure-ingest", "o/a", "g2", "2026-11-12T00:00:00Z")]})
        start, end = datetime(2026, 11, 5, tzinfo=timezone.utc), datetime(2026, 11, 11, tzinfo=timezone.utc)
        self.assertEqual(learn_report.repeat_failures(store, start, end), {
            "o/a": dict(ci=dict(failures=3, repeats=2, rate=0.67), review=dict(failures=1, repeats=0, rate=0.0)),
            "o/b": dict(ci=dict(failures=3, repeats=1, rate=0.33), review=dict(failures=1, repeats=0, rate=0.0))})
        self.assertEqual(learn_report.repeat_failures(store, end, end + timedelta(hours=1)), {})

    def test_ci_lessons_count_failures_around_delivery_or_proof_and_compare_delivered_with_undelivered(self):
        def failure(key, group, occurred):
            return dict(id=key, created="2026-11-19T00:00:00+00:00", occurred=occurred, source="failure-ingest",
                        repo="o/a", group_id=group, command="x", error="{}")

        def lesson(key, created, verification="proven", status="active", previous="", lesson_id=None):
            return dict(id=key, lesson_id=lesson_id or key, previous=previous, created=created, group_id="g" + (lesson_id or key),
                        grouping="exact", verification=verification, status=status)
        occurred = dict(A=["2026-11-01", "2026-11-03", "2026-11-06", "2026-11-15"], B=["2026-11-01", "2026-11-05", "2026-11-06"],
                        C=["2026-11-17", "2026-11-19"], D=["2026-11-06"], E=["2026-11-06"], F=["2026-11-06"],
                        G=["2026-11-01", "2026-11-06"], H=["2026-11-06"], J=["2026-11-06"])
        store = dict(failures={f"{name}{index}": failure(f"{name}{index}", "g" + name, day + "T00:00:00Z")
                               for name, days in occurred.items() for index, day in enumerate(days)},
                     lessons={row["id"]: row for row in [
                         lesson("A", "2026-10-30T00:00:00+00:00", verification="unproven"),
                         lesson("A2", "2026-11-02T00:00:00+00:00", previous="A", lesson_id="A"),
                         lesson("A3", "2026-11-03T00:00:00+00:00", previous="A2", lesson_id="A"),
                         lesson("B", "2026-11-04T00:00:00+00:00"), lesson("C", "2026-11-18T00:00:00+00:00"),
                         lesson("D", "2026-11-01T00:00:00+00:00", status="retired"),
                         lesson("E", "2026-11-01T00:00:00+00:00", verification="unproven"),
                         lesson("F", "2026-11-01T00:00:00+00:00"),
                         lesson("F2", "2026-11-02T00:00:00+00:00", verification="unproven", previous="F", lesson_id="F"),
                         lesson("G", "2026-11-01T00:00:00+00:00"), lesson("H", "2026-11-01T00:00:00+00:00"),
                         lesson("I", "2026-11-01T00:00:00+00:00"), lesson("J", "2026-11-01T00:00:00+00:00")]})
        store["failures"]["A-local"] = dict(failure("A-local", "gA", "2026-11-07T00:00:00Z"),
                                            source="ci-fix-learning-local-reproduction")
        delivered = lambda at, route="bot-pr": dict(state="delivered", at=at, route=route)
        records = dict(A=delivered("2026-11-05T00:00:00+00:00"), B=dict(state="waiting", day="2026-11-05"),
                       C=dict(state="waiting"), D=delivered("2026-11-05T00:00:00+00:00"), E=dict(state="waiting"),
                       F=delivered("2026-11-05T00:00:00+00:00"), G=delivered("2026-11-05T00:00:00+00:00", "existing-test"),
                       H=dict(state="retired"), J={})
        rows = learn_report.ci_recurrence(store, records)
        self.assertEqual(rows, [
            dict(lesson="A", delivered="2026-11-05T00:00:00+00:00", route="bot-pr", proven="2026-11-02T00:00:00+00:00",
                 failures_before=2, failures_after=1, failures_since=2),
            dict(lesson="B", delivered=None, route=None, proven="2026-11-04T00:00:00+00:00",
                 failures_before=1, failures_after=2, failures_since=2),
            dict(lesson="C", delivered=None, route=None, proven="2026-11-18T00:00:00+00:00",
                 failures_before=1, failures_after=1, failures_since=1),
            dict(lesson="G", delivered="2026-11-05T00:00:00+00:00", route="existing-test",
                 proven="2026-11-01T00:00:00+00:00", failures_before=1, failures_after=1, failures_since=1),
            dict(lesson="J", delivered=None, route=None, proven="2026-11-01T00:00:00+00:00",
                 failures_before=0, failures_after=1, failures_since=1)])
        transcript = [dict(lesson="L1", delivered="2026-11-05T00:00:00Z", delivered_by="lesson file",
                           failures_before=3, failures_after=0, failures_since=0),
                      dict(lesson="L2", delivered=None, delivered_by=None)]
        self.assertEqual(learn_report.compared(rows + transcript, datetime(2026, 11, 20, tzinfo=timezone.utc)), dict(
            delivered=dict(lessons=2, failures_before=5, failures_after=1),
            undelivered=dict(lessons=2, failures_before=1, failures_after=3)))
        self.assertEqual(learn_report.ci_recurrence(store, dict(Z=dict(state="waiting"))), [])

    def test_reports_the_median_injected_bytes_per_session_in_the_window(self):
        log = self.store.parent / "injections.jsonl"
        row = lambda at, session, size: json.dumps(dict(at=at, session=session, event="PreToolUse", refs=["r"],
                                                        chars=size, bytes=size))
        log.write_text("\n".join([
            row("2026-11-10T00:00:00+00:00", "a", 100), row("2026-11-10T01:00:00+00:00", "a", 50),
            row("2026-11-10T00:00:00+00:00", "b", 30), row("2026-11-11T00:00:00+00:00", "c", 10),
            row("2026-11-11T00:00:00+00:00", None, 999), row("2026-10-01T00:00:00+00:00", "d", 5000)])
                       + "\n" + row("2026-11-11T00:00:00+00:00", "torn", 7)[:20])
        now = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)
        self.assertNotIn("context_injected", learn_report.report(self.store, now))
        output = learn_report.report(self.store, now, injections=log)
        self.assertEqual(output["context_injected"], dict(
            sessions=3, events=4, median_bytes_per_session=30, total_bytes=190, events_without_session=1))
        command = ["python3", str(ROOT / "learn_report.py"), "--store", str(self.store), "--injections", str(log)]
        self.assertEqual(subprocess.run(command, capture_output=True, text=True, check=True).returncode, 0)
        for bad in ("{", json.dumps(dict(at="2026-11-10", session="a", bytes=1)),
                    json.dumps(dict(at="20261110T000000+0000", session="a", bytes=1)),
                    json.dumps(dict(at=20261110, session="a", bytes=1)),
                    json.dumps(dict(at="2026-11-10T00:00:00+01:99", session="a", bytes=1)),
                    json.dumps(dict(at="2026-11-10T00:00:00+00:00", session="a", bytes=-1)),
                    json.dumps(dict(at="2026-11-10T00:00:00+00:00", session=7, bytes=1)),
                    json.dumps(dict(at="2026-11-10T00:00:00+00:00", session="a", bytes=True))):
            with self.subTest(bad=bad):
                log.write_text(bad + "\n")
                result = subprocess.run(command, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("injections.jsonl line 1 is not an injection record", result.stderr)
        missing = subprocess.run(command[:-1] + [str(log.parent / "missing.jsonl")], capture_output=True, text=True)
        self.assertEqual(missing.returncode, 2)
        self.assertIn("injections log not found", missing.stderr)

    def test_cli_counts_enforced_and_fixed_lessons_and_tracked_promotions(self):
        folder = self.store.parent / "lessons"
        folder.mkdir()
        for name, head in (("L13-conflict-markers", "status: enforced\ncheck: ci/check-conflicts.py"),
                           ("L14-clean-checkout", "status: enforced"),
                           ("L20-fixed", "status: retired\ncheck: example/x#1"),
                           ("L21-obsolete", "status: retired"),
                           ("L22-active", "status: active\ncheck: ci/x.py")):
            (folder / f"{name}.md").write_text(f"---\nid: {name.split('-')[0]}\n{head}\n---\n")
        command = ["python3", str(ROOT / "learn_report.py"), "--store", str(self.store), "--lessons", str(folder)]
        (folder / "promote-to-check.json").write_text(json.dumps({"L4": "https://github.com/example/coldfix/issues/1"}))
        with mock.patch.object(learn_report.transcript_delivery, "recurrence", return_value=([], ["L4", "L13"])):
            output = learn_report.report(self.store, datetime(2026, 11, 11, tzinfo=timezone.utc), 7, folder)
        self.assertEqual((output["enforced"], output["retired_because_fixed"]), (2, 1))
        self.assertEqual(output["promote_to_check_untracked"], ["L13"])
        for bad in ([], {"L4": ""}, {"L4": 1}, {"L4": "not a link"}, {"4": "https://github.com/example/coldfix/issues/1"}):
            (folder / "promote-to-check.json").write_text(json.dumps(bad))
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("promote-to-check.json must map lesson ids to GitHub issue links", result.stderr)

if __name__ == "__main__":
    unittest.main()
