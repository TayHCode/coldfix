from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "learn_stall.py"
COLLECT, DAILY, TRANSCRIPTS = "coldfix-collect.service", "coldfix-daily.service", "coldfix-transcripts.service"
BACKLOG, OSS = "coldfix-backlog.service", "coldfix-oss.service"
SYNC = "coldfix-lessons-sync.service"
FAKE_GH = """
import json, os, sys
words, db = sys.argv[1:], os.environ["FAKE_GH_DB"]
issues = json.load(open(db)) if os.path.exists(db) else []
if os.environ.get("FAKE_GH_FAIL") or words[-2:] != ["-R", "example/repo"]:
    sys.exit(1)
value = lambda flag: words[words.index(flag) + 1]
if words[:2] == ["issue", "view"] and words[3:7] == ["--json", "state", "--jq", ".state"]:
    print(issues[int(words[2]) - 1]["state"].upper())
elif words[:2] == ["issue", "list"] and words[2:10] == ["--label", "stall-alert", "--state", "open", "--limit", "100",
                                                         "--json", "number,title"]:
    print(json.dumps([dict(number=i["number"], title=i["title"]) for i in issues
                      if i["state"] == "open" and "stall-alert" in i["labels"]]))
elif words[:2] == ["issue", "create"]:
    issues.append(dict(number=len(issues) + 1, title=value("--title"), labels=[value("--label")],
                       body=value("--body"), state="open"))
    json.dump(issues, open(db, "w"))
    if os.environ.get("FAKE_GH_LOSE_CREATE"):
        sys.exit(1)
    print(f"https://github.com/example/repo/issues/{len(issues)}")
elif words[:2] == ["issue", "close"]:
    issues[int(words[2]) - 1].update(state="closed", comment=value("--comment"))
else:
    sys.exit(1)
json.dump(issues, open(db, "w"))
"""

FAKE_SYSTEMCTL = """#!/bin/sh
case " $FAKE_SYSTEMCTL_REMOVED " in *" $6 "*) echo not-found ;; *) echo loaded ;; esac
"""


def entry(hours_ago, unit, message):
    when = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return json.dumps({"__REALTIME_TIMESTAMP": str(int(when.timestamp() * 1e6)), "_SYSTEMD_USER_UNIT": unit,
                       "MESSAGE": message if isinstance(message, str) else json.dumps(message)})


def journal(hours, imported=0, proven=0, scanned=0, github_failed=(9,), coldbox_missing=True,
            transcripts_imported=0, github_failure="failed", quiet=0, github=True, daily_unit=COLLECT):
    lines = []
    for index, hour in enumerate(range(hours, quiet, -1)):
        failed = github_failed[index % len(github_failed)]
        collect = dict(github={github_failure: failed, "imported": imported, "skipped": 100},
                       sources={"runner-a": dict(imported=0, deduped=300, unavailable=0)})
        collect["coldbox"] = dict(missing="/runtime/no-local-receipts") if coldbox_missing else dict(imported=0, rejected=0)
        if not github:
            del collect["github"]
        lines += [entry(hour, "init.scope", "Starting coldfix-collect.service"), entry(hour, COLLECT, collect),
                  entry(hour, COLLECT, "learn_daily replay " + json.dumps(dict(proven=proven))),
                  entry(hour, daily_unit, dict(day="2026-11-06", replay=dict(proven=proven, delivered=0))),
                  entry(hour, TRANSCRIPTS, dict(errors=0, imported=transcripts_imported, scanned=scanned, proven=proven))]
    return "\n".join(lines) + "\n"


def result(hours_ago, unit, outcome):
    when = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return json.dumps({"__REALTIME_TIMESTAMP": str(int(when.timestamp() * 1e6)), "_SYSTEMD_USER_UNIT": "init.scope",
                       "USER_UNIT": unit, "JOB_TYPE": "start", "JOB_RESULT": outcome, "MESSAGE": f"{outcome} {unit}"})


class LearnStallTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.note, self.state = self.root / "agentfleet.md", self.root / "state" / "stall.json"

    def check(self, text, deliver=None, issues=(), env=None):
        command = deliver or ["sh", "-c", 'cat >> "$0"', str(self.note)]
        systemd = self.root / "systemd"
        systemd.mkdir(exist_ok=True)
        (systemd / "systemctl").write_text(FAKE_SYSTEMCTL)
        (systemd / "systemctl").chmod(0o755)
        env = dict(env or os.environ)
        env["PATH"] = f"{systemd}{os.pathsep}{env['PATH']}"
        return subprocess.run([sys.executable, str(SCRIPT), "--state", str(self.state), *issues, "--", *command],
                              input=text, text=True, capture_output=True, env=env)

    def issues(self):
        return json.loads(self.db.read_text()) if self.db.exists() else []

    def gh_env(self, **extra):
        bin_dir, self.db = self.root / "bin", self.root / "issues.json"
        bin_dir.mkdir(exist_ok=True)
        (bin_dir / "gh").write_text("#!" + sys.executable + "\n" + FAKE_GH)
        (bin_dir / "gh").chmod(0o755)
        return dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}", FAKE_GH_DB=str(self.db), **extra)

    def notes(self):
        return self.note.read_text() if self.note.exists() else ""

    def test_each_stall_alerts_once_then_recovers(self):
        self.assertEqual(self.check(journal(25)).returncode, 0)
        stalled = [line for line in self.notes().splitlines() if line.startswith(f"STALLED coldfix on {platform.node()} ")]
        for reason in ("0 failures imported from every source in 6 h", "0 lessons proven in 24 h",
                       "0 transcripts scanned in 6 h", "0 transcript lessons proven in 24 h",
                       "github failed in 3 collect runs in a row", "coldbox failed in 3 collect runs in a row"):
            self.assertEqual(sum(reason in line for line in stalled), 1, reason)
        self.assertEqual(len(stalled), 6)
        self.assertEqual(self.notes().count("· agentfleet-alert\n"), 6)
        before = self.notes()
        self.assertEqual(self.check(journal(25)).returncode, 0)
        self.assertEqual(self.notes(), before)
        healthy = journal(25, imported=1, proven=1, scanned=4, github_failed=(0,), coldbox_missing=False)
        self.assertEqual(self.check(healthy).returncode, 0)
        recovered = self.notes()[len(before):]
        self.assertEqual(recovered.count(f"RECOVERED coldfix on {platform.node()} "), 6)
        self.assertNotIn("STALLED", recovered)
        self.assertEqual(json.loads(self.state.read_text())["active"], {})

    def test_lessons_proven_but_undelivered_for_24_hours_alert_until_a_run_reports_none(self):
        healthy = journal(25, imported=1, proven=1, scanned=4, github_failed=(0,), coldbox_missing=False)
        summary = lambda overdue: entry(.5, COLLECT, dict(check_delivery=dict(proven=26, delivered=23, overdue=overdue)))
        self.assertEqual(self.check(healthy + summary(0) + "\n").returncode, 0)
        self.assertEqual(self.notes(), "")
        self.assertEqual(self.check(healthy + entry(2, COLLECT, dict(check_delivery=dict(overdue=0))) + "\n"
                                    + summary(3) + "\n").returncode, 0)
        self.assertEqual(self.notes().count("STALLED"), 1)
        self.assertIn("3 proven lessons are still prose only, with no check in their repository, 24 h after proof",
                      self.notes())
        before = self.notes()
        self.assertEqual(self.check(healthy).returncode, 0)
        self.assertEqual(self.notes(), before)
        self.assertEqual(self.check(healthy + summary(3) + "\n" + entry(.2, COLLECT, dict(check_delivery=dict(overdue=0)))
                                    + "\n").returncode, 0)
        self.assertIn("RECOVERED", self.notes()[len(before):])
        self.assertEqual(json.loads(self.state.read_text())["active"], {})

    def test_unreachable_hosts_alert_only_after_24_hours_without_success(self):
        healthy = journal(25, imported=1, proven=1, scanned=4, github_failed=(0,), coldbox_missing=False)
        skip = [entry(.1, SYNC, "sync skipped, retry next run: mac"),
                entry(.1, SYNC, "synced windows"),
                entry(.1, TRANSCRIPTS, "transcript pull skipped, retry next run: compute to vm")]
        recent = [entry(23, SYNC, "synced mac"), entry(23, TRANSCRIPTS, "transcript pull synced compute to vm")]
        old = [entry(24.1, SYNC, "synced mac"),
               entry(24.1, TRANSCRIPTS, "transcript pull skipped, retry next run: compute to vm")]
        self.assertEqual(self.check(healthy + "\n".join(recent + skip) + "\n").returncode, 0)
        self.assertEqual(self.notes(), "")
        overdue = healthy + "\n".join(old + skip) + "\n"
        self.assertEqual(self.check(overdue).returncode, 0)
        self.assertEqual(self.notes().count("STALLED"), 2)
        self.assertIn("Lessons sync (mac): host unreachable", self.notes())
        self.assertIn("Transcript pull (compute to vm): host unreachable", self.notes())
        self.assertNotIn("Lessons sync (windows)", self.notes())
        self.assertEqual(self.check(overdue).returncode, 0)
        self.assertEqual(self.notes().count("STALLED"), 2)
        recover = [entry(.01, SYNC, "synced mac"), entry(.01, TRANSCRIPTS, "transcript pull synced compute to vm")]
        self.assertEqual(self.check(overdue + "\n".join(recover) + "\n").returncode, 0)
        self.assertEqual(self.notes().count("RECOVERED"), 2)

    def test_skipped_pull_does_not_raise_the_six_hour_scan_alert(self):
        base = journal(25, imported=1, proven=1, scanned=0, github_failed=(0,), coldbox_missing=False)
        self.assertEqual(self.check(base + entry(.1, TRANSCRIPTS, "transcript pull skipped, retry next run: compute")
                                    + "\n").returncode, 0)
        self.assertEqual(self.notes(), "")

    def test_source_status_uses_transfer_events_and_the_service_substate(self):
        fake = self.root / "systemctl"
        fake.write_text(f"""#!/bin/sh
cat <<'EOF'
Id={COLLECT}
ActiveState=activating
SubState=auto-restart

Id={TRANSCRIPTS}
ActiveState=activating
SubState=start

Id=coldfix-oss.timer
ActiveState=inactive
SubState=dead
EOF
""")
        fake.chmod(0o755)
        events = [entry(.1, SYNC, "sync skipped, retry next run: mac"),
                  entry(.2, OSS, "ci-backlog paused: 10 consecutive batches proved 0 lessons")]
        result = subprocess.run([sys.executable, str(SCRIPT), "--sources"], input="\n".join(events), text=True,
                                capture_output=True, check=True,
                                env={**os.environ, "PATH": f"{self.root}{os.pathsep}{os.environ['PATH']}"})
        values = json.loads(result.stdout)
        self.assertEqual(values["Hourly collection"], "Idle. Restart scheduled")
        self.assertEqual(values["Transcripts"], "Running")
        self.assertEqual(values["OSS"], "Paused: 10 consecutive batches proved 0 lessons")
        self.assertEqual(values["Lessons sync (mac)"], "Idle. Host unreachable. Skipped, retry next run")

    def test_a_paused_transcript_backlog_or_oss_run_alerts_until_a_later_run_is_not_paused(self):
        held = [entry(.5, TRANSCRIPTS, dict(paused="3 batches in a row proved nothing; resumes at 2026-11-09T00:00:00+00:00")),
                entry(.5, BACKLOG, "ci-backlog paused until 2026-11-09T00:00:00+00:00: runner offline"),
                entry(.5, OSS, "ci-backlog paused until 2026-11-09T00:00:00+00:00: 3 batches in a row proved 0")]
        healthy = journal(25, imported=1, proven=1, scanned=4, github_failed=(0,), coldbox_missing=False)
        self.assertEqual(self.check(healthy + "\n".join(held) + "\n").returncode, 0)
        stalled = [line for line in self.notes().splitlines() if line.startswith("STALLED")]
        self.assertEqual(len(stalled), 3, stalled)
        for unit, reason in (("coldfix-transcripts", "3 batches in a row proved nothing"),
                             ("coldfix-backlog", "runner offline"), ("coldfix-oss", "3 batches in a row proved 0")):
            self.assertEqual(sum(f"{unit} paused" in line and reason in line for line in stalled), 1, unit)
        ran = [entry(.2, TRANSCRIPTS, dict(errors=0, imported=0, scanned=5)),
               entry(.2, BACKLOG, "ci-backlog batch 9: considered 3 failures, imported 0 of 0 fix pairs"),
               entry(.2, OSS, "ci-backlog: no new fix pairs left in GitHub CI history")]
        before = self.notes()
        self.assertEqual(self.check(healthy + "\n".join(held + ran) + "\n").returncode, 0)
        recovered = self.notes()[len(before):]
        self.assertEqual(recovered.count("RECOVERED"), 3, recovered)
        self.assertNotIn("STALLED", recovered)

    def test_each_active_alert_keeps_exactly_one_open_issue_until_it_recovers(self):
        env, issues = self.gh_env(), ["--issues", "example/repo"]
        prefix = f"STALLED coldfix on {platform.node()}: "
        self.assertEqual(self.check(journal(25), issues=issues, env=env).returncode, 0)
        opened = {issue["title"]: issue for issue in self.issues()}
        self.assertEqual(len(self.issues()), 6)
        self.assertEqual(set(opened), {prefix + key for key in json.loads(self.state.read_text())["active"]})
        self.assertEqual({tuple(issue["labels"]) for issue in self.issues()}, {("stall-alert",)})
        self.assertIn("github failed in 3 collect runs in a row", opened[prefix + "failing:collect:github"]["body"])
        self.assertEqual(self.check(journal(25), issues=issues, env=env).returncode, 0)
        self.assertEqual(self.issues(), list(opened.values()))
        healthy = journal(25, imported=1, proven=1, scanned=4, github_failed=(0,), coldbox_missing=False)
        self.assertEqual(self.check(healthy, issues=issues, env=env).returncode, 0)
        self.assertEqual(len(self.issues()), 6)
        self.assertEqual({issue["state"] for issue in self.issues()}, {"closed"})
        self.assertTrue(all(issue["comment"].startswith("RECOVERED") for issue in self.issues()))

    def test_every_failed_coldfix_unit_run_files_one_issue_until_a_later_run_succeeds(self):
        env, issues = self.gh_env(), ["--issues", "example/repo"]
        healthy = journal(25, imported=1, proven=1, scanned=4, github_failed=(0,), coldbox_missing=False)
        failing = [entry(.6, TRANSCRIPTS, "transcript_lessons.py: error: set COLDFIX_LESSONS_DIR to the shared lessons folder"),
                   result(.6, TRANSCRIPTS, "failed"), result(.5, "coldfix-deploy.service", "failed"),
                   result(.4, "coldfix-state-backup.service", "done"), result(.3, "other.service", "failed"),
                   result(.3, "coldfix-lessons-sync.service", "dependency"), result(.3, "coldfix-recheck.service", "skipped")]
        self.assertEqual(self.check(healthy + "\n".join(failing) + "\n", issues=issues, env=env).returncode, 0)
        titles = {issue["title"]: issue for issue in self.issues()}
        prefix = f"STALLED coldfix on {platform.node()}: "
        self.assertEqual(set(titles), {prefix + "failed:coldfix-transcripts", prefix + "failed:coldfix-deploy",
                                       prefix + "failed:coldfix-lessons-sync"})
        self.assertIn("set COLDFIX_LESSONS_DIR to the shared lessons folder", titles[prefix + "failed:coldfix-transcripts"]["body"])
        again = failing + [result(.2, TRANSCRIPTS, "failed")]
        self.assertEqual(self.check(healthy + "\n".join(again) + "\n", issues=issues, env=env).returncode, 0)
        self.assertEqual(len(self.issues()), 3)
        fixed = again + [result(.1, TRANSCRIPTS, "done")]
        self.assertEqual(self.check(healthy + "\n".join(fixed) + "\n", issues=issues, env=env).returncode, 0)
        state = {issue["title"]: issue["state"] for issue in self.issues()}
        self.assertEqual(state, {prefix + "failed:coldfix-transcripts": "closed", prefix + "failed:coldfix-deploy": "open",
                                 prefix + "failed:coldfix-lessons-sync": "open"})

    def test_a_failed_unit_alert_recovers_once_systemd_no_longer_has_the_unit(self):
        env, issues = self.gh_env(), ["--issues", "example/repo"]
        healthy = journal(25, imported=1, proven=1, scanned=4, github_failed=(0,), coldbox_missing=False)
        failed = healthy + result(.5, "coldfix-before-after.service", "failed") + "\n"
        self.assertEqual(self.check(failed, issues=issues, env=env).returncode, 0)
        self.assertEqual([issue["state"] for issue in self.issues()], ["open"])
        self.assertEqual(self.check(healthy, issues=issues, env=env).returncode, 0)
        self.assertEqual([issue["state"] for issue in self.issues()], ["open"])
        env["FAKE_SYSTEMCTL_REMOVED"] = "coldfix-before-after.service"
        self.assertEqual(self.check(failed, issues=issues, env=env).returncode, 0)
        self.assertEqual([issue["state"] for issue in self.issues()], ["closed"])
        self.assertIn("RECOVERED", self.notes())

    def test_issue_tracking_retries_after_a_failed_gh_call_and_replaces_a_closed_issue(self):
        issues = ["--issues", "example/repo"]
        failed = self.check(journal(25), issues=issues, env=self.gh_env(FAKE_GH_FAIL="1"))
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("issue tracking failed", failed.stderr)
        self.assertEqual(self.issues(), [])
        self.assertEqual(self.notes().count("STALLED"), 6)
        self.assertEqual(len(json.loads(self.state.read_text())["active"]), 6)
        env = self.gh_env()
        self.assertEqual(self.check(journal(25), issues=issues, env=env).returncode, 0)
        self.assertEqual(self.notes().count("STALLED"), 6)
        self.assertEqual(len(self.issues()), 6)
        closed = self.issues()
        closed[0]["state"] = "closed"
        self.db.write_text(json.dumps(closed))
        self.assertEqual(self.check(journal(25), issues=issues, env=env).returncode, 0)
        self.assertEqual([issue["state"] for issue in self.issues()], ["closed"] + ["open"] * 6)
        self.assertEqual(self.issues()[6]["title"], self.issues()[0]["title"])

    def test_a_failed_note_delivery_still_opens_the_issues(self):
        issues, env = ["--issues", "example/repo"], self.gh_env()
        self.assertNotEqual(self.check(journal(25), deliver=["false"], issues=issues, env=env).returncode, 0)
        self.assertFalse(self.state.exists())
        self.assertEqual([issue["state"] for issue in self.issues()], ["open"] * 6)
        self.assertEqual(self.check(journal(25), issues=issues, env=env).returncode, 0)
        self.assertEqual(self.notes().count("STALLED"), 6)
        self.assertEqual(len(self.issues()), 6)

    def test_an_issue_created_without_a_recorded_number_is_adopted_not_duplicated(self):
        issues = ["--issues", "example/repo"]
        lost = self.check(journal(25), issues=issues, env=self.gh_env(FAKE_GH_LOSE_CREATE="1"))
        self.assertNotEqual(lost.returncode, 0)
        self.assertEqual(len(self.issues()), 1)
        self.assertEqual(self.check(journal(25), issues=issues, env=self.gh_env()).returncode, 0)
        self.assertEqual(sorted(issue["title"] for issue in self.issues()),
                         sorted(f"STALLED coldfix on {platform.node()}: {key}"
                                for key in json.loads(self.state.read_text())["active"]))

    def test_an_unjudged_alert_keeps_its_issue_after_a_failed_note_delivery(self):
        issues, env = ["--issues", "example/repo"], self.gh_env()
        self.assertNotEqual(self.check(journal(25), deliver=["false"], issues=issues, env=env).returncode, 0)
        self.assertEqual(self.check(journal(2, imported=1, proven=1, scanned=4, github_failed=(0,), coldbox_missing=False),
                                    issues=issues, env=env).returncode, 0)
        self.assertEqual([issue["state"] for issue in self.issues()], ["open"] * 6)

    def test_short_journal_or_a_broken_streak_does_not_alert(self):
        self.assertEqual(self.check(journal(5, github_failed=(9, 9, 0, 9, 9), coldbox_missing=False)).returncode, 0)
        self.assertEqual(self.notes(), "")
        self.assertEqual(json.loads(self.state.read_text())["active"], {})

    def test_failed_delivery_resends_the_alert_on_the_next_run(self):
        self.assertNotEqual(self.check(journal(25), deliver=["false"]).returncode, 0)
        self.assertFalse(self.state.exists())
        self.assertEqual(self.check(journal(25)).returncode, 0)
        self.assertEqual(self.notes().count("STALLED"), 6)

    def test_transcript_imports_github_discovery_and_short_journals(self):
        self.assertEqual(self.check(journal(25, transcripts_imported=3)).returncode, 0)
        self.assertNotIn("0 failures imported", self.notes())
        self.assertIn("0 transcripts scanned in 6 h", self.notes())
        self.assertEqual(self.check(journal(25)).returncode, 0)
        self.assertIn("0 failures imported from every source in 6 h", self.notes())
        before = self.notes()
        self.assertEqual(self.check(journal(2, imported=0, github_failed=(0,), coldbox_missing=False)).returncode, 0)
        self.assertNotIn("0 failures imported", self.notes()[len(before):])
        self.assertNotIn("0 transcripts scanned", self.notes()[len(before):])
        self.assertEqual(self.check(journal(25, github_failed=("CalledProcessError",),
                                            github_failure="discovery_failed")).returncode, 0)
        self.assertIn("github failed in 3 collect runs in a row", self.notes())

    def test_stopped_collector_alerts_and_short_journal_keeps_streak_alerts(self):
        healthy = dict(imported=1, proven=1, scanned=4, github_failed=(0,), coldbox_missing=False)
        self.assertEqual(self.check(journal(25, quiet=7, **healthy)).returncode, 0)
        self.assertIn("0 failures imported from every source in 6 h", self.notes())
        self.assertIn("0 transcripts scanned in 6 h", self.notes())
        self.assertEqual(self.check(journal(25, proven=1, scanned=4, coldbox_missing=False)).returncode, 0)
        self.assertIn("github failed in 3 collect runs in a row", self.notes())
        before = self.notes()
        self.assertEqual(self.check(journal(2, **healthy)).returncode, 0)
        self.assertNotIn("RECOVERED", self.notes()[len(before):])
        self.assertIn("failing:collect:github", json.loads(self.state.read_text())["active"])

    def test_journal_reach_decides_whether_silent_units_alert(self):
        self.assertEqual(self.check("").returncode, 0)
        self.assertEqual(self.notes(), "")
        self.state.write_text(json.dumps(dict(since=(datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(),
                                              active={})))
        self.assertEqual(self.check(journal(3, github_failed=(0,), coldbox_missing=False)).returncode, 0)
        self.assertEqual(self.notes(), "")
        self.assertEqual(self.check(entry(30, "init.scope", "Started session") + "\n").returncode, 0)
        for reason in ("0 failures imported from every source in 6 h", "0 lessons proven in 24 h",
                       "0 transcripts scanned in 6 h", "0 transcript lessons proven in 24 h"):
            self.assertIn(reason, self.notes())

    def test_a_source_absent_from_later_runs_keeps_its_alert_and_daily_unit_proofs_count(self):
        self.assertEqual(self.check(journal(25)).returncode, 0)
        before = self.notes()
        self.assertEqual(self.check(journal(25, github=False, proven=1, daily_unit=DAILY)).returncode, 0)
        after = self.notes()[len(before):]
        self.assertNotIn("github failed in 3 collect runs in a row no longer holds", after)
        self.assertIn("0 lessons proven in 24 h no longer holds", after)
        self.assertIn("failing:collect:github", json.loads(self.state.read_text())["active"])

    def test_a_source_still_failing_in_the_latest_run_keeps_its_alert(self):
        self.assertEqual(self.check(journal(25)).returncode, 0)
        before = self.notes()
        sources = dict(runner_01=dict(imported=0, unavailable=0))
        gap = [entry(0.6, COLLECT, dict(sources=sources, coldbox=dict(missing="/runtime/no-local-receipts"))),
               entry(0.3, COLLECT, dict(sources=sources, github=dict(failed=9, imported=0),
                                        coldbox=dict(missing="/runtime/no-local-receipts")))]
        self.assertEqual(self.check(journal(25) + "\n".join(gap) + "\n").returncode, 0)
        self.assertNotIn("github failed in 3 collect runs in a row no longer holds", self.notes()[len(before):])
        self.assertIn("failing:collect:github", json.loads(self.state.read_text())["active"])


if __name__ == "__main__":
    unittest.main()
