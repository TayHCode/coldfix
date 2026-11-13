"""Synthetic example-only GitHub API responses. No network or credentials."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import coldfix
import github_sources as sources


DATE = "2026-10-20T12:00:00Z"
URL = "https://github.example.invalid/example/project/"


def user(bot=False):
    return dict(login="example-bot" if bot else "example-user", type="Bot" if bot else "User")


def commit(number=1, message="fix: example mismatch"):
    return dict(oid=f"{number:040x}", url=URL + f"commit/{number}", message=message,
                committedDate=DATE, author=dict(user=dict(login="example-user")))


def pull(number=1):
    return dict(number=number, title='Revert "example change"', body="Example revert body",
                url=URL + f"pull/{number}", mergedAt=DATE, headRefOid=f"{1:040x}",
                mergeCommit=commit(2, "Merge example"), author=dict(login="example-user"))


def review(number=1, body="Example change request", state="CHANGES_REQUESTED", bot=False):
    return dict(id=number, body=body, state=state, user=user(bot), submitted_at=DATE,
                html_url=URL + f"pull/1#pullrequestreview-{number}")


def comment(number=1, body="[P1] Example finding", bot=True, review_id=None):
    return dict(id=number, body=body, user=user(bot), created_at=DATE,
                html_url=URL + f"pull/1#discussion_r{number}", pull_request_review_id=review_id)


def check(number=1, conclusion="failure"):
    return dict(id=number, name="example check", conclusion=conclusion, completed_at=DATE,
                started_at=DATE, url=URL + f"check-runs/{number}",
                html_url=URL + f"actions/runs/{number}", app=dict(slug="example-checker"),
                output=dict(title="Example mismatch", summary="Expected two", text=None))


class FakeGitHub:
    def __init__(self):
        self.history = [commit(1), commit(3, 'Revert "example change"\n\nThis reverts commit ' + f"{4:040x}" + ".")]
        self.pulls = [pull()]
        self.commits = {1: [dict(commit=commit(1)), dict(commit=commit(5, "docs: example change"))]}
        self.issues = {1: [dict(title="Example issue", body="Example issue detail", closed=True,
                              closedAt=DATE, url=URL + "issues/7", author=dict(login="example-user"),
                              repository=dict(nameWithOwner="example/project"))]}
        self.rest = {
            "pulls/1/reviews": [review(), review(2, "[P2] Example bot finding", "COMMENTED", True),
                                review(3, "Looks good", "COMMENTED")],
            "pulls/1/comments": [comment(), comment(2, "Example inline request", False, 1),
                                  comment(3, "Thanks for the change", False)],
            "issues/1/comments": [comment(4, "Please remove the duplicate example.", False)],
        }
        for sha in (1, 2, 5):
            self.rest[f"commits/{sha:040x}/check-suites"] = ("check_suites", [dict(id=11)])
        self.rest["check-suites/11/check-runs"] = ("check_runs", [check(), check(2, "success"), check(3, "timed_out")])
        self.calls = []
        self.graph_page_size = 100
        self.override = None

    def run(self, command, **kwargs):
        self.calls.append((command, kwargs))
        self.assert_read_only(command, kwargs)
        if self.override:
            result = self.override(command, kwargs)
            if result is not None:
                return result
        endpoint = command[6]
        if endpoint == "graphql":
            payload = json.loads(kwargs["input"])
            query, variables = payload["query"], payload["variables"]
            number = variables.get("number")
            if query == sources.PULL:
                value = dict(pullRequest=next(row for row in self.pulls if row["number"] == number))
            else:
                if query == sources.HISTORY:
                    rows, path = self.history, ("defaultBranchRef", "target", "history")
                elif query == sources.PULLS:
                    rows, path = self.pulls, ("pullRequests",)
                elif query == sources.PR_COMMITS:
                    rows, path = self.commits[number], ("pullRequest", "commits")
                else:
                    rows, path = self.issues[number], ("pullRequest", "closingIssuesReferences")
                start = int(variables["cursor"] or 0)
                end = start + self.graph_page_size
                value = dict(nodes=rows[start:end], totalCount=len(rows),
                             pageInfo=dict(hasNextPage=end < len(rows), endCursor=str(end)))
                for key in reversed(path):
                    value = {key: value}
            value = dict(data=dict(repository=value))
        else:
            parsed = urlsplit(endpoint)
            path = parsed.path.removeprefix("repos/example/project/")
            query = parse_qs(parsed.query)
            assert query["per_page"] == ["100"]
            if path.endswith("/check-runs"):
                assert query["filter"] == ["all"]
            page = int(query["page"][0])
            rows = self.rest[path]
            key = None
            if isinstance(rows, tuple):
                key, rows = rows
            value = rows[(page - 1) * 100:page * 100]
            if key:
                value = {key: value, "total_count": len(rows)}
            if path.startswith("commits/") and path.count("/") == 1:
                value = dict(files=value)
        return subprocess.CompletedProcess(command, 0, json.dumps(value), "")

    @staticmethod
    def assert_read_only(command, kwargs):
        assert command[:5] == ["gh", "api", "--hostname", "github.com", "--method"]
        assert kwargs.get("shell", False) is False
        assert kwargs["capture_output"] is True
        if command[6] == "graphql":
            assert command[5:7] == ["POST", "graphql"]
        else:
            assert command[5] == "GET"
        assert command[5] != "POST" or json.loads(kwargs["input"])["query"] in (
            sources.PULL, sources.PULLS, sources.HISTORY, sources.PR_COMMITS, sources.CLOSING)


class GitHubSourcesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = self.root / ".coldfix"
        self.fake = FakeGitHub()

    def run_adapter(self, *args, code=0, default_output=False):
        if not default_output and "--output" not in args and "--migrate-codex-labels" not in args:
            args = (*args, "--output", "github-events.jsonl")
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sources.subprocess, "run", side_effect=self.fake.run), redirect_stdout(stdout), redirect_stderr(stderr):
            result = sources.main(["--repo", "example/project", "--store", str(self.store), *args])
        self.assertEqual(result, code, stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        return stdout.getvalue(), stderr.getvalue()

    def events(self, name="github-events.jsonl"):
        return [json.loads(line) for line in (self.store / name).read_text().splitlines()]

    def codex_fixture(self, severity="P2", merged=False, later=False, reaction=None):
        self.fake.pulls[0]["mergedAt"] = DATE if merged else None
        body = f"![{severity} Badge](https://img.shields.io/badge/{severity}-yellow) Example finding\n<!-- codex-code-review-finding:v1 -->"
        finding = comment(body=body)
        finding.update(user=dict(login="chatgpt-codex-connector[bot]"), path="example.py",
                       original_commit_id=f"{1:040x}")
        self.fake.rest["pulls/1/comments"] = [finding]
        self.fake.rest["pulls/comments/1/reactions"] = [dict(id=1, content=reaction)] if reaction else []
        self.fake.commits[1][1]["commit"]["committedDate"] = "2026-10-21T12:00:00Z"
        self.fake.rest[f"commits/{5:040x}"] = [dict(filename="example.py" if later else "other.py")]
        return finding

    def test_codex_tag_severity_and_each_classification_rule(self):
        cases = [
            ("P0", False, False, None, "real bug"),
            ("P1", False, False, None, "real bug"),
            ("P2", False, True, None, "real bug"),
            ("P2", False, False, "+1", "real bug"),
            ("P2", False, False, "-1", "false alarm"),
            ("P2", True, False, None, "unresolved"),
            ("P3", True, False, None, "unresolved"),
            ("P2", True, False, "-1", "false alarm"),
            ("P2", False, False, None, "style"),
            ("P1", True, False, "-1", "real bug"),
            ("P2", True, True, "-1", "real bug"),
        ]
        for index, (severity, merged, later, reaction, expected) in enumerate(cases):
            with self.subTest(case=index):
                self.fake = FakeGitHub()
                self.codex_fixture(severity, merged, later, reaction)
                name = f"codex-{index}.jsonl"
                self.run_adapter("--pr", "1", "--output", name)
                events = [row for row in self.events(name) if row["agent"] == "codex-review"]
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["command"], f"PR review finding {severity}: {expected}")
                self.assertEqual(events[0]["kind"], "review")
                self.assertEqual(events[0]["thread"], URL + "pull/1")
                records = coldfix.input_records((self.store / name).read_text(), "jsonl", "github")
                self.assertTrue(any(row["agent"] == "codex-review" for row in records))

    def test_codex_shields_badge_and_unknown_severity(self):
        for index, (body, severity) in enumerate([
            ("![](https://img.shields.io/badge/P2-orange)", "P2"),
            ("[P3 Badge]", "P3"),
            ("Example finding without badge", "unknown"),
        ]):
            self.fake = FakeGitHub()
            finding = self.codex_fixture()
            finding["body"] = body + "\n<!-- codex-code-review-finding:v1 -->"
            name = f"badge-{index}.jsonl"
            self.run_adapter("--pr", "1", "--output", name)
            event = next(row for row in self.events(name) if row["agent"] == "codex-review")
            self.assertEqual(event["command"], f"PR review finding {severity}: style")

    def test_codex_boilerplate_is_skipped(self):
        self.codex_fixture()
        for index, marker in enumerate(("codex-pull-request-review-summary", "codex-review-receipt"), 10):
            inline = comment(index, f"[P1] Fix example {marker}")
            formal = review(index, f"Fix example {marker}")
            for row in (inline, formal):
                row["user"] = dict(login="chatgpt-codex-connector[bot]")
            self.fake.rest["pulls/1/comments"].append(inline)
            self.fake.rest["pulls/1/reviews"].append(formal)
        self.run_adapter("--pr", "1")
        self.assertFalse(any("codex-review-receipt" in row["error"] or
                             "codex-pull-request-review-summary" in row["error"] for row in self.events()))

    def test_codex_requires_bot_and_marker(self):
        finding = self.codex_fixture()
        finding["user"] = user()
        other = dict(finding, id=2, user=dict(login="chatgpt-codex-connector[bot]"), body="[P2] Example finding")
        self.fake.rest["pulls/1/comments"].append(other)
        self.run_adapter("--pr", "1")
        self.assertFalse(any(row["agent"] == "codex-review" for row in self.events()))

    def test_codex_rebased_comment_preserved_without_assuming_later_change(self):
        finding = self.codex_fixture(later=True)
        finding["original_commit_id"] = f"{99:040x}"
        self.run_adapter("--pr", "1")
        event = next(row for row in self.events() if row["agent"] == "codex-review")
        self.assertEqual(event["command"], "PR review finding P2: style")

    def test_human_discussing_codex_marker_is_preserved(self):
        self.fake.rest["pulls/1/comments"] = [comment(42, "Please fix codex-review-receipt", False)]
        self.run_adapter("--pr", "1")
        self.assertTrue(any(row["error"] == "Please fix codex-review-receipt" for row in self.events()))

    def test_codex_earlier_file_change_does_not_count(self):
        self.codex_fixture(merged=True, later=True)
        self.fake.commits[1][1]["commit"]["committedDate"] = "2026-10-19T12:00:00Z"
        self.run_adapter("--pr", "1")
        event = next(row for row in self.events() if row["agent"] == "codex-review")
        self.assertEqual(event["command"], "PR review finding P2: unresolved")

    def test_unaddressed_merged_playground_finding_is_unresolved_not_false_alarm(self):
        finding = self.codex_fixture(merged=True)
        finding["body"] = ("**<sub><sub>![P2 Badge](https://img.shields.io/badge/P2-yellow?style=flat)</sub></sub>"
                           "  Point the invalid-label CLI test at the new stub**\n\n"
                           "<!-- codex-code-review-finding:v1 -->")
        self.run_adapter("--pr", "1")
        event = next(row for row in self.events() if row["agent"] == "codex-review")
        self.assertEqual(event["command"], "PR review finding P2: unresolved")

    def test_extracts_history_and_only_explicit_reviews_and_ingests_v2(self):
        stdout, stderr = self.run_adapter()
        receipt = json.loads(stdout)
        events = self.events()
        self.assertEqual(receipt, dict(events=11, revert=2, issue=2, review=5, ci=2, requests=13,
                                       imported_records=11, skipped_duplicates=0))
        self.assertEqual(stderr, "")
        self.assertNotIn("example", stdout)
        self.assertEqual(events[0], dict(kind="issue", command="fix commit " + f"{1:040x}",
                                        error="fix: example mismatch", repo="example/project", agent="example-user",
                                        source_ref=URL + "commit/1", occurred=DATE, thread=""))
        self.assertEqual([row["error"] for row in events if row["kind"] == "review"],
                         ["Example change request", "[P2] Example bot finding", "[P1] Example finding",
                          "Example inline request", "Please remove the duplicate example."])
        self.assertEqual([row["source_ref"] for row in events if row["kind"] == "ci"],
                         [URL + "actions/runs/1", URL + "actions/runs/3"])
        self.assertEqual(self.store.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.store / "github-events.jsonl").stat().st_mode & 0o777, 0o600)
        result = subprocess.run([sys.executable, str(Path(coldfix.__file__)), "--store", str(self.store),
                                 "ingest", "--input", str(self.store / "github-events.jsonl")],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["imported_records"], 0)
        records = [json.loads(path.read_text()) for path in (self.store / "failures").glob("*.json")]
        self.assertEqual({row["version"] for row in records}, {2})
        self.assertEqual({row["source_ref"] for row in records}, {row["source_ref"] for row in events})

    def test_failed_check_preserves_head_sha(self):
        self.run_adapter()
        event = next(row for row in self.events() if row["kind"] == "ci")
        try:
            metadata = json.loads(event["error"])
        except json.JSONDecodeError:
            self.fail("failed check did not preserve structured head SHA")
        self.assertEqual(metadata["head_sha"], f"{1:040x}")
        self.assertIn("Example mismatch", metadata["details"])
        records = [json.loads(path.read_text()) for path in (self.store / "failures").glob("*.json")]
        record = next(row for row in records if row["source_ref"] == event["source_ref"])
        self.assertEqual(json.loads(record["error"])["head_sha"], f"{1:040x}")

    def test_graphql_and_rest_connections_traverse_every_page(self):
        self.fake.graph_page_size = 1
        self.fake.rest["pulls/1/comments"] = [comment(number, "[P1] Example finding " + str(number)) for number in range(1, 202)]
        self.fake.rest["check-suites/11/check-runs"] = ("check_runs", [check(number) for number in range(1, 202)])
        self.run_adapter()
        events = self.events()
        self.assertEqual(sum(row["kind"] == "review" for row in events), 204)
        self.assertEqual(sum(row["kind"] == "ci" for row in events), 201)
        endpoints = [command[6] for command, _ in self.fake.calls]
        self.assertIn("repos/example/project/pulls/1/comments?per_page=100&page=3", endpoints)
        self.assertIn("repos/example/project/check-suites/11/check-runs?per_page=100&page=3&filter=all", endpoints)
        graph = [json.loads(kwargs["input"]) for command, kwargs in self.fake.calls if command[6] == "graphql"]
        self.assertEqual({row["query"] for row in graph if row["variables"].get("cursor")},
                         {sources.HISTORY, sources.PR_COMMITS})

    def test_all_pr_and_closing_reference_pages_and_selected_prs(self):
        self.fake.graph_page_size = 1
        self.fake.pulls.append(dict(pull(2), mergedAt=None))
        self.fake.commits[2] = []
        self.fake.issues[1].append(dict(self.fake.issues[1][0], url=URL + "issues/8"))
        for endpoint in ("reviews", "comments"):
            self.fake.rest[f"pulls/2/{endpoint}"] = []
        self.fake.rest["issues/2/comments"] = []
        self.run_adapter("--pr", "1", "--pr", "2", "--pr", "1")
        graph = [json.loads(kwargs["input"]) for command, kwargs in self.fake.calls if command[6] == "graphql"]
        self.assertEqual([row["variables"]["number"] for row in graph if row["query"] == sources.PULL], [1, 2])
        self.assertNotIn(sources.HISTORY, [row["query"] for row in graph])
        self.assertNotIn(sources.PULLS, [row["query"] for row in graph])
        self.assertEqual(sum(row["command"].startswith("closed by") for row in self.events()), 2)
        self.fake.calls.clear()
        self.run_adapter("--output", "all.jsonl")
        graph = [json.loads(kwargs["input"]) for command, kwargs in self.fake.calls if command[6] == "graphql"]
        self.assertEqual([row["variables"]["cursor"] for row in graph if row["query"] == sources.PULLS], [None, "1"])

    def test_since_filters_event_time_and_is_validated_before_calls(self):
        self.fake.rest["pulls/1/reviews"][0]["submitted_at"] = "2026-10-21T00:30:00+01:00"
        self.run_adapter("--since", "2026-10-20T23:00:00Z")
        self.assertEqual([row["error"] for row in self.events()], ["Example change request"])
        for value in ("2026-02-30", "2026-10-20T12:00:00", "private-canary", "2026-10-20T12:00:00Zbad"):
            with self.subTest(value=value):
                self.fake.calls.clear()
                stdout, stderr = self.run_adapter("--since", value, "--output", "other.jsonl", code=2)
                self.assertEqual(self.fake.calls, [])
                self.assertEqual(stdout, "")
                self.assertNotIn("private-canary", stderr)
        self.fake.calls.clear()
        self.run_adapter("--pr", "0", code=2)
        self.assertEqual(self.fake.calls, [])

    def test_imperative_heuristic_ignores_generic_quotes_code_and_negations(self):
        findings = ["Please add the example check.", "Could you remove this duplicate?", "Use the existing helper.",
                    "Thanks. Please validate this field.", "1. Replace the example parser."]
        for body in findings:
            self.assertTrue(sources.change_finding(body), body)
        for body in ("Looks good", "Thanks for fixing this", "Can you explain this?", "No changes requested.",
                     "Not requesting changes.", "> Please remove this.", "```python\nremove example\n```", "I already fixed this."):
            self.assertFalse(sources.change_finding(body), body)
        self.assertTrue(sources.change_finding("```suggestion\nexample\n```"))
        self.assertTrue(sources.change_finding("**[P1]** Example finding"))
        self.assertTrue(sources.change_finding("![P1 Badge](https://example.invalid/badge/P1-orange) Example finding"))
        self.assertTrue(sources.change_finding("![priority](https://example.invalid/badge/P0-red) Example finding"))
        self.assertFalse(sources.change_finding("You should consider the example."))

    def test_selected_pr_includes_fix_named_only_by_squash_commit(self):
        self.fake.pulls[0]["mergeCommit"] = commit(2, "fix: example squash correction")
        self.run_adapter("--pr", "1")
        matching = [row for row in self.events() if row["source_ref"] == URL + "commit/2"]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["error"], "fix: example squash correction")
        self.assertEqual(matching[0]["kind"], "issue")

    def test_empty_commit_message_is_valid_history_but_not_a_failure_signal(self):
        self.fake.history.append(commit(9, ""))
        self.fake.commits[1].append(dict(commit=commit(9, "")))
        self.fake.rest[f"commits/{9:040x}/check-suites"] = ("check_suites", [])
        self.run_adapter()
        self.assertEqual(len(self.events()), 11)
        self.assertNotIn(URL + "commit/9", [row["source_ref"] for row in self.events()])

    def test_nullable_review_bodies_skip_pending_and_keep_formal_change_request(self):
        pending_without_time = review(14, None, "PENDING")
        del pending_without_time["submitted_at"]
        self.fake.rest["pulls/1/reviews"] = [
            dict(review(11, None, "PENDING"), submitted_at=None),
            pending_without_time,
            review(12, None, "CHANGES_REQUESTED"),
            review(13, None, "COMMENTED"),
        ]
        self.run_adapter()
        reviews = [row for row in self.events() if row["kind"] == "review"]
        formal = [row for row in reviews if "#pullrequestreview-" in row["source_ref"]]
        self.assertEqual(len(formal), 1)
        self.assertEqual(formal[0]["source_ref"], URL + "pull/1#pullrequestreview-12")
        self.assertEqual(formal[0]["error"], "Changes requested")

    def test_nullable_check_metadata_preserves_failed_run_evidence(self):
        cases = [
            (dict(html_url=None), "source_ref", URL + "check-runs/1"),
            (dict(app=None), "agent", "unknown"),
            (dict(app=dict(name="Example checker")), "agent", "Example checker"),
            (dict(completed_at=None), "occurred", DATE),
        ]
        for index, (changes, field, expected) in enumerate(cases):
            with self.subTest(field=field, changes=changes):
                self.fake.rest["check-suites/11/check-runs"][1][0] = dict(check(), **changes)
                name = f"nullable-{index}.jsonl"
                self.run_adapter("--output", name)
                row = next(row for row in self.events(name) if row["kind"] == "ci")
                self.assertEqual(row[field], expected)
                self.assertIn("failure", row["error"])

    def test_missing_check_occurrence_time_rejects_incomplete_evidence(self):
        self.fake.rest["check-suites/11/check-runs"][1][0].update(completed_at=None, started_at=None)
        self.run_adapter(code=2)
        self.assertFalse(self.store.exists())

    def test_comment_without_optional_body_does_not_abort_other_evidence(self):
        row = comment(9)
        del row["body"]
        self.fake.rest["issues/1/comments"] = [row]
        self.run_adapter()
        self.assertEqual(len(self.events()), 10)

    def test_bot_imperative_requests_are_ingested_without_priority_tags(self):
        self.fake.rest["pulls/1/comments"] = [comment(9, "Please validate the example field.", True)]
        self.run_adapter()
        matching = [row for row in self.events() if row["error"] == "Please validate the example field."]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["kind"], "review")
        self.assertEqual(matching[0]["agent"], "example-bot")

    def test_repeated_rest_page_is_incomplete_without_publishing(self):
        page = [comment(number) for number in range(1, 101)]
        self.fake.override = lambda command, kwargs: (
            subprocess.CompletedProcess(command, 0, json.dumps(page), "")
            if "pulls/1/comments?" in command[6] else None)
        self.run_adapter(code=2)
        self.assertFalse(self.store.exists())
        self.assertEqual(sum("pulls/1/comments?" in command[6] for command, _ in self.fake.calls), 2)

    def test_cli_with_synthetic_gh_executable_writes_only_private_batch(self):
        executable = self.root / "gh"
        executable.write_text(
            "#!/usr/bin/env python3\nimport sys\n"
            f"sys.path[:0] = {[str(Path(sources.__file__).parent), str(Path(__file__).parent)]!r}\n"
            "from test_github_sources import FakeGitHub\n"
            "payload = sys.stdin.read() if 'graphql' in sys.argv else None\n"
            "result = FakeGitHub().run(['gh', *sys.argv[1:]], input=payload, capture_output=True, text=True, check=False)\n"
            "print(result.stdout)\n")
        executable.chmod(0o700)
        result = subprocess.run([sys.executable, sources.__file__, "--repo", "example/project", "--pr", "1"],
                                cwd=self.root, env=dict(os.environ, PATH=str(self.root) + os.pathsep + os.environ["PATH"]),
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["events"], 10)
        self.assertNotIn("example", result.stdout + result.stderr)
        batches = list(self.store.glob("github-events-*.jsonl"))
        self.assertEqual(len(batches), 1)
        self.assertEqual(len(self.events(batches[0].name)), 10)

    def test_api_failures_and_late_malformed_data_publish_nothing_and_hide_raw_text(self):
        scenarios = [
            subprocess.CompletedProcess([], 1, "private-stdout-canary", "private-stderr-canary"),
            subprocess.CompletedProcess([], 0, '{"errors":[{"message":"private-error-canary"}],"data":{}}', ""),
            subprocess.CompletedProcess([], 0, "private-json-canary", ""),
            subprocess.CompletedProcess([], 0, '{"data":{},"data":{}}', ""),
        ]
        for response in scenarios:
            with self.subTest(response=response):
                self.fake.override = lambda command, kwargs: response
                stdout, stderr = self.run_adapter(code=2)
                self.assertEqual(stdout, "")
                self.assertNotIn("canary", stderr)
                self.assertFalse(self.store.exists())
        self.fake.override = None
        self.fake.rest["check-suites/11/check-runs"][1][-1]["conclusion"] = "private-shape-canary"
        stdout, stderr = self.run_adapter(code=2)
        self.assertNotIn("canary", stdout + stderr)
        self.assertFalse(self.store.exists())

    def test_bad_counts_and_cursor_loops_fail_closed(self):
        for mode in ("rest-count", "graph-count", "cursor"):
            with self.subTest(mode=mode):
                self.fake = FakeGitHub()
                self.fake.graph_page_size = 1
                def override(command, kwargs):
                    if mode == "rest-count" and "check-runs?" in command[6]:
                        return subprocess.CompletedProcess(command, 0, json.dumps(dict(total_count=2, check_runs=[check()])), "")
                    if mode != "rest-count" and command[6] == "graphql" and json.loads(kwargs["input"])["query"] == sources.HISTORY:
                        page = dict(nodes=[commit()], totalCount=3, pageInfo=dict(hasNextPage=mode == "cursor", endCursor="same"))
                        return subprocess.CompletedProcess(command, 0, json.dumps(dict(data=dict(repository=dict(defaultBranchRef=dict(target=dict(history=page)))))), "")
                self.fake.override = override
                self.run_adapter(code=2)
                self.assertFalse(self.store.exists())

    def test_refuses_existing_output_unsafe_paths_and_public_store_permissions_before_calls(self):
        self.run_adapter()
        original = (self.store / "github-events.jsonl").read_bytes()
        for args in ((), ("--output", "../outside.jsonl"), ("--store", str(self.root / "public"))):
            self.fake.calls.clear()
            self.run_adapter(*args, code=2)
            self.assertEqual(self.fake.calls, [])
            self.assertEqual((self.store / "github-events.jsonl").read_bytes(), original)
        self.store.chmod(0o755)
        self.run_adapter("--output", "other.jsonl", code=2)
        self.assertFalse((self.store / "other.jsonl").exists())
        self.store.chmod(0o700)
        (self.store / "linked.jsonl").symlink_to(self.root / "outside.jsonl")
        self.run_adapter("--output", "linked.jsonl", code=2)
        self.assertFalse((self.root / "outside.jsonl").exists())

    def failure_records(self):
        return [json.loads(path.read_text()) for path in sorted((self.store / "failures").glob("*.json"))]

    def test_events_are_imported_into_the_store_in_the_same_step(self):
        stdout, _ = self.run_adapter("--pr", "1")
        summary = json.loads(stdout)
        records = self.failure_records()
        self.assertEqual(len(records), summary["events"])
        self.assertEqual(summary["imported_records"], summary["events"])
        self.assertEqual(summary["skipped_duplicates"], 0)
        self.assertEqual(sorted(row["source_ref"] for row in records),
                         sorted(row["source_ref"] for row in self.events()))

    def test_second_run_with_a_new_output_skips_duplicates(self):
        self.run_adapter("--pr", "1")
        stdout, _ = self.run_adapter("--pr", "1", "--output", "second.jsonl")
        summary = json.loads(stdout)
        self.assertEqual((summary["imported_records"], summary["skipped_duplicates"]), (0, summary["events"]))
        self.assertEqual(len(self.failure_records()), summary["events"])

    def test_default_output_is_unique_and_repeated_runs_deduplicate(self):
        self.run_adapter("--pr", "1", default_output=True)
        first = {path.name: path.read_bytes() for path in self.store.glob("*.jsonl")}
        stdout, _ = self.run_adapter("--pr", "1", default_output=True)
        files = list(self.store.glob("*.jsonl"))
        self.assertEqual(len(files), 2)
        for name, content in first.items():
            self.assertEqual((self.store / name).read_bytes(), content)
        self.assertEqual(json.loads(stdout)["imported_records"], 0)
        self.assertEqual(len(self.failure_records()), json.loads(stdout)["events"])

    def historical_codex_record(self, severity="P2", **kwargs):
        finding = self.codex_fixture(severity=severity, merged=True, **kwargs)
        self.run_adapter("--pr", "1", default_output=True)
        record = next(row for row in self.failure_records()
                      if row["agent"] == "codex-review" and row["command"].startswith(f"PR review finding {severity}:"))
        record["command"] = f"PR review finding {severity}: false alarm"
        record["thread"] = "https://github.com/example/project/pull/1"
        record["group_id"] = record["signature"] = coldfix.group_id(record)
        path = self.store / "failures" / (record["id"] + ".json")
        path.write_text(coldfix.encoded(record))
        return finding, record, path

    def test_migration_relabels_p2_p3_and_is_idempotent(self):
        for severity in ("P2", "P3"):
            with self.subTest(severity=severity):
                self.fake = FakeGitHub()
                _, original, path = self.historical_codex_record(severity)
                legacy = dict(coldfix.new_record(), repo="example", command="check", error="Example failure",
                              agent="worker", thread="", source="manual")
                legacy["group_id"] = coldfix.group_id(legacy)
                coldfix.save(self.store, "failures", legacy)
                stdout, _ = self.run_adapter("--migrate-codex-labels")
                self.assertEqual(json.loads(stdout)["relabeled_records"], 1)
                updated = json.loads(path.read_text())
                self.assertEqual(updated["command"], f"PR review finding {severity}: unresolved")
                self.assertEqual(updated["id"], original["id"])
                self.assertEqual(updated["group_id"], coldfix.group_id(updated))
                self.assertEqual(updated["signature"], updated["group_id"])
                self.assertEqual(json.loads((self.store / "codex-label-backup" / path.name).read_text()), original)
                coldfix.load_store(self.store)
                before = path.read_bytes()
                self.fake.calls.clear()
                stdout, _ = self.run_adapter("--migrate-codex-labels")
                self.assertEqual(json.loads(stdout)["relabeled_records"], 0)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(self.fake.calls, [])

    def test_migration_preserves_explicit_dismissal_open_pr_and_fixed_finding(self):
        _, original, path = self.historical_codex_record()
        before = path.read_bytes()
        for mode in ("dismissed", "open", "fixed", "other-repo"):
            with self.subTest(mode=mode):
                self.fake = FakeGitHub()
                self.codex_fixture(merged=mode != "open", later=mode == "fixed",
                                   reaction="-1" if mode == "dismissed" else None)
                if mode == "other-repo":
                    changed = dict(original, thread="https://github.com/another/project/pull/1")
                    path.write_text(coldfix.encoded(changed))
                stdout, _ = self.run_adapter("--migrate-codex-labels")
                self.assertEqual(json.loads(stdout)["relabeled_records"], 0)
                if mode != "other-repo":
                    self.assertEqual(path.read_bytes(), before)

    def test_migration_archives_old_duplicate_when_corrected_finding_exists(self):
        _, original, path = self.historical_codex_record()
        self.run_adapter("--pr", "1", default_output=True)
        corrected = next(row for row in self.failure_records()
                         if row["agent"] == "codex-review" and row["command"].endswith(": unresolved"))
        stdout, _ = self.run_adapter("--migrate-codex-labels")
        self.assertEqual(json.loads(stdout)["archived_duplicates"], 1)
        self.assertFalse(path.exists())
        self.assertEqual(json.loads((self.store / "codex-label-backup" / path.name).read_text()), original)
        records = [row for row in self.failure_records() if row["agent"] == "codex-review"]
        self.assertEqual(records, [corrected])
        stdout, _ = self.run_adapter("--migrate-codex-labels")
        self.assertEqual(json.loads(stdout)["archived_duplicates"], 0)
        coldfix.load_store(self.store)

    def test_import_existing_recovers_interrupted_default_batch(self):
        import_batch = sources.import_batch
        with patch.object(sources, "import_batch", side_effect=OSError("synthetic interruption")):
            self.run_adapter("--pr", "1", default_output=True, code=2)
        self.assertEqual(self.failure_records(), [])
        with patch.object(sources, "import_batch", wraps=import_batch):
            stdout, _ = self.run_adapter("--import-existing")
        self.assertEqual(json.loads(stdout)["files"], 1)
        self.assertEqual(json.loads(stdout)["imported_records"], 10)
        stdout, _ = self.run_adapter("--import-existing")
        self.assertEqual(json.loads(stdout)["imported_records"], 0)

    def test_migration_refuses_referenced_evidence_before_writes(self):
        _, original, path = self.historical_codex_record()
        duplicate = dict(original, **coldfix.new_record())
        duplicate["version"] = 2
        coldfix.save(self.store, "failures", duplicate)
        proposal = coldfix.new_record()
        proposal.update(group_id=original["group_id"], kind="test", title="Example proposal",
                        rule="Check the example", check_command="python3 check.py", evidence=sorted([original["id"], duplicate["id"]]))
        coldfix.save(self.store, "proposals", proposal)
        before = path.read_bytes()
        self.run_adapter("--migrate-codex-labels", code=2)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((self.store / "codex-label-backup").exists())
        coldfix.load_store(self.store)

    def migration_with_waiting_writer(self, mode):
        _, original, _ = self.historical_codex_record()
        duplicate = dict(original, **coldfix.new_record())
        duplicate["version"] = 2
        coldfix.save(self.store, "failures", duplicate)
        runner = ("import fcntl, runpy, sys\n"
                  "original = fcntl.flock\n"
                  "def notified(fd, operation):\n"
                  "    if operation == fcntl.LOCK_EX: print('waiting', flush=True)\n"
                  "    return original(fd, operation)\n"
                  "fcntl.flock = notified\n"
                  "sys.argv = sys.argv[1:]\n"
                  "runpy.run_path(sys.argv[0], run_name='__main__')\n")
        command = [sys.executable, "-c", runner, coldfix.__file__, "--store", str(self.store),
                   "propose", "--group", original["group_id"], "--kind", "test", "--title", "Example",
                   "--rule", "Check the example", "--check-command", "python3 check.py"]
        if mode == "ingest":
            batch = next(self.store.glob("github-events-*.jsonl"))
            command = [sys.executable, "-c", runner, coldfix.__file__, "--store", str(self.store),
                       "ingest", "--input", str(batch)]
        elif mode == "import-existing":
            command = [sys.executable, "-c", runner, sources.__file__, "--store", str(self.store),
                       "--import-existing"]
        children = []
        collect = sources.collect
        def concurrent_propose(*args, **kwargs):
            child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            children.append(child)
            self.assertEqual(child.stdout.readline(), "waiting\n")
            return collect(*args, **kwargs)
        try:
            with patch.object(sources, "collect", side_effect=concurrent_propose):
                self.run_adapter("--migrate-codex-labels")
            stdout, stderr = children[0].communicate(timeout=10)
            if mode == "propose":
                self.assertEqual(children[0].returncode, 2, stdout + stderr)
                self.assertIn("unknown failure group", stderr)
            else:
                self.assertEqual(children[0].returncode, 0, stdout + stderr)
                self.assertEqual(json.loads(stdout)["imported_records"], 0)
                records = [row for row in self.failure_records() if row["agent"] == "codex-review"]
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["command"], "PR review finding P2: unresolved")
            self.assertEqual(list((self.store / "proposals").glob("*.json")), [])
            coldfix.load_store(self.store)
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.communicate()

    def test_propose_waits_for_migration_and_reloads_changed_evidence(self):
        self.migration_with_waiting_writer("propose")

    def test_ingest_waits_for_migration_and_deduplicates_corrected_finding(self):
        self.migration_with_waiting_writer("ingest")

    def test_batch_import_waits_for_migration_and_deduplicates_corrected_finding(self):
        self.migration_with_waiting_writer("import-existing")

    def test_migration_preserves_measurement_snapshot(self):
        _, _, path = self.historical_codex_record()
        result = subprocess.run([sys.executable, coldfix.__file__, "--store", str(self.store), "measure"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertTrue(list((self.store / "measurements").glob("*.json")))
        before = path.read_bytes()
        _, stderr = self.run_adapter("--migrate-codex-labels", code=2)
        self.assertIn("measurement evidence", stderr)
        self.assertEqual(path.read_bytes(), before)
        coldfix.load_store(self.store)

    def test_migration_resumes_after_backup_without_changing_the_original(self):
        _, original, path = self.historical_codex_record()
        create = coldfix.atomic_create
        def interrupted(target, content, **kwargs):
            if Path(target) == path:
                raise OSError("synthetic interruption")
            return create(target, content, **kwargs)
        with patch.object(coldfix, "atomic_create", side_effect=interrupted):
            self.run_adapter("--migrate-codex-labels", code=2)
        self.assertEqual(json.loads(path.read_text()), original)
        backup = self.store / "codex-label-backup" / path.name
        self.assertEqual(json.loads(backup.read_text()), original)
        stdout, _ = self.run_adapter("--migrate-codex-labels")
        self.assertEqual(json.loads(stdout)["relabeled_records"], 1)
        self.assertEqual(json.loads(backup.read_text()), original)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)

    def test_migration_api_failure_leaves_records_unchanged(self):
        _, _, path = self.historical_codex_record()
        before = path.read_bytes()
        self.fake.override = lambda *args: subprocess.CompletedProcess([], 1, "secret-canary", "secret-canary")
        stdout, stderr = self.run_adapter("--migrate-codex-labels", code=2)
        self.assertNotIn("secret-canary", stdout + stderr)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((self.store / "codex-label-backup").exists())

    def test_existing_default_output_says_why_and_changes_nothing(self):
        self.run_adapter("--pr", "1")
        before = len(self.failure_records())
        _, stderr = self.run_adapter("--pr", "1", code=2)
        self.assertIn("already exists", stderr)
        self.assertNotIn("incomplete GitHub data", stderr)
        self.assertEqual(len(self.failure_records()), before)

    def test_import_existing_loads_every_batch_and_audit_file_once(self):
        self.store.mkdir(mode=0o700)
        (self.store / "github-audit").mkdir(mode=0o700)
        self.run_adapter("--pr", "1", "--output", "github-example-pr1-20261029.jsonl")
        batch = self.store / "github-example-pr1-20261029.jsonl"
        (self.store / "github-example-pr2-20261029.jsonl").write_bytes(batch.read_bytes())
        (self.store / "github-example-pr3-20261029.jsonl").touch()
        (self.store / "github-audit" / "verified-events.jsonl").write_text(
            json.dumps(dict(self.events("github-example-pr1-20261029.jsonl")[0], command="audit-only")) + "\n")
        (self.store / "github-events.jsonl").write_text("not json\n")
        for path in sorted(self.store.glob("failures/*.json")):
            path.unlink()
        for _ in range(2):
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertEqual(sources.main(["--import-existing", "--store", str(self.store)]), 0, stderr.getvalue())
        expected = len(self.events("github-example-pr1-20261029.jsonl")) + 1
        self.assertEqual(len(self.failure_records()), expected)
        self.assertEqual(json.loads(stdout.getvalue())["imported_records"], 0)

    def test_import_existing_reports_the_bad_file_and_keeps_earlier_imports(self):
        self.store.mkdir(mode=0o700)
        self.run_adapter("--pr", "1", "--output", "github-example-pr1-20261029.jsonl")
        (self.store / "github-example-pr2-20261029.jsonl").write_text("not json\n")
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(sources.main(["--import-existing", "--store", str(self.store)]), 2)
        self.assertIn("github-example-pr2-20261029.jsonl", stderr.getvalue())
        self.assertNotIn("not json", stderr.getvalue())

    def test_empty_default_output_from_an_earlier_run_is_replaced(self):
        self.store.mkdir(mode=0o700)
        (self.store / "github-events.jsonl").touch()
        self.run_adapter()
        self.assertTrue(self.events())

    def test_nonempty_output_is_still_never_overwritten(self):
        self.run_adapter()
        original = (self.store / "github-events.jsonl").read_bytes()
        self.run_adapter(code=2)
        self.assertEqual((self.store / "github-events.jsonl").read_bytes(), original)

    def test_source_text_stays_data_and_never_reaches_shell_or_diagnostics(self):
        self.fake.history[0]["message"] = "fix: $(touch example-marker) `example-command` private-text-canary"
        stdout, stderr = self.run_adapter()
        self.assertNotIn("private-text-canary", stdout + stderr)
        self.assertIn("private-text-canary", self.events()[0]["error"])
        for command, kwargs in self.fake.calls:
            self.assertNotIn("private-text-canary", str(command) + str(kwargs.get("input")))
        self.assertFalse((self.root / "example-marker").exists())


def run_row(number, name="example build", conclusion="failure", created_at=DATE, updated_at=DATE):
    return dict(id=number, name=name, display_title=f"Example title {number}", conclusion=conclusion,
                created_at=created_at, updated_at=updated_at, html_url=URL + f"actions/runs/{number}")


def commit_row(number, message):
    return dict(sha=f"{number:040x}", html_url=URL + f"commit/{number}",
                commit=dict(message=message, committer=dict(date=DATE)), author=dict(login="example-user"))


RUN_STATUSES = {"completed", "action_required", "cancelled", "failure", "neutral", "skipped", "stale", "success",
                "timed_out", "in_progress", "queued", "requested", "waiting", "pending"}


class FakePublicGitHub:
    def __init__(self):
        self.repos = {
            "example/alpha": dict(private=False, runs=[run_row(1), run_row(2)],
                                  commits=[commit_row(1, "fix: example mismatch"),
                                           commit_row(2, 'Revert "example change"'),
                                           commit_row(3, "docs: unrelated")]),
            "example/beta": dict(private=False, runs=[run_row(3)], commits=[]),
        }
        self.remaining = 5000
        self.calls = []

    def run(self, command, **kwargs):
        self.calls.append(command)
        assert command[:7] == ["gh", "api", "--hostname", "github.com", "--method", "GET", command[6]]
        assert "input" not in kwargs or kwargs["input"] is None
        parts = urlsplit(command[6])
        query = parse_qs(parts.query)
        if parts.path == "rate_limit":
            value = dict(resources=dict(core=dict(remaining=self.remaining, reset=1_800_000_000)))
        else:
            _, owner, name, *rest = parts.path.split("/")
            repo = self.repos[f"{owner}/{name}"]
            if not rest:
                value = dict(private=repo["private"])
            else:
                self.remaining -= 1
                page = int(query["page"][0])
                rows = repo["runs" if rest[-1] == "runs" else "commits"]
                if rest[-1] == "runs":
                    status = query["status"][0]
                    assert status in RUN_STATUSES, f"unsupported status filter {status}"
                    rows = [row for row in rows if (status == "completed" or row["conclusion"] == status)
                            and row["created_at"] >= query["created"][0][2:]]
                value = rows[(page - 1) * 100:page * 100]
                if rest[-1] == "runs":
                    value = dict(workflow_runs=value, total_count=len(rows))
        return subprocess.CompletedProcess(command, 0, json.dumps(value), "")


class PublicRepoTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = self.root / ".coldfix"
        self.allowlist = self.root / "allowlist.txt"
        self.allowlist.write_text("# public OSS\nexample/alpha\n\nexample/beta  # second\nexample/alpha\n")
        self.fake = FakePublicGitHub()

    def run_adapter(self, *args, code=0):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sources.subprocess, "run", side_effect=self.fake.run), redirect_stdout(stdout), redirect_stderr(stderr):
            result = sources.main(["--oss-allowlist", str(self.allowlist), "--store", str(self.store),
                                   "--since", DATE, *args])
        self.assertEqual(result, code, stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        return json.loads(stdout.getvalue()) if stdout.getvalue() else None

    def test_dry_run_reports_records_per_repo_and_writes_nothing(self):
        summary = self.run_adapter("--dry-run")
        by_repo = {row["repo"]: row for row in summary["repos"]}
        self.assertEqual(list(by_repo), ["example/alpha", "example/beta"])
        self.assertEqual((by_repo["example/alpha"]["records"], by_repo["example/alpha"]["ci"],
                          by_repo["example/alpha"]["issue"], by_repo["example/alpha"]["revert"]), (4, 2, 1, 1))
        self.assertEqual(by_repo["example/beta"]["records"], 1)
        self.assertEqual(summary["records"], 5)
        self.assertFalse(self.store.exists())

    def test_gather_is_read_only_and_imports_once(self):
        first = self.run_adapter("--output", "github-oss.jsonl")
        self.assertEqual((first["imported_records"], first["skipped_duplicates"]), (5, 0))
        rows = [json.loads(line) for line in (self.store / "github-oss.jsonl").read_text().splitlines()]
        self.assertEqual({row["kind"] for row in rows}, {"ci", "issue", "revert"})
        self.assertEqual({row["repo"] for row in rows}, {"example/alpha", "example/beta"})
        self.assertTrue(all(call[5] == "GET" for call in self.fake.calls))
        second = self.run_adapter("--output", "github-oss-2.jsonl")
        self.assertEqual((second["imported_records"], second["skipped_duplicates"]), (0, 5))

    def test_private_repo_is_refused_and_other_repos_still_gather(self):
        self.fake.repos["example/alpha"]["private"] = True
        summary = self.run_adapter("--dry-run", code=3)
        self.assertEqual([(row["repo"], row["status"]) for row in summary["repos"]],
                         [("example/alpha", "failed"), ("example/beta", "ok")])
        self.assertEqual(summary["records"], 1)
        self.assertFalse(any("actions/runs" in call[6] for call in self.fake.calls if "alpha" in call[6]))

    def test_stops_when_rate_limit_is_spent_and_skips_remaining_repos(self):
        self.fake.remaining = 2
        summary = self.run_adapter("--dry-run", code=3)
        self.assertEqual([row["status"] for row in summary["repos"]], ["rate_limited", "skipped_rate_limited"])
        self.assertEqual(summary["records"], 0)
        self.assertLessEqual(len([call for call in self.fake.calls if call[6] != "rate_limit"]), 2)

    def test_exhausted_limit_makes_no_repo_requests(self):
        self.fake.remaining = 0
        summary = self.run_adapter("--dry-run", code=3)
        self.assertEqual(summary["records"], 0)
        self.assertEqual([call[6] for call in self.fake.calls], ["rate_limit"])

    def test_full_page_of_runs_fetches_the_next_page(self):
        self.fake.repos["example/beta"]["runs"] = [run_row(number) for number in range(1, 102)]
        summary = self.run_adapter("--dry-run")
        self.assertEqual(next(row for row in summary["repos"] if row["repo"] == "example/beta")["ci"], 101)

    def test_more_failed_runs_than_github_exposes_is_reported_incomplete(self):
        self.fake.repos["example/beta"]["runs"] = [run_row(number) for number in range(1, 1102)]
        summary = self.run_adapter("--dry-run", code=3)
        beta = next(row for row in summary["repos"] if row["repo"] == "example/beta")
        self.assertEqual((beta["status"], beta["ci"]), ("incomplete", 1000))

    def test_timed_out_and_action_required_runs_are_gathered_as_ci_failures(self):
        self.fake.repos["example/beta"]["runs"] = [
            run_row(1, conclusion="timed_out"), run_row(2, conclusion="action_required"),
            run_row(3, conclusion="startup_failure"), run_row(4, conclusion="success")]
        summary = self.run_adapter("--dry-run")
        beta = next(row for row in summary["repos"] if row["repo"] == "example/beta")
        self.assertEqual((beta["status"], beta["ci"]), ("ok", 3))

    def test_run_created_before_since_but_failed_after_it_is_in_scope(self):
        self.fake.repos["example/beta"]["runs"] = [
            run_row(1, created_at="2026-10-15T12:00:00Z", updated_at="2026-10-21T12:00:00Z"),
            run_row(2, created_at="2026-10-15T12:00:00Z", updated_at="2026-10-16T12:00:00Z"),
            run_row(3, created_at="2026-08-01T12:00:00Z", updated_at="2026-10-21T12:00:00Z")]
        summary = self.run_adapter("--dry-run")
        beta = next(row for row in summary["repos"] if row["repo"] == "example/beta")
        self.assertEqual(beta["ci"], 1)

    def test_import_existing_finds_oss_batches(self):
        self.run_adapter("--output", "github-oss.jsonl")
        other = self.root / "other" / ".coldfix"
        other.mkdir(parents=True, mode=0o700)
        (other / "github-oss-1.jsonl").write_text((self.store / "github-oss.jsonl").read_text())
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(sources.main(["--import-existing", "--store", str(other)]), 0)
        self.assertEqual((json.loads(stdout.getvalue())["files"], json.loads(stdout.getvalue())["imported_records"]), (1, 5))

    def test_bad_allowlist_or_flags_fail_before_any_call(self):
        self.allowlist.write_text("example/alpha\nnot a repo\n")
        self.run_adapter("--dry-run", code=2)
        self.assertEqual(self.fake.calls, [])
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            self.assertEqual(sources.main(["--dry-run"]), 2)
            self.assertEqual(sources.main(["--oss-allowlist", str(self.allowlist), "--repo", "example/alpha"]), 2)
        self.assertEqual(self.fake.calls, [])


if __name__ == "__main__":
    unittest.main()
