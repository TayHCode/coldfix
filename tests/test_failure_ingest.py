"""Sanitized failed-run and blocker CLI proofs. No network or credentials."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import coldfix as c
import failure_ingest as ingest
from status_summary import event_source


ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "failure_ingest.py"
FIXTURES = ROOT / "tests" / "fixtures"


def fixture(name="failed-ci-run.json"):
    return json.loads((FIXTURES / name).read_text())


class FailureIngestTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        self.store = self.work / ".coldfix"
        self.input = self.work / "input.json"

    def cli(self, *args, env=None):
        return subprocess.run([sys.executable, str(ENTRY), "--store", str(self.store), *args],
                              capture_output=True, text=True, env=env)

    def invoke(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = ingest.main(["--store", str(self.store), *args])
        return code, out.getvalue(), err.getvalue()

    def write(self, value):
        self.input.write_text(json.dumps(value))
        return ["json", "--input", str(self.input)]

    def records(self):
        return list(c.load_store(self.store)[1]["failures"].values())

    def test_json_retains_ci_metadata_and_is_idempotent(self):
        args = self.write(fixture())
        first = self.cli(*args)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(json.loads(first.stdout), dict(imported=1, skipped=0))
        row = self.records()[0]
        before = {p: p.read_bytes() for p in self.store.rglob("*.json")}
        self.assertEqual(row["version"], 2)
        self.assertEqual(row["kind"], "ci")
        self.assertEqual(row["source_ref"], "https://github.com/example/project/actions/runs/412#attempt-1")
        self.assertEqual(event_source(row), "github")
        metadata = json.loads(row["error"])
        for key, value in fixture().items():
            self.assertEqual(metadata[key], value)
        self.assertEqual(metadata["classification"]["category"], "real-bug")
        again = self.cli(*args)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(json.loads(again.stdout), dict(imported=0, skipped=1))
        self.assertEqual(before, {p: p.read_bytes() for p in self.store.rglob("*.json")})
        self.assertEqual(self.store.stat().st_mode & 0o777, 0o700)
        for path in before:
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.store / "outcomes").exists())

    def test_historical_source_label_preserves_plain_text_grouping(self):
        event = ingest.ci_event(fixture())
        event["error"] = "legacy plain text diagnostic"
        historical = c.event_record(event, "failure-ingest")
        same = dict(historical, source="other-history")
        self.assertEqual(c.signature(historical), c.signature(same))
        c.save(self.store, "failures", historical)
        grouped = subprocess.run([sys.executable, str(ROOT / "coldfix.py"), "--store", str(self.store), "status", "--json"],
                                 capture_output=True, text=True)
        self.assertEqual(grouped.returncode, 0, grouped.stderr)
        self.assertEqual(json.loads(grouped.stdout)["totals"]["events"], 1)

    def test_recurrence_ignores_run_and_thread_metadata(self):
        first = fixture()
        second = dict(first, run_id=999, duration_seconds=200, runner="runner-a", attempt=2)
        blocker = fixture("t3-blocker.json")
        other = dict(blocker, thread_id="another-thread", time_spent_seconds=999, model="another-model")
        result = self.cli(*self.write([first, second, blocker, other]))
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = self.records()
        ci = [r for r in rows if r["kind"] == "ci"]
        t3 = [r for r in rows if r["kind"] == "stuck"]
        self.assertNotEqual(ci[0]["group_id"], ci[1]["group_id"])
        self.assertEqual(c.signature(ci[0]), c.signature(ci[1]))
        self.assertEqual(c.signature(t3[0]), c.signature(t3[1]))
        different = ingest.record(ingest.ci_event(dict(second, run_id=998, log_tail="AssertionError: different failure")))
        self.assertNotEqual(c.signature(ci[0]), c.signature(different))
        grouped = subprocess.run([sys.executable, str(ROOT / "coldfix.py"), "--store", str(self.store), "group"],
                                 capture_output=True, text=True)
        self.assertEqual(grouped.returncode, 0, grouped.stderr)
        self.assertEqual(json.loads(grouped.stdout)["members"], 4)
        self.assertEqual(json.loads(grouped.stdout)["groups"], 2)

    def test_saved_network_and_flake_samples(self):
        for name, category in (("failed-ci-network.json", "env-infra"), ("failed-ci-flake.json", "flake")):
            with self.subTest(name=name):
                result = self.cli(*self.write(fixture(name)))
                self.assertEqual(result.returncode, 0, result.stderr)
                row = next(r for r in self.records() if json.loads(r["error"])["run_id"] == fixture(name)["run_id"])
                self.assertEqual(json.loads(row["error"])["classification"]["category"], category)

    def test_t3_partial_and_blocked_preserve_caller_attestation(self):
        blocked = fixture("t3-blocker.json")
        partial = dict(blocked, status="partial")
        result = self.cli(*self.write([blocked, partial]))
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = self.records()
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row["kind"], "stuck")
            self.assertEqual(row["thread"], "example-thread")
            self.assertEqual(event_source(row), "t3")
            meta = json.loads(row["error"])
            self.assertEqual(meta["classification"], dict(category="agent-mistake", basis="caller-attested blocker category", rules=[]))
            self.assertEqual(meta["coldfix_fixable"], True)
            self.assertEqual(meta["human_needed"], False)
            self.assertEqual(meta["what_tried"], blocked["what_tried"])
        status = subprocess.run([sys.executable, str(ROOT / "coldfix.py"), "--store", str(self.store), "status", "--json"],
                                capture_output=True, text=True)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout)["by_source"]["t3"]["events"], 2)
        grouped = subprocess.run([sys.executable, str(ROOT / "coldfix.py"), "--store", str(self.store), "group"],
                                 capture_output=True, text=True)
        self.assertEqual(grouped.returncode, 0, grouped.stderr)
        self.assertEqual(json.loads(grouped.stdout)["members"], 2)

    def test_classifier_conservative_rules_and_same_head_retry(self):
        cases = [("No space left on device", "env-infra"), ("npm ERR! ERESOLVE unable to resolve", "dependency"),
                 ("AssertionError: expected 2", "real-bug"), ("failed exit 1", "unknown"),
                 ("tests\tRun tests\t2026-10-20T12:00:00.000Z AssertionError: expected 2", "real-bug"),
                 ("flake? retrying", "unknown"), ("AssertionError: ERESOLVE", "unknown")]
        for i, (log, expected) in enumerate(cases):
            with self.subTest(log=log):
                result = self.cli(*self.write(dict(fixture(), run_id=500 + i, log_tail=log)))
                self.assertEqual(result.returncode, 0, result.stderr)
                row = next(row for row in self.records() if json.loads(row["error"])["run_id"] == 500 + i)
                self.assertEqual(json.loads(row["error"])["classification"]["category"], expected)
        retry = dict(attempt=2, head_sha="a" * 40, conclusion="success",
                     url="https://github.com/example/project/actions/runs/412/attempts/2")
        self.assertEqual(self.cli(*self.write(dict(fixture(), retry_evidence=retry))).returncode, 0)
        row = next(row for row in self.records() if json.loads(row["error"])["run_id"] == 412)
        self.assertEqual(json.loads(row["error"])["classification"]["category"], "flake")

    def test_invalid_batch_writes_nothing_and_never_echoes_input(self):
        sentinel = "sensitive-example-do-not-echo"
        invalids = [dict(fixture(), run_id=True), dict(fixture(), head_sha="bad"),
                    dict(fixture(), duration_seconds=-1), dict(fixture(), duration_seconds=float("nan")),
                    dict(fixture(), attempt=0), dict(fixture(), pr=True), dict(fixture(), conclusion="cancelled"),
                    dict(fixture(), repo="../bad/repo"), dict(fixture(), occurred="2026-02-30T12:00:00Z"),
                    dict(fixture(), occurred="2026-10-20"), dict(fixture(), workflow="bad\nline"),
                    dict(fixture(), extra=sentinel),
                    dict(fixture("t3-blocker.json"), human_needed="false"),
                    dict(fixture("t3-blocker.json"), what_tried=[42]),
                    dict(fixture("t3-blocker.json"), evidence_links=["https://user:" + "password@example.invalid"]),
                    dict(fixture("t3-blocker.json"), status="completed"),
                    dict(fixture("t3-blocker.json"), blocker_category="maybe"), []]
        for invalid in invalids:
            with self.subTest(invalid=type(invalid).__name__):
                result = self.cli(*self.write([dict(fixture(), log_tail=sentinel), invalid]))
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertNotIn(sentinel, result.stderr)
                self.assertFalse(self.store.exists())
        self.input.write_text('{"type":"ci","type":"t3"}')
        self.assertEqual(self.cli("json", "--input", str(self.input)).returncode, 2)

    def test_tail_bound_and_retries_are_distinct(self):
        base = fixture()
        result = self.cli(*self.write([dict(base, log_tail="s" * 9000), dict(base, attempt=2)]))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.records()), 2)
        first = next(row for row in self.records() if "#attempt-1" in row["source_ref"])
        self.assertEqual(json.loads(first["error"])["log_tail"], "s" * 8000)

    def test_conflicting_identity_rejects_entire_batch_without_changes(self):
        self.assertEqual(self.cli(*self.write(fixture())).returncode, 0)
        before = {p: p.read_bytes() for p in self.store.rglob("*.json")}
        result = self.cli(*self.write([dict(fixture(), run_id=413), dict(fixture(), log_tail="different")]))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(before, {p: p.read_bytes() for p in self.store.rglob("*.json")})

    def test_retry_after_second_write_interruption(self):
        args = self.write([fixture(), fixture("t3-blocker.json")])
        original = c.save
        writes = []

        def interrupted(root, collection, row):
            writes.append(row["id"])
            if len(writes) == 2:
                raise OSError("sensitive-example")
            original(root, collection, row)

        with patch.object(c, "save", side_effect=interrupted):
            code, out, err = self.invoke(*args)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertNotIn("sensitive-example", err)
        self.assertEqual(len(self.records()), 1)
        result = self.cli(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), dict(imported=1, skipped=1))
        self.assertEqual(len(self.records()), 2)
        self.assertEqual(json.loads(self.cli(*args).stdout), dict(imported=0, skipped=2))

    def test_concurrent_cli_imports_converge(self):
        args = self.write([fixture(), fixture("t3-blocker.json")])
        command = [sys.executable, str(ENTRY), "--store", str(self.store), *args]
        processes = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(4)]
        outputs = [process.communicate(timeout=20) for process in processes]
        self.assertEqual([process.returncode for process in processes], [0, 0, 0, 0])
        self.assertEqual(sum(json.loads(out)["imported"] for out, _ in outputs), 2)
        self.assertEqual(sum(json.loads(out)["skipped"] for out, _ in outputs), 6)
        self.assertEqual(len(self.records()), 2)

    def test_retry_after_publish_before_success_response(self):
        args = self.write(fixture())
        original = c.save

        def after_publish(root, collection, row):
            original(root, collection, row)
            raise OSError("sensitive-example")

        with patch.object(c, "save", side_effect=after_publish):
            self.assertEqual(self.invoke(*args)[0], 2)
        self.assertEqual(len(self.records()), 1)
        result = self.cli(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), dict(imported=0, skipped=1))

    def test_public_or_symlink_store_is_rejected(self):
        args = self.write(fixture())
        self.store.mkdir(mode=0o755)
        self.store.chmod(0o755)
        self.assertEqual(self.cli(*args).returncode, 2)
        self.assertEqual(list(self.store.iterdir()), [])
        self.store.rmdir()
        actual = self.work / "actual"
        actual.mkdir(mode=0o700)
        self.store.symlink_to(actual, target_is_directory=True)
        self.assertEqual(self.cli(*args).returncode, 2)
        self.assertEqual(list(actual.iterdir()), [])

    def fake_github(self, *, error=False):
        run = dict(id=412, status="completed", conclusion="failure", repository=dict(id=7, full_name="example/project"),
                   run_attempt=1, name="verify", head_sha="a" * 40, pull_requests=[dict(number=17)],
                   run_started_at="2026-10-20T11:58:00Z", updated_at="2026-10-20T12:00:00Z")
        job = dict(id=888, run_id=412, status="completed", conclusion="failure", name="unit tests", runner_name="example-runner",
                   started_at="2026-10-20T11:58:30Z", completed_at="2026-10-20T12:00:00Z")
        data = self.work / "github.json"
        data.write_text(json.dumps(dict(run=run, jobs=dict(total_count=1, jobs=[job]), error=error)))
        calls = self.work / "calls.jsonl"
        executable = self.work / "gh"
        executable.write_text('''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
a = sys.argv[1:]
d = json.loads(Path(os.environ["FAKE_GITHUB"]).read_text())
with Path(os.environ["FAKE_CALLS"]).open("a") as f:
    f.write(json.dumps(a) + "\\n")
if d["error"]:
    print("sensitive-example", file=sys.stderr)
    sys.exit(1)
if a[:2] == ["run", "list"]:
    print(json.dumps(d.get("runs", [dict(databaseId=412, attempt=d["run"]["run_attempt"], conclusion="failure")])))
elif a[:2] == ["run", "view"]:
    if d.get("log_error"):
        print(d["log_error"], file=sys.stderr)
        sys.exit(1)
    print(d.get("log", "AssertionError: expected 2, got 3"))
elif a[0] == "api" and a[-1].endswith("/logs"):
    job = a[-1].split("/")[-2]
    if job not in d.get("job_logs", {}):
        print(d.get("job_log_error", "HTTP 404"), file=sys.stderr)
        sys.exit(1)
    print(d["job_logs"][job])
elif a[0] == "api" and "/annotations?" in a[-1]:
    page = int(a[-1].rsplit("page=", 1)[1])
    print(json.dumps(d["annotations"].get(a[-1].split("/")[-2], [])[(page - 1) * 100:page * 100]))
elif a[0] == "api" and a[-1].endswith("/412"):
    print(json.dumps(d.get("latest", d["run"])))
elif a[0] == "api" and "/412/attempts/" in a[-1] and "/jobs" not in a[-1]:
    print(json.dumps(d["run"]))
elif a[0] == "api" and "/jobs?" in a[-1]:
    print(json.dumps(d["jobs"]))
else:
    sys.exit(3)
''')
        executable.chmod(0o700)
        env = dict(os.environ, PATH=str(self.work) + os.pathsep + os.environ["PATH"], FAKE_GITHUB=str(data), FAKE_CALLS=str(calls))
        return env, data, calls

    def test_empty_saved_log_cannot_reserve_completed_identity(self):
        for log in ("", " \n", "diagnostic" + " " * ingest.TAIL):
            with self.subTest(length=len(log)):
                result = self.cli(*self.write(dict(fixture(), log_tail=log)))
                self.assertEqual(result.returncode, 2)
                self.assertFalse(self.store.exists())

    def test_empty_remote_log_is_retryable_without_import(self):
        run = dict(id=412, status="completed", conclusion="failure", repository=dict(full_name="example/project"),
                   run_attempt=1, name="verify", head_sha="a" * 40, pull_requests=[],
                   run_started_at="2026-10-20T11:58:00Z", updated_at="2026-10-20T12:00:00Z")
        with patch.object(ingest.GitHub, "api", return_value=run), patch.object(ingest.GitHub, "pages", return_value=[]), patch.object(ingest, "gh", return_value=" \n"):
            code, out, err = self.invoke("github", "--repo", "example/project", "--run-id", "412")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertFalse(self.store.exists())

    def test_github_cli_fetch_retains_runner_pr_job_and_attempt(self):
        env, _, calls = self.fake_github()
        result = self.cli("github", "--repo", "Example/Project", "--run-id", "412", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        meta = json.loads(self.records()[0]["error"])
        self.assertEqual(meta["duration_seconds"], 120)
        self.assertEqual(meta["jobs"], [dict(id=888, name="unit tests", runner="example-runner", duration_seconds=90, conclusion="failure")])
        self.assertEqual(meta["pr"], 17)
        self.assertEqual(meta["attempt"], 1)
        self.assertEqual(meta["classification"]["category"], "real-bug")
        requests = [json.loads(line) for line in calls.read_text().splitlines()]
        self.assertEqual(len(requests), 3)
        self.assertEqual(requests[-1], ["run", "view", "412", "--repo", "github.com/example/project", "--attempt", "1", "--log-failed"])
        self.assertTrue(all("GET" in request for request in requests if request[0] == "api"))

    def test_github_fetch_records_failing_verify_command_and_repository_id(self):
        env, data, _ = self.fake_github()
        fake = json.loads(data.read_text())
        fake["log"] = ("unit tests\tverify\t2026-10-20T11:59:58.1234567Z FAILED (failures=1)\n"
                       "unit tests\tverify\t2026-10-20T11:59:59.1234567Z FAIL: real verification command failed")
        data.write_text(json.dumps(fake))
        result = self.cli("github", "--repo", "example/project", "--run-id", "412", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        meta = json.loads(self.records()[0]["error"])
        self.assertEqual((meta.get("command"), meta.get("repository_id")), ("./.fleet/verify.sh", 7))

    def test_json_rejects_a_masked_failed_step_command(self):
        result = self.cli(*self.write(dict(fixture(), failed_step_command="pytest --token=***")))
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.store.exists())

    def test_reimport_accepts_legacy_and_enriched_ci_records_without_overwriting_either(self):
        command = "python3 -m unittest discover -s tests"
        for run_id, enrich_first in ((412, False), (413, True)):
            with self.subTest(enrich_first=enrich_first):
                old = dict(fixture(), run_id=run_id)
                enriched = dict(old, failed_step_command=command)
                first, later = (enriched, old) if enrich_first else (old, enriched)
                self.assertEqual(self.cli(*self.write(first)).returncode, 0)
                before = self.records()
                result = self.cli(*self.write(later))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), dict(imported=0, skipped=1))
                self.assertEqual(self.records(), before)
                if enrich_first:
                    conflict = dict(enriched, failed_step_command="python -m unittest discover -s tests")
                    self.assertEqual(self.cli(*self.write(conflict)).returncode, 2)
                    self.assertEqual(self.records(), before)

    def test_github_fetch_retains_the_failed_shell_command_outside_the_log_tail(self):
        env, data, _ = self.fake_github()
        fake = json.loads(data.read_text())
        command = "python3 -m unittest discover -s tests"

        def log_step(name, lines, log_format, second):
            step = "UNKNOWN STEP" if log_format == "unknown" else name
            prefix = "unit tests\t" + step + "\t" if log_format != "raw" else ""
            return "\n".join(prefix + f"2026-10-20T11:59:{second}.1234567Z " + line for line in lines)

        def shell_step(command, prefixed):
            escape = "^[" if prefixed else "\x1b"
            return ["##[group]Run " + command, escape + "[36;1m" + command + escape + "[0m",
                    "  shell: /usr/bin/bash -e {0}", "##[endgroup]"]

        for log_format in ("named", "unknown", "raw"):
            prefixed = log_format != "raw"
            for outcome in ("failure", "continued", "passed"):
                with self.subTest(log_format=log_format, outcome=outcome):
                    failed = outcome == "failure"
                    fake["jobs"]["jobs"][0]["steps"] = [
                        dict(name="Tests", conclusion="failure" if failed else "success",
                             started_at="2026-10-20T11:59:51Z", completed_at="2026-10-20T11:59:56Z"),
                        dict(name="Cleanup", conclusion="success" if failed else "failure",
                             started_at="2026-10-20T11:59:57Z", completed_at="2026-10-20T11:59:59Z"),
                    ]
                    error = "##[error]Process completed with exit code 1."
                    output = ["x" * ingest.TAIL] + ([error] if outcome != "passed" else [])
                    cleanup = ["##[group]Run example/action@v4", "##[endgroup]"] + ([] if failed else [error])
                    log = (log_step("Tests", shell_step(command, prefixed), log_format, 51) + "\n"
                           + log_step("Tests", output, log_format, 56) + "\n"
                           + log_step("Cleanup", cleanup, log_format, 59))
                    fake.update(log=log if prefixed else "", job_logs={"888": log})
                    data.write_text(json.dumps(fake))
                    with patch.dict(os.environ, env):
                        record = ingest.fetch("example/project", 412, 1)
                    self.assertNotIn(command, json.loads(record["error"])["log_tail"])
                    self.assertEqual(ingest.recorded_command(record), command if failed else None)

    def test_poll_skips_ingested_run_before_fetch_and_imports_later_attempt(self):
        env, data, calls = self.fake_github()
        config = self.work / "repos.json"
        config.write_text('{"repos":["example/project"]}')
        args = ["poll", "--config", str(config), "--limit", "5"]
        result = self.cli(*args, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), dict(imported=1, skipped=0))
        calls.write_text("")
        again = self.cli(*args, env=env)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(json.loads(again.stdout), dict(imported=0, skipped=1))
        self.assertEqual(len(calls.read_text().splitlines()), 1)
        response = json.loads(data.read_text())
        response["run"]["run_attempt"] = 2
        data.write_text(json.dumps(response))
        newer = self.cli(*args, env=env)
        self.assertEqual(newer.returncode, 0, newer.stderr)
        self.assertEqual(json.loads(newer.stdout), dict(imported=1, skipped=0))
        self.assertEqual(len(self.records()), 2)

    def test_remote_errors_and_bad_identity_leave_no_store(self):
        env, data, _ = self.fake_github(error=True)
        args = ["github", "--repo", "example/project", "--run-id", "412"]
        result = self.cli(*args, env=env)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("sensitive-example", result.stdout + result.stderr)
        self.assertFalse(self.store.exists())
        response = json.loads(data.read_text())
        response["error"] = False
        response["run"]["repository"]["full_name"] = "other/project"
        data.write_text(json.dumps(response))
        self.assertEqual(self.cli(*args, env=env).returncode, 2)
        self.assertFalse(self.store.exists())

    def test_poll_does_not_skip_manual_event_with_same_reference(self):
        event = ingest.ci_event(fixture())
        event["error"] = "historical summary without logs"
        manual = c.event_record(event, "manual-history")
        c.save(self.store, "failures", manual)
        env, _, _ = self.fake_github()
        config = self.work / "repos.json"
        config.write_text('{"repos":["example/project"]}')
        result = self.cli("poll", "--config", str(config), env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), dict(imported=1, skipped=0))
        self.assertEqual(len(self.records()), 2)

    def test_poll_continues_after_unavailable_run(self):
        env, data, _ = self.fake_github()
        response = json.loads(data.read_text())
        response["runs"] = [dict(databaseId=413, attempt=1, conclusion="failure"),
                            dict(databaseId=412, attempt=1, conclusion="failure")]
        data.write_text(json.dumps(response))
        config = self.work / "repos.json"
        config.write_text('{"repos":["example/project"]}')
        result = self.cli("poll", "--config", str(config), env=env)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout), dict(imported=1, skipped=0, failed=1))
        self.assertEqual(json.loads(self.records()[0]["error"])["run_id"], 412)

    def test_poll_includes_timeouts_and_ignores_successful_runs(self):
        env, data, _ = self.fake_github()
        response = json.loads(data.read_text())
        response["run"]["conclusion"] = "timed_out"
        response["jobs"]["jobs"][0]["conclusion"] = "timed_out"
        response["runs"] = [dict(databaseId=413, attempt=1, conclusion="success"),
                            dict(databaseId=412, attempt=1, conclusion="timed_out")]
        data.write_text(json.dumps(response))
        config = self.work / "repos.json"
        config.write_text('{"repos":["example/project"]}')
        result = self.cli("poll", "--config", str(config), env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), dict(imported=1, skipped=0))
        self.assertEqual(json.loads(self.records()[0]["error"])["conclusion"], "timed_out")

    def test_expired_github_logs_use_failure_annotations(self):
        env, data, _ = self.fake_github()
        response = json.loads(data.read_text())
        response.update(log_error="failed to get run log: HTTP 410: Server Error",
                        job_log_error="HTTP 410: Gone",
                        annotations={"888": [dict(annotation_level="failure", message="AssertionError: expected two rows")]})
        data.write_text(json.dumps(response))
        result = self.cli("github", "--repo", "example/project", "--run-id", "412", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("AssertionError: expected two rows", json.loads(self.records()[0]["error"])["log_tail"])

    def test_empty_failed_run_log_uses_existing_job_log_fallback(self):
        env, data, _ = self.fake_github()
        response = json.loads(data.read_text())
        response.update(log="", job_logs={"888": "FAIL: test_example (tests.test_unit.UnitTests.test_example)"})
        data.write_text(json.dumps(response))
        result = self.cli("github", "--repo", "example/project", "--run-id", "412", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.records()[0]["error"])["log_tail"], response["job_logs"]["888"] + "\n")

    def test_expired_job_log_status_in_parentheses_uses_annotations(self):
        env, data, _ = self.fake_github()
        response = json.loads(data.read_text())
        response.update(log_error="failed to get run log: HTTP 410: Server Error",
                        job_log_error="gh: Gone (HTTP 410)", annotations={
            "888": [dict(annotation_level="failure", message="AssertionError: expected two rows")]})
        data.write_text(json.dumps(response))
        result = self.cli("github", "--repo", "example/project", "--run-id", "412", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("AssertionError: expected two rows", json.loads(self.records()[0]["error"])["log_tail"])

    def test_expired_log_annotations_keep_the_failure_message(self):
        env, data, _ = self.fake_github()
        response = json.loads(data.read_text())
        response.update(log_error="HTTP 410: Gone", job_log_error="HTTP 410: Gone", annotations={
            "888": [dict(annotation_level="failure", message="AssertionError: expected two rows")]
                   + [dict(annotation_level="failure", message="")] * ingest.TAIL})
        data.write_text(json.dumps(response))
        result = self.cli("github", "--repo", "example/project", "--run-id", "412", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("AssertionError: expected two rows", json.loads(self.records()[0]["error"])["log_tail"])

    def test_expired_logs_without_failure_diagnostics_remain_rejected(self):
        env, data, _ = self.fake_github()
        response = json.loads(data.read_text())
        response["annotations"] = {"888": [dict(annotation_level="notice", message="unrelated notice")]}
        for log_error, job_log_error in (("HTTP 410: Gone", "HTTP 410: Gone"),
                                         ("log not found: 412", "HTTP 404")):
            with self.subTest(log_error=log_error):
                response.update(log_error=log_error, job_log_error=job_log_error)
                data.write_text(json.dumps(response))
                result = self.cli("github", "--repo", "example/project", "--run-id", "412", env=env)
                self.assertEqual(result.returncode, 2)
                self.assertFalse(self.store.exists())

    def test_github_quota_errors_do_not_use_annotation_fallback(self):
        env, data, calls = self.fake_github()
        response = json.loads(data.read_text())
        response.update(log_error="HTTP 403: API rate limit exceeded", annotations={
            "888": [dict(annotation_level="failure", message="AssertionError: expected two rows")]})
        data.write_text(json.dumps(response))
        result = self.cli("github", "--repo", "example/project", "--run-id", "412", env=env)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.store.exists())
        self.assertFalse(any("annotations?" in line for line in calls.read_text().split("\n")))
        response.update(log_error="HTTP 410: Gone", job_log_error="HTTP 410: Gone")
        data.write_text(json.dumps(response))
        retried = self.cli("github", "--repo", "example/project", "--run-id", "412", env=env)
        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertIn("AssertionError: expected two rows", json.loads(self.records()[0]["error"])["log_tail"])

    def test_github_job_without_log_imports_failure_annotations(self):
        env, data, _ = self.fake_github()
        response = json.loads(data.read_text())
        for log_error, job_logs, job_log_error in (("HTTP 502: Bad Gateway", {"888": "complete job log"}, "HTTP 404"),
                                                   ("log not found: 888", {}, "HTTP 502: Bad Gateway")):
            response.update(log_error=log_error, job_logs=job_logs, job_log_error=job_log_error)
            data.write_text(json.dumps(response))
            self.assertEqual(self.cli("github", "--repo", "example/project", "--run-id", "412", env=env).returncode, 2)
            self.assertFalse(self.store.exists())
        response["log_error"] = "log not found: 888"
        second = dict(response["jobs"]["jobs"][0], id=889, name="lint", runner_name="hosted-runner")
        response["jobs"]["jobs"][0].update(runner_name="cbx-1793998683-04f242", steps=[
            dict(name="Set up runner", conclusion="success"), dict(name="Run coldbox checks", conclusion=None)])
        response["jobs"] = dict(total_count=2, jobs=[response["jobs"]["jobs"][0], second])
        response["job_logs"] = {"889": "lint failed: E501 line too long"}
        response.pop("job_log_error")
        noise = [dict(annotation_level="notice", message="unrelated notice")] * 100
        response["annotations"] = {"888": noise + [
            dict(annotation_level="failure", message="The self-hosted runner lost communication with the server.")]}
        data.write_text(json.dumps(response))
        result = self.cli("github", "--repo", "example/project", "--run-id", "412", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        meta = json.loads(self.records()[0]["error"])
        self.assertEqual(meta["runner"], "cbx-1793998683-04f242; hosted-runner")
        self.assertEqual(meta["log_tail"], "unit tests: GitHub kept no log; unfinished or failed steps: Run coldbox checks\n"
                                           "unit tests: The self-hosted runner lost communication with the server.\n"
                                           "lint failed: E501 line too long\n")
        self.assertEqual(meta["classification"]["category"], "env-infra")

    def test_poll_imports_a_logless_run_without_failed_jobs_beside_unrelated_coldbox_records(self):
        self.store.mkdir(mode=0o700)
        unrelated = dict(ingest.ci_event(dict(fixture(), run_id=7)), source_ref="coldbox:note", error="plain text")
        ingest.import_records(self.store, [ingest.record(unrelated)])
        env, data, _ = self.fake_github()
        response = json.loads(data.read_text())
        response["jobs"] = dict(total_count=0, jobs=[])
        response["log_error"] = "log not found: 412"
        data.write_text(json.dumps(response))
        config = self.work / "repos.json"
        config.write_text('{"repos":["example/project"]}')
        result = self.cli("poll", "--config", str(config), env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), dict(imported=1, skipped=0))
        meta = [json.loads(row["error"]) for row in self.records() if "/actions/runs/412" in row["source_ref"]][0]
        self.assertEqual(meta["log_tail"], "GitHub kept no log for run 412 attempt 1 and listed no failed jobs")

    def test_poll_imports_only_the_hosted_failure_of_a_run_whose_coldbox_job_has_a_receipt(self):
        def receipt(runner, attempt):
            path = self.work / (runner + ".json")
            path.write_text(json.dumps(dict(schema="coldbox.receipt/v1", id=runner, mode="runner", repo="example/project",
                                            sha="a" * 40, verdict="fail", started_at="2026-10-20T11:58:00Z",
                                            checks=[dict(check="unit", command="python3 -m unittest", status="fail", exit=1)],
                                            github=dict(run_id="412", run_attempt=str(attempt), workflow="verify", job="unit tests"))))
            loaded = subprocess.run([sys.executable, str(ROOT / "coldfix.py"), "--store", str(self.store), "ingest",
                                     "--format", "coldbox", "--input", str(path)], capture_output=True, text=True)
            self.assertEqual(loaded.returncode, 0, loaded.stderr)
        receipt("cbx-1793998683-04f242", 1)
        receipt("cbx-1793998683-04f243", 2)
        env, data, calls = self.fake_github()
        response = json.loads(data.read_text())
        coldbox = dict(response["jobs"]["jobs"][0], runner_name="cbx-1793998683-04f242")
        hosted = dict(coldbox, id=889, name="lint", runner_name="hosted-runner")
        response["jobs"] = dict(total_count=2, jobs=[coldbox, hosted])
        response["job_logs"] = {"888": "coldbox job log", "889": "lint failed: E501 line too long"}
        data.write_text(json.dumps(response))
        config = self.work / "repos.json"
        config.write_text('{"repos":["example/project"]}')
        result = self.cli("poll", "--config", str(config), env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), dict(imported=1, skipped=0))
        meta = [json.loads(row["error"]) for row in self.records() if "/actions/runs/412" in row["source_ref"]][0]
        self.assertEqual((meta["runner"], meta["job"], meta["log_tail"]), ("hosted-runner", "lint", "lint failed: E501 line too long\n"))
        response["run"]["run_attempt"] = 2
        response["runs"] = [dict(databaseId=412, attempt=2, conclusion="failure")]
        response["jobs"] = dict(total_count=1, jobs=[dict(coldbox, runner_name="cbx-1793998683-04f243")])
        data.write_text(json.dumps(response))
        result = self.cli("poll", "--config", str(config), env=env)
        self.assertEqual(json.loads(result.stdout), dict(imported=0, skipped=0, coldbox=1))
        self.assertNotIn("--log-failed", calls.read_text())

    def test_poll_backfills_verify_receipt_outside_recent_runs_once(self):
        receipt = dict(schema="coldbox.receipt/v1", id="cbx-1793998683-04f242", mode="runner",
                       repo="example/project", sha="a" * 40, verdict="fail", started_at="2026-10-20T11:58:00Z",
                       checks=[dict(check="unit", command="python3 /opt/example/ci-check --ci", status="fail", exit=1)],
                       github=dict(run_id="412", run_attempt="1", workflow="verify", job="unit tests"))
        self.input.write_text(json.dumps(receipt))
        loaded = subprocess.run([sys.executable, str(ROOT / "coldfix.py"), "--store", str(self.store),
                                 "ingest", "--format", "coldbox", "--input", str(self.input)], capture_output=True, text=True)
        self.assertEqual(loaded.returncode, 0, loaded.stderr)
        env, data, calls = self.fake_github()
        env["COLDFIX_CI_COMMAND"] = "ci-check --ci"
        response = json.loads(data.read_text())
        response["runs"] = []
        response["log"] = "FAIL: test_example (tests.test_unit.UnitTests.test_example)\n"
        response["jobs"]["jobs"][0]["runner_name"] = receipt["id"]
        data.write_text(json.dumps(response))
        config = self.work / "repos.json"
        config.write_text('{"repos":["example/project"]}')
        result = self.cli("poll", "--config", str(config), "--limit", "1", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), dict(imported=1, skipped=0))
        meta = [json.loads(row["error"]) for row in self.records() if row["source_ref"].startswith("https:")][0]
        self.assertEqual(meta["type"], "ci")
        self.assertEqual(meta["log_tail"], (response["log"] + "\n")[-ingest.TAIL:])
        before = calls.read_text()
        result = self.cli("poll", "--config", str(config), env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls.read_text()[len(before):].count("--log-failed"), 0)

    @patch.dict(os.environ, {"COLDFIX_CI_COMMAND": "ci-check --ci"})
    def test_poll_historical_backlog_advances_after_unavailable_run(self):
        rows = []
        for run_id, workflow in [(410, "other"), (411, "verify"), (412, None)]:
            metadata = dict(type="coldbox", id=f"cbx-{run_id}", repo="example/project",
                            command="ci-check --ci", github=dict(run_id=str(run_id), run_attempt="1", workflow=workflow))
            rows.append(c.event_record(dict(kind="ci", repo="example/project", agent="unknown", command="verify", error=json.dumps(metadata),
                                           source_ref=f"coldbox:{run_id}", occurred="2026-10-20T12:00:00Z"), "coldbox"))
        for row, run_id in zip(rows, (410, 411, 412)):
            row["id"] = f"{run_id:032x}"
        ingest.import_records(self.store, rows)
        with patch.object(ingest, "gh", return_value=[]), patch.object(ingest, "fetch", side_effect=c.ColdfixError("unavailable")) as fetch:
            result = ingest.poll(self.store, ["example/project"], 1, "verify")
        self.assertEqual(result["failed"], 1)
        self.assertEqual(fetch.call_args.args[1], 411)
        fetched = ingest.record(ingest.ci_event(fixture()))
        def fetch_next(repo, run_id, *args, **kwargs):
            if run_id == 411:
                raise c.ColdfixError("unavailable")
            return fetched
        with patch.object(ingest, "gh", return_value=[]), patch.object(ingest, "fetch", side_effect=fetch_next) as fetch:
            result = ingest.poll(self.store, ["example/project"], 1, "verify")
        self.assertEqual(result["imported"], 1)
        self.assertEqual([call.args[1] for call in fetch.call_args_list], [412])
        with patch.object(ingest, "gh", return_value=[]), patch.object(ingest, "fetch") as fetch:
            ingest.poll(self.store, ["example/project"], 1, "verify")
        fetch.assert_not_called()
        ledger = self.store / "github-waiting.json"
        waiting = json.loads(ledger.read_text())
        for _, run in waiting.values():
            run["retry_after"] = 0
        ledger.write_text(json.dumps(waiting))
        with patch.object(ingest, "gh", return_value=[]), patch.object(ingest, "fetch", side_effect=c.ColdfixError("unavailable")) as fetch:
            ingest.poll(self.store, ["example/project"], 1, "verify")
        self.assertEqual(fetch.call_count, 1)

    def test_poll_waits_for_the_coldbox_receipt_before_importing(self):
        env, data, _ = self.fake_github()
        response = json.loads(data.read_text())
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        response["run"]["updated_at"] = now
        response["runs"] = [dict(databaseId=412, attempt=1, conclusion="failure", updatedAt=now)]
        response["jobs"]["jobs"][0]["runner_name"] = "cbx-1793998683-04f242"
        response["log_error"] = "HTTP 502: Bad Gateway"
        data.write_text(json.dumps(response))
        config = self.work / "repos.json"
        config.write_text('{"repos":["example/project"]}')
        self.store.mkdir(mode=0o700)
        result = self.cli("poll", "--config", str(config), env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), dict(imported=0, skipped=0, waiting_for_receipt=1))
        (self.store / "collected-receipts.json").write_text(json.dumps({"cbx-1793998683-04f242.json": "invalid coldbox receipt"}))
        result = self.cli("poll", "--config", str(config), env=env)
        self.assertEqual(json.loads(result.stdout), dict(imported=0, skipped=0, waiting_for_receipt=1))
        (self.store / "collected-receipts.json").write_text(json.dumps({"cbx-1793998683-04f242.json": "imported"}))
        response["runs"] = [dict(databaseId=412, attempt=2, conclusion="success", updatedAt=now)]
        response["latest"] = dict(response["run"], run_attempt=2, conclusion="success")
        data.write_text(json.dumps(response))
        result = self.cli("poll", "--config", str(config), env=env)
        self.assertEqual(json.loads(result.stdout), dict(imported=0, skipped=0, failed=1))
        response.pop("log_error")
        data.write_text(json.dumps(response))
        result = self.cli("poll", "--config", str(config), env=env)
        self.assertEqual(json.loads(result.stdout), dict(imported=1, skipped=0))
        result = self.cli("poll", "--config", str(config), env=env)
        self.assertEqual(json.loads(result.stdout), dict(imported=0, skipped=0))
        hours_ago = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        unavailable = {f"https://github.com/example/project/actions/runs/{run_id}#attempt-1":
                       ["example/project", dict(databaseId=run_id, attempt=1, conclusion="failure", workflowName="verify", updatedAt=updated)]
                       for run_id, updated in ((410, hours_ago), (411, "2026-10-20T12:00:00Z"))}
        waiting = self.store / "github-waiting.json"
        waiting.write_text(json.dumps(unavailable))
        listing = response["runs"]
        response["runs"] = listing + [unavailable["https://github.com/example/project/actions/runs/411#attempt-1"][1]]
        data.write_text(json.dumps(response))
        result = self.cli("poll", "--config", str(config), env=env)
        self.assertEqual(json.loads(result.stdout), dict(imported=0, skipped=0, failed=2))
        self.assertEqual(list(json.loads(waiting.read_text())), ["https://github.com/example/project/actions/runs/410#attempt-1"])
        response["runs"] = listing
        data.write_text(json.dumps(response))
        waiting.write_text(json.dumps({}))
        result = self.cli("poll", "--config", str(config), env=env)
        self.assertEqual(json.loads(result.stdout), dict(imported=0, skipped=0))


if __name__ == "__main__":
    unittest.main()
