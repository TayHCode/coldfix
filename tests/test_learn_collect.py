import json
import os
from pathlib import Path
import tempfile
import unittest
import subprocess
import io
import tarfile
from unittest.mock import patch

import coldfix as c
import failure_ingest as ingest
import learn_collect


ROOT = Path(__file__).resolve().parents[1]
RUN = json.loads((ROOT / "tests" / "fixtures" / "failed-ci-run.json").read_text())


def receipt(identifier, repo="example/app"):
    return dict(schema="coldbox.receipt/v1", id=identifier, mode="runner", repo=repo, sha="a" * 40,
                verdict="fail", started_at="2026-11-01T12:00:00Z",
                checks=[dict(check="unit", command="python3 -m unittest", status="fail", exit=1)])


class LearnCollectTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.store = self.work / ".coldfix"
        self.store.mkdir(mode=0o700)
        self.receipts = self.work / "receipts"
        self.receipts.mkdir()
        fleet = patch.dict(os.environ, {"COLDFIX_FLEET_CLI": "acme"})
        fleet.start()
        self.addCleanup(fleet.stop)

    def write(self, name, row, age=120):
        path = self.receipts / name
        path.write_text(json.dumps(row))
        stamp = path.stat().st_mtime - age
        os.utime(path, (stamp, stamp))
        return path

    def failures(self):
        return c.load_store(self.store)[1]["failures"]

    def test_log_read_error_is_retryable(self):
        row = receipt("cbx-1700000000-a1b2c3")
        path = self.write("cbx-1.json", row)
        (self.receipts / (row["id"] + ".log")).write_text("failure")
        with patch.object(Path, "open", side_effect=PermissionError("unavailable")):
            with self.assertRaises(Exception) as raised:
                c.input_records(json.dumps(row), "coldbox", str(path))
        self.assertIsInstance(raised.exception, PermissionError)
        unavailable = subprocess.CompletedProcess([], 2, "", "coldfix: cannot read or write local files; existing records left unchanged\n")
        with patch.object(learn_collect.subprocess, "run", return_value=unavailable):
            self.assertEqual(learn_collect.receipts(self.store, self.receipts)["rejected"], 1)
        self.assertEqual(learn_collect.receipts(self.store, self.receipts)["imported"], 1)
        self.assertEqual(len(self.failures()), 1)

    def test_receipts_import_once_wait_for_settled_files_and_remember_rejected(self):
        self.write("cbx-1.json", receipt("cbx-1700000000-a1b2c3"))
        self.write("cbx-2.json", receipt("cbx-1700000001-a1b2c3", repo="bare-name"))
        self.write("cbx-3.json", receipt("cbx-1700000002-a1b2c3"), age=0)
        self.assertEqual(learn_collect.receipts(self.store, self.receipts), dict(imported=1, skipped=0, rejected=1, known_invalid=0))
        self.assertEqual(len(self.failures()), 1)
        self.assertEqual(learn_collect.receipts(self.store, self.receipts), dict(imported=0, skipped=0, rejected=0, known_invalid=1))
        ledger = json.loads((self.store / "collected-receipts.json").read_text())
        self.assertEqual(ledger["cbx-1.json"], "imported")
        self.assertEqual(ledger["cbx-2.json"]["reason"], "coldbox receipt repo must be owner/repo; nothing imported")
        self.assertEqual(set(ledger), {"cbx-1.json", "cbx-2.json"})
        stamp = (self.receipts / "cbx-3.json").stat().st_mtime - 120
        os.utime(self.receipts / "cbx-3.json", (stamp, stamp))
        self.assertEqual(learn_collect.receipts(self.store, self.receipts), dict(imported=1, skipped=0, rejected=0, known_invalid=1))
        self.assertEqual(len(self.failures()), 2)

    def test_invalid_receipt_is_parsed_again_only_after_it_or_the_parser_changes(self):
        self.write("cbx-1.json", receipt("cbx-1700000000-a1b2c3", repo="stress"))
        self.assertEqual(learn_collect.receipts(self.store, self.receipts), dict(imported=0, skipped=0, rejected=1, known_invalid=0))
        with patch.object(learn_collect.subprocess, "run", side_effect=AssertionError("parsed again")):
            self.assertEqual(learn_collect.receipts(self.store, self.receipts), dict(imported=0, skipped=0, rejected=0, known_invalid=1))
        with patch.object(learn_collect, "PARSER", "next parser"):
            self.assertEqual(learn_collect.receipts(self.store, self.receipts), dict(imported=0, skipped=0, rejected=1, known_invalid=0))
            self.assertEqual(learn_collect.receipts(self.store, self.receipts), dict(imported=0, skipped=0, rejected=0, known_invalid=1))
        self.write("cbx-1.json", receipt("cbx-1700000000-a1b2c3"))
        self.assertEqual(learn_collect.receipts(self.store, self.receipts), dict(imported=1, skipped=0, rejected=0, known_invalid=0))
        self.assertEqual(json.loads((self.store / "collected-receipts.json").read_text()), {"cbx-1.json": "imported"})

    def test_receipt_rejected_for_its_log_is_retried_until_the_log_is_repaired(self):
        row = receipt("cbx-1700000000-a1b2c3")
        self.write("cbx-1.json", row)
        log = self.receipts / (row["id"] + ".log")
        log.write_bytes(b"failure\x00")
        for _ in range(2):
            self.assertEqual(learn_collect.receipts(self.store, self.receipts), dict(imported=0, skipped=0, rejected=1, known_invalid=0))
        log.write_text("failure")
        self.assertEqual(learn_collect.receipts(self.store, self.receipts), dict(imported=1, skipped=0, rejected=0, known_invalid=0))

    def test_receipt_rejected_by_an_older_parser_is_imported(self):
        self.write("cbx-1.json", receipt("cbx-1700000000-a1b2c3"))
        (self.store / "collected-receipts.json").write_text(
            json.dumps({"cbx-1.json": "invalid coldbox receipt; nothing imported"}))
        self.assertEqual(learn_collect.receipts(self.store, self.receipts), dict(imported=1, skipped=0, rejected=0, known_invalid=0))
        self.assertEqual(len(self.failures()), 1)

    def test_provisional_receipt_waits_for_its_final_rewrite(self):
        provisional = dict(receipt("cbx-1700000000-a1b2c3"), sha=None, verdict="incomplete", checks=[],
                           vm=dict(ttl_seconds=21600, destroyed=False))
        for age in (600, 21601):
            self.write("cbx-1.json", provisional, age=age)
            self.assertEqual(learn_collect.receipts(self.store, self.receipts), dict(imported=0, skipped=0, rejected=0, known_invalid=0))
            self.assertFalse((self.store / "collected-receipts.json").exists())
        final = dict(receipt("cbx-1700000000-a1b2c3"), vm=dict(ttl_seconds=21600, destroyed=True,
                                                               destroyed_at="2026-11-01T12:10:00+00:00"))
        self.write("cbx-1.json", final)
        self.assertEqual(learn_collect.receipts(self.store, self.receipts), dict(imported=1, skipped=0, rejected=0, known_invalid=0))
        self.write("cbx-2.json", dict(receipt("cbx-1700000001-a1b2c3"), vm=dict(name="cbx-1700000001-a1b2c3")))
        (self.receipts / "cbx-3.json").write_text("[" * 100000)
        stamp = (self.receipts / "cbx-3.json").stat().st_mtime - 120
        os.utime(self.receipts / "cbx-3.json", (stamp, stamp))
        self.assertEqual(learn_collect.receipts(self.store, self.receipts), dict(imported=1, skipped=0, rejected=1, known_invalid=0))

    def test_poll_keeps_only_named_workflow_and_continues_after_a_repository_fails(self):
        runs = [dict(databaseId=412, attempt=1, conclusion="failure", workflowName="verify"),
                dict(databaseId=413, attempt=1, conclusion="failure", workflowName="coldfix-rules")]
        def gh(command, raw=False):
            if "example/broken" in command[3]:
                raise c.ColdfixError("unavailable")
            return runs
        fetched = []
        def fetch(repo, run_id, attempt):
            fetched.append(run_id)
            return ingest.record(ingest.ci_event(RUN))
        with patch.object(ingest, "gh", side_effect=gh), patch.object(ingest, "fetch", side_effect=fetch):
            result = ingest.poll(self.store, ["example/broken", "example/project"], 30, "verify")
        self.assertEqual(result, dict(imported=1, skipped=0, unlisted=1))
        self.assertEqual(fetched, [412])
        self.assertEqual(len(self.failures()), 1)

    def test_poll_cli_fails_when_no_repository_can_be_listed(self):
        config = self.work / "repos.json"
        config.write_text(json.dumps(dict(repos=["example/project"])))
        with patch.object(ingest, "gh", side_effect=c.ColdfixError("unavailable")), patch("sys.stdout"):
            self.assertEqual(ingest.main(["--store", str(self.store), "poll", "--config", str(config)]), 2)

    def main(self):
        with patch("sys.stdout") as stdout:
            code = learn_collect.main(["--store", str(self.store), "--owner", "example", "--receipts", str(self.receipts)])
        return code, json.loads("".join(call.args[0] for call in stdout.write.call_args_list))

    def test_exit_fails_only_when_no_github_repository_could_be_listed(self):
        repos = ["example/one", "example/two"]
        cases = [(dict(imported=0, skipped=0, failed=3), 0), (dict(imported=0, skipped=0, unlisted=1), 0),
                 (dict(imported=0, skipped=0, unlisted=2), 2)]
        for github, code in cases:
            with self.subTest(github=github), patch("ci_fix_learning.discover", return_value=repos), \
                    patch.object(ingest, "poll", return_value=github):
                with patch.object(learn_collect, "reviews", return_value=dict(imported=0, deduped=0, unavailable=0), create=True):
                    actual, result = self.main()
                self.assertEqual(actual, code)
                self.assertEqual(result["github"], github)
                self.assertEqual(result["coldbox"], dict(imported=0, skipped=0, rejected=0, known_invalid=0))
                self.assertEqual(result.get("codex_review"), dict(imported=0, deduped=0, unavailable=0))

    def test_empty_receipts_option_skips_local_receipts(self):
        self.write("cbx-1.json", receipt("cbx-1700000000-a1b2c3"))
        with patch("ci_fix_learning.discover", return_value=[]), \
                patch.object(ingest, "poll", return_value=dict(imported=0, skipped=0)), \
                patch("sys.stdout") as stdout:
            self.addCleanup(os.chdir, os.getcwd())
            os.chdir(self.receipts)
            code = learn_collect.main(["--store", str(self.store), "--owner", "example", "--receipts", ""])
        result = json.loads("".join(call.args[0] for call in stdout.write.call_args_list))
        self.assertEqual(code, 0)
        self.assertNotIn("coldbox", result)
        self.assertEqual(self.failures(), {})

    def test_missing_local_receipts_folder_still_collects_remote_coldbox_receipts(self):
        payload = json.dumps(receipt("cbx-1700000000-a1b2c3")).encode()
        bundle = self.work / "runner.tar"
        with tarfile.open(bundle, "w") as archive:
            info = tarfile.TarInfo("./cbx-1700000000-a1b2c3.json")
            info.size, info.mtime = len(payload), os.path.getmtime(self.receipts) - 120
            archive.addfile(info, io.BytesIO(payload))
        config = self.work / "sources.json"
        config.write_text(json.dumps({"edge-a1": {"format": "coldbox-tar", "command": ["cat", str(bundle)]}}))
        absent = self.work / "no-local-receipts"
        with patch.dict(os.environ, {"COLDFIX_SOURCES": str(config)}), \
                patch("ci_fix_learning.discover", return_value=[]), \
                patch.object(ingest, "poll", return_value=dict(imported=0, skipped=0)), \
                patch("sys.stdout") as stdout:
            code = learn_collect.main(["--store", str(self.store), "--owner", "example", "--receipts", str(absent)])
        result = json.loads("".join(call.args[0] for call in stdout.write.call_args_list))
        self.assertEqual(code, 0)
        self.assertEqual(result["coldbox"], dict(missing=str(absent)))
        self.assertEqual((result["sources"]["edge-a1"]["imported"], result["sources"]["edge-a1"]["unavailable"]), (1, 0))
        self.assertEqual(len(self.failures()), 1)

    def test_discovery_failure_still_collects_receipts(self):
        self.write("cbx-1.json", receipt("cbx-1700000000-a1b2c3"))
        with patch("ci_fix_learning.discover", side_effect=c.ColdfixError("unavailable")):
            code, output = self.main()
        self.assertEqual((code, output["coldbox"]["imported"]), (2, 1))
        self.assertEqual(output["github"], dict(discovery_failed="ColdfixError"))
        self.assertEqual(len(self.failures()), 1)

    def test_review_sources_collect_and_dedupe_without_blocking_other_sources(self):
        row = dict(id=1, user={"login": "chatgpt-codex-connector[bot]"}, body="<!-- codex-code-review-finding:v1 --> Fix the calculation.",
                   html_url="https://github.com/example/project/pull/1#discussion_r1", created_at="2026-11-01T12:00:00Z")
        review_receipt = dict(repo="example/project", pr=1, sha="a" * 40, model="reviewer", verdict="FAIL",
                              summary="Found a bug", findings=["Correct the calculation."])
        bundle = self.work / "reviews.tar"
        payload = json.dumps(review_receipt).encode()
        with tarfile.open(bundle, "w") as archive:
            info = tarfile.TarInfo("review.json")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
            malformed = ("[" * 2000 + "]" * 2000).encode()
            info = tarfile.TarInfo("malformed.json")
            info.size = len(malformed)
            archive.addfile(info, io.BytesIO(malformed))
        config = self.work / "sources.json"
        config.write_text(json.dumps({"review": {"format": "cold-review-tar", "command": ["cat", str(bundle)]},
                                      "broken": {"format": "cold-review-tar", "command": ["false"]}}))
        self.write("cbx-1.json", receipt("cbx-1700000000-a1b2c3"))
        with patch.dict(os.environ, {"COLDFIX_SOURCES": str(config)}), \
                patch("ci_fix_learning.discover", return_value=["example/project"]), \
                patch.object(ingest, "poll", return_value={"imported": 0, "skipped": 0, "unlisted": 0}), \
                patch("github_sources.GitHub.pages", return_value=[row]):
            _, first = self.main()
            _, second = self.main()
        self.assertEqual((first.get("codex_review", {}).get("imported"), second.get("codex_review", {}).get("deduped")), (1, 1))
        self.assertEqual((first["sources"]["review"]["imported"], second["sources"]["review"]["deduped"]), (1, 1))
        self.assertEqual(first["sources"]["review"]["rejected"], 1)
        self.assertEqual(first["sources"]["broken"]["unavailable"], 1)
        self.assertEqual(first["sources"]["broken"]["reason"], "CalledProcessError")
        self.assertEqual(first["coldbox"]["imported"], 1)
        self.assertEqual(len(self.failures()), 3)
        records = list(self.failures().values())
        self.assertTrue(any(record["source_ref"] == row["html_url"] and record["error"] == row["body"] for record in records))
        self.assertTrue(any(record["command"] == "acme review" and "Correct the calculation." in record["error"] for record in records))

    def test_review_archive_source_is_unavailable_without_the_fleet_setting(self):
        config = self.work / "sources.json"
        config.write_text(json.dumps({"review": {"format": "cold-review-tar", "command": ["false"]}}))
        with patch.dict(os.environ, {"COLDFIX_SOURCES": str(config)}), \
                patch("ci_fix_learning.discover", return_value=["example/project"]), \
                patch.object(ingest, "poll", return_value={"imported": 0, "skipped": 0, "unlisted": 0}), \
                patch("github_sources.GitHub.pages", return_value=[]):
            os.environ.pop("COLDFIX_FLEET_CLI", None)
            _, output = self.main()
        self.assertEqual(output["sources"]["review"],
                         dict(imported=0, deduped=0, rejected=0, unavailable=1,
                              reason="review receipt import is off: set COLDFIX_FLEET_CLI"))

    def test_codex_source_failure_does_not_abort_receipts(self):
        self.write("cbx-1.json", receipt("cbx-1700000000-a1b2c3"))
        with patch("ci_fix_learning.discover", return_value=["example/project"]), \
                patch.object(ingest, "poll", return_value={"imported": 0, "skipped": 0, "unlisted": 0}), \
                patch("github_sources.GitHub.pages", side_effect=c.ColdfixError("unavailable")):
            code, output = self.main()
        self.assertEqual(code, 0)
        self.assertEqual(output.get("codex_review", {}).get("unavailable"), 1)
        self.assertEqual(output["codex_review"]["reason"], {"example/project": "ColdfixError"})
        self.assertEqual(output["coldbox"]["imported"], 1)

    def test_codex_findings_import_once_and_a_failed_repo_marks_only_itself(self):
        def finding(repo):
            return dict(id=1, user={"login": "chatgpt-codex-connector[bot]"}, body="<!-- codex-code-review-finding:v1 --> Fix " + repo,
                        html_url=f"https://github.com/{repo}/pull/1#discussion_r1", created_at="2026-11-01T12:00:00Z")

        def pages(github, path):
            if github.repo == "example/broken":
                raise c.ColdfixError("unavailable")
            return [finding(github.repo)]

        calls = []
        real = learn_collect.import_reviews

        def import_reviews(root, records):
            calls.append(sorted(row["repo"] for row in records))
            return real(root, records)

        with patch("ci_fix_learning.discover", return_value=["example/one", "example/broken", "example/two"]), \
                patch.object(ingest, "poll", return_value={"imported": 0, "skipped": 0, "unlisted": 0}), \
                patch("github_sources.GitHub.pages", pages), patch.object(learn_collect, "import_reviews", import_reviews):
            _, first = self.main()
            _, second = self.main()
        self.assertEqual(calls, [["example/one", "example/two"]] * 2)
        self.assertEqual(first["codex_review"], dict(imported=2, deduped=0, unavailable=1, reason={"example/broken": "ColdfixError"}))
        self.assertEqual(second["codex_review"], dict(imported=0, deduped=2, unavailable=1, reason={"example/broken": "ColdfixError"}))
        self.assertEqual(sorted(row["repo"] for row in self.failures().values()), ["example/one", "example/two"])

    def test_github_run_already_reported_by_a_coldbox_receipt_is_not_imported_twice(self):
        bundle_dir = self.work / "remote"
        bundle_dir.mkdir()
        for name, run_id in (("cbx-1793998683-04f242", 412), ("cbx-1793998684-04f243", 413)):
            path = bundle_dir / (name + ".json")
            path.write_text(json.dumps(dict(receipt(name, repo="Example/Project"),
                                            github=dict(run_id=str(run_id), run_attempt="1", workflow="verify", job="verify"))))
            stamp = path.stat().st_mtime - 120
            os.utime(path, (stamp, stamp))
        config = self.work / "sources.json"
        config.write_text(json.dumps({"runner": {"format": "coldbox-tar", "command": ["tar", "-C", str(bundle_dir), "-cf", "-", "."]}}))
        runs = [dict(databaseId=run_id, attempt=attempt, conclusion="failure", workflowName="verify")
                for run_id, attempt in ((412, 1), (413, 1), (412, 2), (414, 1))]
        runners = {412: "cbx-1793998683-04f242", 413: "cbx-1793998684-04f243", 414: "cbx-1793998683-04f242"}
        def fetch(repo, run_id, attempt, hold=lambda jobs: None, covered=frozenset()):
            jobs = [dict(id=1, name="verify", runner=runners[run_id], duration_seconds=1, conclusion="failure")]
            if run_id == 413:
                jobs.append(dict(id=2, name="lint", runner="hosted-runner", duration_seconds=1, conclusion="failure"))
            jobs = [job for job in jobs if job["runner"] not in covered]
            if not jobs:
                return "coldbox"
            if hold(jobs):
                return hold(jobs)
            if (run_id, attempt) == (412, 1):
                raise c.ColdfixError("log download failed")
            return ingest.record(ingest.ci_event(dict(RUN, run_id=run_id, attempt=attempt, jobs=jobs)))
        with patch.dict(os.environ, {"COLDFIX_SOURCES": str(config)}), \
                patch("ci_fix_learning.discover", return_value=["example/project"]), \
                patch.object(ingest, "gh", return_value=runs), patch.object(ingest, "fetch", side_effect=fetch), \
                patch.object(learn_collect, "reviews", return_value=dict(imported=0, deduped=0, unavailable=0)):
            code, output = self.main()
        self.assertEqual(code, 0)
        self.assertEqual(output["sources"]["runner"]["imported"], 2)
        self.assertEqual(output["github"], dict(imported=3, skipped=0, coldbox=1))
        imported = [json.loads(row["error"]) for row in self.failures().values() if "/actions/runs/" in row["source_ref"]]
        self.assertEqual(sorted((row["run_id"], row["attempt"], tuple(job["runner"] for job in row["jobs"])) for row in imported),
                         [(412, 2, ("cbx-1793998683-04f242",)), (413, 1, ("hosted-runner",)), (414, 1, ("cbx-1793998683-04f242",))])


if __name__ == "__main__":
    unittest.main()
