import argparse
import copy
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import ci_fix_learning as ci
import coldfix as c

ROOT = Path(ci.__file__).parent
WORKFLOW = '''name: verify
on: [push, pull_request]
jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Test
        run: python3 -m unittest discover -s tests -v
'''


def workflow_run(number, sha, conclusion="failure", day=1, branch="main", prs=()):
    return dict(id=number, head_sha=sha, conclusion=conclusion, workflow_id=7,
                path=".github/workflows/verify.yml", event="push", head_branch=branch,
                head_repository=dict(full_name="example/project"),
                pull_requests=[dict(number=n) for n in prs], run_attempt=1,
                created_at=f"2026-10-{day:02}T12:00:00Z")


def job(number=10, conclusion="failure", name="verify"):
    return dict(id=number, name=name, conclusion=conclusion, check_run_url=f"https://api.github.com/repos/example/project/check-runs/{number}",
                steps=[dict(name="Test", conclusion=conclusion)])


class FakeGitHub(ci.GitHub):
    def __init__(self, runs, jobs):
        super().__init__("example/project")
        self.runs, self.jobs = runs, jobs
        self.comparisons = []

    def pages(self, endpoint, key=None, query="", bounded=False, limit=None):
        if endpoint == "actions/runs":
            return self.runs
        if endpoint == "commits":
            return []
        return self.jobs[int(endpoint.split("/")[2])]

    def successor(self, before, after):
        self.comparisons.append((before, after))
        return before != after and after != "f" * 40


class PairingTests(unittest.TestCase):
    def test_same_workflow_job_context_later_distinct_descendant(self):
        red = workflow_run(1, "a" * 40)
        wrong_branch = workflow_run(2, "b" * 40, "success", 2, "other")
        same_sha = workflow_run(3, "a" * 40, "success", 3)
        wrong_workflow = dict(workflow_run(4, "c" * 40, "success", 4), workflow_id=8)
        wrong_job = workflow_run(5, "d" * 40, "success", 5)
        rewritten = workflow_run(6, "f" * 40, "success", 6)
        green = workflow_run(7, "e" * 40, "success", 7)
        gh = FakeGitHub([red, wrong_branch, same_sha, wrong_workflow, wrong_job, rewritten, green],
                       {1: [job()], 3: [job(30, "success", "other")], 5: [job(50, "success", "other")], 7: [job(70, "success")]})
        result = ci.collect(gh, 20)
        self.assertEqual([(r.run["id"], r.green[0]["id"], r.green[1]["id"]) for r in result], [(1, 7, 70)])
        self.assertEqual(gh.comparisons, [("a" * 40, "d" * 40), ("a" * 40, "f" * 40), ("a" * 40, "e" * 40)])
        self.assertFalse(ci.same_context(dict(red, pull_requests=[dict(number=1)]), dict(red, pull_requests=[dict(number=2)]), gh.repo))
        self.assertTrue(ci.same_context(dict(red, pull_requests=[dict(number=1)]), dict(red, head_branch="renamed", pull_requests=[dict(number=1)]), gh.repo))
        self.assertFalse(ci.same_context(red, dict(red, head_repository=dict(full_name="fork/project")), gh.repo))

    def test_prior_failed_attempt_and_green_job_in_failed_workflow(self):
        original = workflow_run(1, "a" * 40)
        latest = dict(original, run_attempt=2, conclusion="success")
        green = workflow_run(2, "b" * 40, "failure", 2)
        gh = FakeGitHub([latest, green], {1: [job()], 2: [job(20, "success")]})
        with patch.object(gh, "api", return_value=original) as api:
            rows = ci.collect(gh, 20)
        api.assert_called_once_with("actions/runs/1/attempts/1")
        self.assertEqual((rows[-1].run["id"], rows[-1].run["run_attempt"], rows[-1].green[1]["id"]), (1, 1, 20))

    def test_pairing_read_failure_holds_affected_candidate_and_retains_earlier_pair(self):
        red = workflow_run(1, "a" * 40, day=2)
        green = workflow_run(2, "b" * 40, "success", 3)
        other = workflow_run(3, "c" * 40, day=1)
        gh = FakeGitHub([red, green, other], {1: [job()], 2: [job(20, "success")], 3: [job(30)]})
        successor = gh.successor
        def compare(before, after):
            if before == "c" * 40:
                raise ci.Skipped("read unavailable")
            return successor(before, after)
        # Include a later run for the affected candidate.
        later = workflow_run(4, "d" * 40, "success", 4)
        gh.runs.append(later)
        gh.jobs[4] = [job(40, "success")]
        with patch.object(gh, "successor", side_effect=compare):
            rows = ci.collect(gh, 20)
        self.assertEqual(len(rows), 2)
        self.assertIsNotNone(rows[0].green)
        self.assertIsNone(rows[1].green)
        self.assertEqual(rows[1].reason, "workflow pairing source unavailable")
        self.assertTrue(gh.incomplete)

    def test_prior_attempt_read_failure_keeps_current_run_pair(self):
        red = dict(workflow_run(1, "a" * 40), run_attempt=2)
        green = workflow_run(2, "b" * 40, "success", 2)
        gh = FakeGitHub([red, green], {1: [job()], 2: [job(20, "success")]})
        with patch.object(gh, "api", side_effect=ci.Skipped("read unavailable"), create=True):
            rows = ci.collect(gh, 20)
        self.assertIsNotNone(rows[0].green)
        self.assertIn("prior workflow attempt unavailable; current runs retained", gh.incomplete)

    def test_external_read_failure_preserves_discovered_workflow_pair(self):
        red = workflow_run(1, "a" * 40)
        green = workflow_run(2, "b" * 40, "success", 2)
        gh = FakeGitHub([red, green], {1: [job()], 2: [job(20, "success")]})
        pages = gh.pages
        def unavailable(endpoint, *args, **kwargs):
            if endpoint == "commits":
                raise ci.Skipped("read operation unavailable")
            return pages(endpoint, *args, **kwargs)
        with patch.object(gh, "pages", side_effect=unavailable):
            rows = ci.collect(gh, 20)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].green[1]["id"], 20)
        self.assertIn("external check enumeration unavailable; workflow results retained", gh.incomplete)

    def test_mixed_outcome_duplicate_green_job_name_is_ambiguous(self):
        red = workflow_run(1, "a" * 40)
        green = workflow_run(2, "b" * 40, "failure", 2)
        gh = FakeGitHub([red, green], {1: [job()], 2: [job(20, "success"), job(21)]})
        row = ci.collect(gh, 20)[0]
        self.assertIsNone(row.green)
        self.assertIn("no later green sharing history", row.reason)

    def test_no_green_and_startup_are_explicit_and_cap_is_reported(self):
        gh = FakeGitHub([workflow_run(1, "a" * 40), workflow_run(2, "b" * 40, day=2)], {1: [job()], 2: []})
        rows = ci.collect(gh, 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].reason, "workflow has no executed failed job; runner or startup unavailable")
        self.assertIn("max-failures", gh.incomplete[0])
        gh = FakeGitHub([workflow_run(1, "a" * 40)], {1: [job()]})
        self.assertEqual(ci.collect(gh, 20)[0].reason, "no later green sharing history for the same workflow, job and branch/PR")

    def test_approval_held_run_with_no_jobs_is_held_not_a_failure(self):
        gh = FakeGitHub([workflow_run(1, "a" * 40, "action_required")], {1: []})
        self.assertEqual(ci.collect(gh, 1), [])
        self.assertEqual(len(gh.held), 1)
        self.assertEqual(gh.incomplete, [])

    def test_approval_held_runs_do_not_consume_failure_cap(self):
        gh = FakeGitHub([workflow_run(1, "a" * 40, "action_required", day=2), workflow_run(2, "b" * 40)],
                       {1: [], 2: [job()]})
        rows = ci.collect(gh, 1)
        self.assertEqual([row.run["id"] for row in rows], [2])
        self.assertEqual(len(gh.held), 1)

    def test_startup_job_with_no_failed_steps_is_not_a_code_failure(self):
        gh = FakeGitHub([workflow_run(1, "a" * 40), workflow_run(2, "b" * 40, "success", 2)],
                       {1: [dict(job(), steps=[])]})
        row = ci.collect(gh, 1)[0]
        self.assertIn("startup unavailable", row.reason)
        self.assertIsNone(row.green)
        self.assertEqual(gh.comparisons, [])

    def test_external_commit_selection_is_bounded_before_more_requests(self):
        gh = ci.GitHub("example/project")
        with patch.object(gh, "api", return_value=[dict(sha="a" * 40), dict(sha="b" * 40)]) as api:
            self.assertEqual(len(gh.pages("commits", bounded=True, limit=2)), 2)
        api.assert_called_once_with("commits?per_page=2&page=1")
        self.assertEqual(gh.incomplete, ["commits reached selected item bound"])

    def test_large_external_suite_and_check_history_has_bounded_GET_count(self):
        gh = ci.GitHub("example/project")
        def response(command):
            endpoint = command[-1]
            if "/actions/runs?" in endpoint:
                value = dict(total_count=0, workflow_runs=[])
            elif "/commits?" in endpoint:
                value = [dict(sha="a" * 40)]
            elif "/check-suites?" in endpoint:
                value = dict(total_count=10000, check_suites=[dict(id=i, head_branch="main",
                    repository=dict(full_name=gh.repo)) for i in (1, 2)])
            else:
                suite = int(endpoint.split("check-suites/")[1].split("/")[0])
                value = dict(total_count=10000, check_runs=[dict(id=suite * 10 + i,
                    head_sha="a" * 40, conclusion="success", name="test", app=dict(id=99, slug="other"),
                    started_at="2026-10-01T12:00:00Z") for i in (1, 2)])
            return json.dumps(value).encode()
        with patch.object(ci, "run", side_effect=response):
            self.assertEqual(ci.collect(gh, 2), [])
        self.assertEqual(gh.requests, 5)
        self.assertEqual(len(gh.incomplete), 3)

    def test_paginated_GET_and_comparison_are_read_only(self):
        gh = ci.GitHub("example/project")
        commands = []
        def execute(command):
            commands.append(command)
            endpoint = command[-1]
            if endpoint.startswith("repos/example/project/compare/"):
                return json.dumps(dict(status="ahead", merge_base_commit=dict(sha="a" * 40), base_commit=dict(sha="a" * 40))).encode()
            page = int(endpoint.split("page=")[-1])
            rows = [dict(id=i) for i in (range(1, 101) if page == 1 else [101])]
            return json.dumps(dict(total_count=101, workflow_runs=rows)).encode()
        with patch.object(ci, "run", side_effect=execute):
            rows = gh.pages("actions/runs", "workflow_runs", bounded=True)
            self.assertEqual(rows[-1], dict(id=101))
            self.assertTrue(gh.successor("a" * 40, "b" * 40))
            self.assertFalse(gh.successor("a" * 40, "a" * 40))
        self.assertEqual(len(commands), 3)
        self.assertTrue(all(cmd[0:2] == ["gh", "api"] and cmd[cmd.index("--method") + 1] == "GET" for cmd in commands))
        self.assertTrue(all("logs" not in cmd[-1] for cmd in commands))

    def test_rebased_branch_green_is_a_successor_but_older_or_unrelated_commits_are_not(self):
        red, green, base = "a" * 40, "b" * 40, "c" * 40
        def commit(sha, day):
            return dict(sha=sha, commit=dict(committer=dict(date=f"2026-10-{day:02}T12:00:00Z")))
        def compare(status, merge_base, base_commit=red, green_day=3, ahead=None):
            commits = [commit(base, 1), commit(green, green_day)]
            return dict(status=status, merge_base_commit=dict(sha=merge_base), base_commit=commit(base_commit, 2),
                        ahead_by=len(commits) if ahead is None else ahead, commits=commits)
        cases = [(compare("ahead", red), True), (compare("diverged", base), True),
                 (compare("behind", green), False), (compare("identical", red), False),
                 (compare("ahead", base), False), (compare("diverged", base, base), False),
                 (compare("diverged", base, green_day=1), False), (compare("diverged", base, ahead=300), False)]
        for value, expected in cases:
            gh = ci.GitHub("example/project")
            with self.subTest(value=value), patch.object(gh, "api", return_value=value) as api:
                self.assertIs(gh.successor(red, green), expected)
                api.assert_called_once_with(f"compare/{red}...{green}")

    def test_identical_row_shifted_across_pages_counted_once(self):
        gh = ci.GitHub("example/project")
        pages = {1: list(range(1, 101)), 2: list(range(100, 200))}
        with patch.object(gh, "api", side_effect=lambda endpoint: dict(total_count=199, workflow_runs=[dict(id=i) for i in pages[int(endpoint.split("page=")[-1])]])):
            rows = gh.pages("actions/runs", "workflow_runs")
        self.assertEqual([row["id"] for row in rows], list(range(1, 200)))

    def test_pull_files_keep_distinct_paths_with_identical_contents(self):
        gh = ci.GitHub("example/project")
        files = [dict(filename=name, sha="a" * 40) for name in ("first.py", "second.py")]
        with patch.object(gh, "api", return_value=files):
            self.assertEqual(gh.pages("pulls/1/files"), files)

    def test_pull_files_reject_conflicting_rows_for_the_same_path(self):
        gh = ci.GitHub("example/project")
        files = [dict(filename="value.py", sha=char * 40) for char in "ab"]
        with patch.object(gh, "api", return_value=files):
            with self.assertRaises(c.ColdfixError):
                gh.pages("pulls/1/files")

    def test_limit_counts_unique_rows_after_shifted_duplicate(self):
        gh = ci.GitHub("example/project")
        pages = {1: list(range(1, 101)), 2: list(range(100, 200)), 3: list(range(200, 300))}
        with patch.object(gh, "api", side_effect=lambda endpoint: dict(total_count=300, workflow_runs=[dict(id=i) for i in pages[int(endpoint.split("page=")[-1])]])):
            rows = gh.pages("actions/runs", "workflow_runs", limit=150)
        self.assertEqual([row["id"] for row in rows], list(range(1, 151)))

    def test_conflicting_or_repeated_page_rejected(self):
        gh = ci.GitHub("example/project")
        pages = {1: [dict(id=1, status="queued")] + [dict(id=i) for i in range(2, 101)], 2: [dict(id=1, status="done")] + [dict(id=i) for i in range(101, 200)]}
        with patch.object(gh, "api", side_effect=lambda endpoint: dict(total_count=199, workflow_runs=pages[int(endpoint.split("page=")[-1])])):
            with self.assertRaises(c.ColdfixError):
                gh.pages("actions/runs", "workflow_runs")

    def test_duplicate_page_rejected_and_history_bound_explicit(self):
        gh = ci.GitHub("example/project")
        batch = dict(total_count=101, workflow_runs=[dict(id=i) for i in range(100)])
        with patch.object(gh, "api", return_value=batch):
            with self.assertRaises(c.ColdfixError):
                gh.pages("actions/runs", "workflow_runs")
        gh = ci.GitHub("example/project")
        with patch.object(gh, "api", side_effect=lambda endpoint: dict(total_count=2000, workflow_runs=[dict(id=i) for i in range((int(endpoint.split('page=')[-1]) - 1) * 100, int(endpoint.split('page=')[-1]) * 100)])):
            self.assertEqual(len(gh.pages("actions/runs", "workflow_runs", bounded=True)), 1000)
        self.assertEqual(gh.incomplete, ["actions/runs reached 1000-item history bound"])

    def test_owner_discovery_expands_instead_of_silently_truncating(self):
        commands = []
        def execute(command):
            commands.append(command)
            amount = 100 if len(commands) == 1 else 101
            return json.dumps([dict(nameWithOwner=f"example-owner/repo{i}", isPrivate=True) for i in range(amount)]).encode()
        with patch.object(ci, "run", side_effect=execute):
            repos = ci.discover("example-owner")
        self.assertEqual(len(repos), 101)
        self.assertEqual([cmd[cmd.index("--limit") + 1] for cmd in commands], ["100", "200"])
        self.assertTrue(all(cmd[:3] == ["gh", "repo", "list"] and "private" in cmd for cmd in commands))

    def test_external_check_pair_retained_as_unsupported(self):
        gh = ci.GitHub("example/project")
        checks = {"a" * 40: dict(id=11, head_sha="a" * 40, conclusion="failure", name="lint", app=dict(id=99, slug="other"), pull_requests=[], started_at="2026-10-01T12:00:00Z"),
                  "b" * 40: dict(id=22, head_sha="b" * 40, conclusion="success", name="lint", app=dict(id=99, slug="other"), pull_requests=[], started_at="2026-10-02T12:00:00Z")}
        def pages(endpoint, *args, **kwargs):
            if endpoint == "actions/runs":
                return []
            if endpoint == "commits":
                return [dict(sha=s) for s in checks]
            if endpoint.endswith("check-suites"):
                sha = endpoint.split("/")[1]
                return [dict(id=1 if sha[0] == "a" else 2, head_branch="main", repository=dict(full_name=gh.repo))]
            return [checks[("a" if "/1/" in endpoint else "b") * 40]]
        with patch.object(gh, "pages", side_effect=pages), patch.object(gh, "successor", return_value=True):
            rows = ci.collect(gh, 20)
        self.assertEqual((rows[0].source, rows[0].green[0]["id"], rows[0].reason), ("check", 22, "external check has no supported workflow command"))


class WorkflowTests(unittest.TestCase):
    def test_multiline_run_scalars_are_rejected_without_truncation(self):
        for value in (
            'python3 -m unittest discover\n          -s ci_tests',
            "'python3 -m unittest discover\n          -s ci_tests'",
            '"python3 -m unittest discover\n          -s ci_tests"',
            '>\n          python3 -m unittest discover\n          -s ci_tests',
            '>-\n          python3 -m unittest discover\n          -s ci_tests',
        ):
            with self.subTest(value=value), self.assertRaises(ci.Skipped):
                ci.workflow_plan(WORKFLOW.replace('python3 -m unittest discover -s tests -v', value), job())

    def test_setup_after_failed_check_cannot_change_its_selected_runtime(self):
        alternate = f"3.{sys.version_info.minor + 1}"
        later = WORKFLOW + f'      - uses: actions/setup-python@v5\n        with:\n          python-version: "{alternate}"\n'
        setup = '      - uses: actions/setup-python@v5\n        with:\n          python-version: "{}"\n'
        reset = WORKFLOW.replace("      - name: Test", setup.format(alternate) +
            setup.format(f"{sys.version_info.major}.{sys.version_info.minor}") + "      - name: Test")
        with patch.object(ci.shutil, "which", return_value=None):
            self.assertEqual(ci.workflow_plan(later, job())["command"][0], sys.executable)
        with patch.object(ci.shutil, "which", return_value="/opt/alternate/bin/python-alternate"):
            self.assertEqual(ci.workflow_plan(reset, job())["command"][0], "/opt/alternate/bin/python-alternate")

    def test_setup_python_version_that_is_not_installed_uses_the_local_python(self):
        for version in (f"{sys.version_info.major}.{sys.version_info.minor}", "3.x", "pypy3.10"):
            setup = f'      - uses: actions/setup-python@v5\n        with:\n          python-version: "{version}"\n          cache: pip\n'
            content = WORKFLOW.replace("      - name: Test", setup + "      - name: Test")
            with self.subTest(version=version), patch.object(ci.shutil, "which", return_value=None):
                self.assertEqual(ci.workflow_plan(content, job())["command"][0], sys.executable)

    def test_pip_install_steps_before_the_check_become_setup(self):
        for command, expected in [("pip install -r requirements.txt", ["install", "-r", "requirements.txt"]),
                                  ("python -m pip install --upgrade pip", ["install", "--upgrade", "pip"]),
                                  ("python3 -m pip install pytest 'ruff>=0.4,<1' -e .", ["install", "pytest", "ruff>=0.4,<1", "-e", "."]),
                                  ("pip install .[dev]", ["install", ".[dev]"])]:
            content = WORKFLOW.replace("      - name: Test", f"      - run: {command}\n      - name: Test")
            with self.subTest(command=command):
                self.assertEqual(ci.workflow_plan(content, job())["setup"], [expected])
        self.assertEqual(ci.workflow_plan(WORKFLOW, job())["setup"], [])

    def test_unquoted_pip_redirections_are_held(self):
        for requirement in ("pkg>=2", "pkg<3", "pkg>2", "pkg>>2", "pkg 2>2",
                            "pkg 2>>2", "pkg <>2", "pkg<<2", '"pkg>=2">2',
                            "pkg>=2,<3", "pkg>''2"):
            command = f"pip install {requirement}"
            content = WORKFLOW.replace("      - name: Test", f"      - run: {command}\n      - name: Test")
            with self.subTest(command=command), self.assertRaises(ci.Skipped):
                ci.workflow_plan(content, job())

    def test_quoted_pip_specifiers_keep_their_constraints(self):
        for requirement, expected in (("'pkg>=2'", "pkg>=2"), ('"pkg<3"', "pkg<3"),
                                      ("'pkg>=2,<3'", "pkg>=2,<3"), ("pkg'>'=2", "pkg>=2")):
            command = f"python3 -m pip install {requirement}"
            content = WORKFLOW.replace("      - name: Test", f"      - run: {command}\n      - name: Test")
            with self.subTest(command=command):
                self.assertEqual(ci.workflow_plan(content, job())["setup"], [["install", expected]])

    def test_unsafe_pip_install_steps_are_held(self):
        for command in ("pip install git+https://example.com/x.git", "pip install --index-url https://example.com/simple pkg",
                        "pip install -r /etc/passwd", "pip install -r ../outside.txt", "pip install pkg; curl example.com",
                        "pip install", "pip install ./local/path", "pip uninstall pkg", "curl example.com | sh", "pip install pkg | tee log"):
            content = WORKFLOW.replace("      - name: Test", f"      - run: {command}\n      - name: Test")
            with self.subTest(command=command), self.assertRaises(ci.Skipped):
                ci.workflow_plan(content, job())

    def test_pip_install_step_needs_the_default_checkout_and_no_step_settings(self):
        install = "      - run: pip install pkg\n"
        for content in (WORKFLOW.replace("      - uses: actions/checkout@v4\n", install + "      - uses: actions/checkout@v4\n"),
                        WORKFLOW.replace("      - name: Test", install.rstrip() + "\n        if: true\n      - name: Test"),
                        WORKFLOW.replace("      - name: Test", install.rstrip() + "\n        working-directory: src\n      - name: Test")):
            with self.subTest(content=content), self.assertRaises(ci.Skipped):
                ci.workflow_plan(content, job())

    MATRIX = WORKFLOW.replace("    steps:", "    strategy:\n      fail-fast: false\n      matrix:\n        python: ['3.9', \"3.10\"]\n        flavour:\n          - lite\n          - full\n    steps:"
        ).replace("-s tests -v", "-s ${{ matrix.flavour }} -v")

    def test_simple_matrix_expands_to_the_entry_named_by_the_failed_job(self):
        self.assertEqual(ci.workflow_plan(self.MATRIX, job(name="verify (3.9, lite)"))["command"][-3:], ["-s", "lite", "-v"])
        for name, flavour in (("verify (3.9, lite)", "lite"), ("verify (3.10, full)", "full"), ("verify (3.9, full)", "full")):
            with self.subTest(name=name):
                self.assertEqual(ci.workflow_plan(self.MATRIX, job(name=name))["command"][-2], flavour)
        with self.assertRaises(ci.Skipped):
            ci.workflow_plan(self.MATRIX, job(name="verify (3.8, lite)"))

    def test_matrix_entries_sharing_a_job_name_are_held_as_ambiguous(self):
        content = self.MATRIX.replace("    runs-on:", "    name: verify ${{ matrix.python }}\n    runs-on:", 1).replace("['3.9', \"3.10\"]", "['3.9']")
        with self.assertRaises(ci.Skipped):
            ci.workflow_plan(content, job(name="verify 3.9"))

    def test_pip_setup_before_a_typescript_check_is_held(self):
        content = WORKFLOW.replace("python3 -m unittest discover -s tests -v", "tsc --noEmit").replace("      - name: Test", "      - run: pip install pkg\n      - name: Test")
        with self.assertRaises(ci.Skipped):
            ci.workflow_plan(content, job())

    def test_matrix_value_selects_the_runner_and_input_before_they_are_checked(self):
        content = self.MATRIX.replace("ubuntu-latest", "${{ matrix.os }}").replace("        python:", "        os: [ubuntu-latest, windows-latest]\n        python:")
        self.assertEqual(ci.workflow_plan(content, job(name="verify (ubuntu-latest, 3.9, lite)"))["command"][-2], "lite")
        with self.assertRaises(ci.Skipped):
            ci.workflow_plan(content, job(name="verify (windows-latest, 3.9, lite)"))

    def test_unsupported_matrix_forms_and_other_expressions_are_held(self):
        for content in (self.MATRIX.replace("      matrix:\n", "      matrix:\n        include:\n          - flavour: odd\n"),
                        self.MATRIX.replace("flavour }}", "other }}"),
                        self.MATRIX.replace("      matrix:\n", "      matrix: ${{ fromJSON(x) }}\n"),
                        self.MATRIX.replace("          - lite", "          - ${{ github.ref }}"),
                        self.MATRIX.replace("-s ${{ matrix.flavour }}", "-s ${{ github.ref }}"),
                        WORKFLOW.replace("-s tests", "-s ${{ matrix.flavour }}")):
            with self.subTest(content=content), self.assertRaises(ci.Skipped):
                ci.workflow_plan(content, job())

    def test_checkout_and_preparation_semantics_are_not_silently_ignored(self):
        checkout = "      - uses: actions/checkout@v4\n"
        variants = [WORKFLOW.replace(checkout, checkout + "        with:\n          " + key + ": other\n")
                    for key in ("repository", "ref", "path", "fetch-depth", "submodules")]
        variants += [WORKFLOW.replace(checkout, checkout + "        with: {ref: main}\n"),
                     WORKFLOW.replace(checkout, checkout + "        if: false\n"),
                     WORKFLOW.replace(checkout, ""), WORKFLOW.replace(checkout, checkout * 2),
                     WORKFLOW.replace(checkout, checkout + "      - run: python3 prepare.py\n"),
                     WORKFLOW.replace(checkout, checkout + "      - uses: actions/setup-python@v5\n        with:\n          python-version-file: runtime.txt\n")]
        for content in variants:
            with self.subTest(content=content), self.assertRaises(ci.Skipped):
                ci.workflow_plan(content, job())
        duplicate = dict(job(), steps=[dict(name="Test", conclusion="failure"), dict(name="Test", conclusion="skipped")])
        with self.assertRaises(ci.Skipped):
            ci.workflow_plan(WORKFLOW, duplicate)

    def test_unsupported_step_settings_in_either_mapping_position_are_held(self):
        for setting in ("working-directory: src", "shell: python", "env: {}", "if: false"):
            for first in (True, False):
                entry = ("      - " + setting + "\n        name: Test" if first else
                         "      - name: Test\n        " + setting)
                content = WORKFLOW.replace("      - name: Test", entry)
                with self.subTest(setting=setting, first=first), self.assertRaises(ci.Skipped):
                    ci.workflow_plan(content, job())

    def test_unreadable_mapping_keys_are_held_at_each_scope(self):
        for indent, anchor in (("", "jobs:"), ("    ", "    steps:"), ("        ", "        run:")):
            for key in ('"env\\u0020"', '? [env]', '"unterminated'):
                content = WORKFLOW.replace(anchor, f"{indent}{key} : {{}}\n" + anchor)
                with self.subTest(indent=indent, key=key), self.assertRaises(ci.Skipped):
                    ci.workflow_plan(content, job())

    def test_supported_mapping_key_spellings_share_the_same_plan(self):
        expected = ci.workflow_plan(WORKFLOW, job())
        for quote in ("", "'", '"'):
            content = re.sub(r"^( *(?:- )?)([\w-]+):",
                             lambda m: f"{m[1]}{quote}{m[2]}{quote} :", WORKFLOW, flags=re.M)
            with self.subTest(quote=quote):
                self.assertEqual(ci.workflow_plan(content, job()), expected)

    def test_whitespace_step_keys_are_held(self):
        for key, value in (("working-directory", "src"), ("shell", "python"), ("env", "{}"), ("if", "false")):
            for quote in ("", "'", '"'):
                for first in (True, False):
                    setting = f"{quote}{key}{quote} : {value}"
                    entry = ("      - " + setting + "\n        name: Test" if first else
                             "      - name: Test\n        " + setting)
                    with self.subTest(setting=setting, first=first), self.assertRaises(ci.Skipped):
                        ci.workflow_plan(WORKFLOW.replace("      - name: Test", entry), job())

    def test_whitespace_job_keys_are_held(self):
        for key in ("defaults", "env", "if", "uses", "strategy", "container", "services"):
            for quote in ("", "'", '"'):
                content = WORKFLOW.replace("    steps:", f"    {quote}{key}{quote} : {{}}\n    steps:")
                with self.subTest(key=key, quote=quote), self.assertRaises(ci.Skipped):
                    ci.workflow_plan(content, job())

    def test_whitespace_workflow_keys_are_held(self):
        for key in ("defaults", "env"):
            for quote in ("", "'", '"'):
                content = WORKFLOW.replace("jobs:", f"{quote}{key}{quote} : {{}}\njobs:")
                with self.subTest(key=key, quote=quote), self.assertRaises(ci.Skipped):
                    ci.workflow_plan(content, job())

    def test_quoted_unsupported_step_settings_are_held(self):
        for key, value in (("working-directory", "src"), ("shell", "python"), ("env", "{}"), ("if", "false")):
            for quote in ('"', "'"):
                setting = f"{quote}{key}{quote}: {value}"
                for first in (True, False):
                    entry = ("      - " + setting + "\n        name: Test" if first else
                             "      - name: Test\n        " + setting)
                    content = WORKFLOW.replace("      - name: Test", entry)
                    with self.subTest(setting=setting, first=first), self.assertRaises(ci.Skipped):
                        ci.workflow_plan(content, job())

    def test_standalone_python_tools_use_the_selected_runtime(self):
        version = f"3.{sys.version_info.minor + 1}"
        setup = f'      - uses: actions/setup-python@v5\n        with:\n          python-version: "{version}"\n'
        for command, expected in [("pytest tests", ["pytest", "tests"]), ("mypy src", ["mypy", "src"]), ("ruff check .", ["ruff", "check", "."])]:
            content = WORKFLOW.replace("      - name: Test", setup + "      - name: Test").replace("python3 -m unittest discover -s tests -v", command)
            with patch.object(ci.shutil, "which", return_value="/usr/bin/selected-python"):
                self.assertEqual(ci.workflow_plan(content, job())["command"], ["/usr/bin/selected-python", "-m", *expected])

    def test_supported_check_and_multiline_single_command(self):
        self.assertEqual(ci.workflow_plan(WORKFLOW, job())["command"], [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"])
        block = WORKFLOW.replace("run: python3", "run: |\n          python3")
        self.assertEqual(ci.workflow_plan(block, job())["kind"], "test")
        for command in ("ruff check .", "python3 -m mypy src", "tsc --noEmit", "python3 -m pytest tests"):
            self.assertIn(ci.supported(command)[1], ("test", "lint"))

    def test_unavailable_or_unsafe_workflows_are_held(self):
        variants = [WORKFLOW.replace("ubuntu-latest", "windows-latest"),
                    WORKFLOW.replace("    steps:", "    services:\n      db: postgres\n    steps:"),
                    WORKFLOW.replace("    steps:", "    env:\n      TOKEN: ${{ secrets.TOKEN }}\n    steps:"),
                    WORKFLOW.replace("actions/checkout@v4", "custom/release@v1"),
                    WORKFLOW.replace("python3 -m unittest discover -s tests -v", "npm run deploy"),
                    WORKFLOW.replace("python3 -m unittest discover -s tests -v", "python3 -m unittest; gh release create x"),
                    WORKFLOW.replace("    steps:", "    strategy:\n      matrix: {}\n    steps:"),
                    WORKFLOW.replace("      - name: Test", "      - name: Test\n        working-directory: ../outside")]
        for content in variants:
            with self.subTest(content=content), self.assertRaises(ci.Skipped):
                ci.workflow_plan(content, job())
        for command in ("ruff check --fix .", "python3 deploy.py", "pytest /etc", "pytest ../outside", "npm test", "mypy --install-types src", "node --test", "node --test --eval danger", "tsc --noEmit false"):
            with self.subTest(command=command), self.assertRaises(ci.Skipped):
                ci.supported(command)


class LocalProofTests(unittest.TestCase):
    def test_multiline_plain_scalar_cannot_prove_an_unrelated_fix(self):
        workflow = self.source / '.github/workflows/verify.yml'
        workflow.write_text(WORKFLOW.replace('python3 -m unittest discover -s tests -v',
            'python3 -m unittest discover\n          -s ci_tests'))
        suite = self.source / 'ci_tests'
        suite.mkdir()
        (suite / 'test_ci.py').write_text('import unittest\nclass T(unittest.TestCase):\n def test_ci(self): self.fail("CI still fails")\n')
        (self.source / 'test_root.py').write_text('import unittest\nclass T(unittest.TestCase):\n def test_root(self): self.assertEqual(1, 2)\n')
        before = self.commit('CI suite and unrelated root failure')
        (self.source / 'test_root.py').write_text('import unittest\nclass T(unittest.TestCase):\n def test_root(self): self.assertEqual(1, 1)\n')
        after = self.commit('fix only unrelated root test')
        actual = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'ci_tests'],
                                cwd=self.source, capture_output=True)
        self.assertEqual(actual.returncode, 1)
        result = self.attempt(before, after)
        self.assertEqual(result['status'], 'SKIPPED', result)
        self.assertFalse(result['proven'])

    def test_quoted_directory_cannot_prove_an_unrelated_root_fix(self):
        self.check_directory_cannot_prove_an_unrelated_root_fix('"working-directory": src')

    def test_whitespace_directory_cannot_prove_an_unrelated_root_fix(self):
        self.check_directory_cannot_prove_an_unrelated_root_fix('"working-directory" : src')

    def check_directory_cannot_prove_an_unrelated_root_fix(self, setting):
        workflow = self.source / '.github/workflows/verify.yml'
        workflow.write_text(WORKFLOW.replace('      - name: Test',
            '      - name: Test\n        ' + setting))
        suite = self.source / 'src/tests'
        suite.mkdir(parents=True)
        (suite / 'test_ci.py').write_text('import unittest\nclass T(unittest.TestCase):\n def test_ci(self): self.fail("CI still fails")\n')
        (self.source / 'value').write_text('bad')
        before = self.commit('quoted CI directory with failing tests')
        (self.source / 'value').write_text('good')
        after = self.commit('fix only unrelated root test')
        actual = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v'],
                                cwd=self.source / 'src', capture_output=True)
        self.assertEqual(actual.returncode, 1)
        result = self.attempt(before, after)
        self.assertEqual(result['status'], 'SKIPPED', result)
        self.assertFalse(result['proven'])

    def setUp(self):
        def fixture_replay(*args, **kwargs):
            paths = kwargs.pop('receipts', None)
            if paths is not None:
                paths.append('/var/lib/vm02/jobs/fixture.json')
            with ci.environment(ci.clean_env(self.path)):
                return c.replay_tree(*args, **kwargs)
        executor = patch.object(ci, "vm_replay_tree", side_effect=fixture_replay)
        executor.start()
        self.addCleanup(executor.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.root = self.path / ".coldfix"
        self.root.mkdir(mode=0o700)
        self.output = self.root / "results"
        self.output.mkdir(mode=0o700)
        self.source = self.path / "source"
        self.source.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.source / ".github/workflows").mkdir(parents=True)
        (self.source / ".github/workflows/verify.yml").write_text(WORKFLOW)
        (self.source / "tests").mkdir()
        self.testfile = self.source / "tests/test_value.py"
        self.testfile.write_text('import unittest, os\nfrom pathlib import Path\nclass T(unittest.TestCase):\n def test_value(self):\n  self.assertNotIn("GH_TOKEN", os.environ)\n  self.assertNotIn("GITHUB_TOKEN", os.environ)\n  self.assertNotIn("PRIVATE_SECRET", os.environ)\n  self.assertEqual(Path("value").read_text(), "good")\n')
        (self.source / "value").write_text("bad")
        self.before = self.commit("before")
        (self.source / "value").write_text("good")
        self.after = self.commit("after")

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.source), *args], stderr=subprocess.PIPE, text=True).strip()

    def commit(self, message):
        self.git("add", ".")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def attempt(self, before=None, after=None, timeout=10, number=1):
        red = workflow_run(number, before or self.before, day=number)
        green = workflow_run(99, after or self.after, "success", 20)
        gh = FakeGitHub([red, green], {number: [job()], 99: [job(99, "success")]})
        failure = ci.collect(gh, 20)[0]
        return ci.attempt(gh, failure, self.root, self.output, timeout, clone_url=self.source.as_uri())

    def candidate(self, result):
        return json.loads((self.output / result["artifact"]).read_text())

    def test_configured_runner_replays_ci_without_coldbox(self):
        with patch.dict(os.environ, {'COLDFIX_REPLAY_RUNNER': 'sh -c {script}'}), \
                patch.object(ci, 'vm_replay_tree', side_effect=AssertionError('Coldbox used')):
            with patch.object(ci.sys, 'executable', os.path.realpath(sys.executable)):
                result = self.attempt()
        self.assertTrue(result['proven'], result)
        self.assertEqual(self.candidate(result)['replay']['reversed']['status'], 'FAIL')

    def test_green_fix_is_not_proven_when_reversal_still_passes(self):
        execute = ci.vm_replay_tree.side_effect
        def without_reversal(*args, **kwargs):
            kwargs.pop('reverse_patch', None)
            return execute(*args, **kwargs)
        with patch.object(ci, 'vm_replay_tree', side_effect=without_reversal):
            result = self.attempt()
        self.assertFalse(result['proven'])
        self.assertEqual(self.candidate(result)['replay']['reversed']['status'], 'PASS')

    def wheel(self, name, body):
        import zipfile
        path = self.source / f"{name}-1.0-py3-none-any.whl"
        meta = f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n"
        wheel = "Wheel-Version: 1.0\nGenerator: fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(f"{name}.py", body)
            archive.writestr(f"{name}-1.0.dist-info/METADATA", meta)
            archive.writestr(f"{name}-1.0.dist-info/WHEEL", wheel)
            archive.writestr(f"{name}-1.0.dist-info/RECORD", "")
        return path.name

    def test_pip_setup_installs_into_a_private_environment_for_both_replays(self):
        wheel = self.wheel("coldfixdep", "VALUE = 'good'\n")
        (self.source / "requirements.txt").write_text(wheel + "\n")
        (self.source / ".github/workflows/verify.yml").write_text(
            WORKFLOW.replace("      - name: Test", "      - run: pip install -r requirements.txt\n      - name: Test"))
        self.testfile.write_text('import unittest\nfrom pathlib import Path\nimport coldfixdep\nclass T(unittest.TestCase):\n def test_value(self):\n  self.assertEqual(coldfixdep.VALUE, Path("value").read_text())\n')
        (self.source / "value").write_text("bad")
        before = self.commit("dependency-backed test fails")
        (self.source / "value").write_text("good")
        after = self.commit("fix value")
        result = self.attempt(before, after, timeout=120)
        self.assertEqual((result["status"], result["red_reproduced"], result["proven"]), ("PROVEN", True, True), result)
        with self.assertRaises(subprocess.CalledProcessError):
            subprocess.check_output([sys.executable, "-c", "import coldfixdep"], stderr=subprocess.PIPE)

    def test_failed_pip_setup_is_incomplete_not_a_reproduced_failure(self):
        (self.source / "requirements.txt").write_text("coldfix-package-that-does-not-exist-0\n")
        (self.source / ".github/workflows/verify.yml").write_text(
            WORKFLOW.replace("      - name: Test", "      - run: pip install -r requirements.txt\n      - name: Test"))
        before = self.commit("unsatisfiable requirement")
        (self.source / "value").write_text("later")
        after = self.commit("later change")
        result = self.attempt(before, after, timeout=120)
        self.assertEqual((result["status"], result["red_reproduced"], result["proven"]), ("SKIPPED", False, False), result)

    def test_pull_request_default_merge_checkout_is_held_before_replay(self):
        red = dict(workflow_run(1, self.before), event="pull_request")
        green = dict(workflow_run(99, self.after, "success", 20), event="pull_request")
        failure = ci.Failure("workflow", red, job(), (green, job(99, "success")))
        with patch.object(c, "replay_tree", side_effect=AssertionError("PR source must not be guessed")):
            result = ci.attempt(FakeGitHub([], {}), failure, self.root, self.output, 10, clone_url=self.source.as_uri())
        self.assertEqual(result["status"], "SKIPPED")
        self.assertIn("PR merge source", result["reason"])
        self.assertFalse((self.root / "lessons").exists())
        self.assertEqual(list((self.root / "ci-work").iterdir()), [])

    def test_real_git_single_pair_emits_lesson_without_fake_historical_recurrence(self):
        with patch.dict(os.environ, dict(GH_TOKEN="sensitive-token", GITHUB_TOKEN="sensitive-token", PRIVATE_SECRET="sensitive-token")):
            result = self.attempt()
        self.assertEqual((result["status"], result["red_reproduced"], result["proven"]), ("PROVEN", True, True))
        self.assertEqual(result["lesson"]["verification"], "proven")
        candidate = self.candidate(result)
        self.assertEqual((candidate["replay"]["before"]["commit"], candidate["replay"]["after"]["commit"]), (self.before, self.after))
        self.assertEqual((candidate["replay"]["before"]["status"], candidate["replay"]["after"]["status"]), ("FAIL", "PASS"))
        self.assertIn("what_went_wrong", candidate)
        store = c.load_store(self.root)[1]
        self.assertEqual(len(store["failures"]), 2)
        self.assertEqual({row["kind"] for row in store["failures"].values()}, {"ci", "test"})
        self.assertEqual(sum(row["source"] == "ci-fix-learning" for row in store["failures"].values()), 1)
        self.assertEqual(store["lessons"][result["lesson"]["id"]]["status"], "active")
        self.assertEqual(list((self.root / "ci-work").iterdir()), [])
        repeat = self.attempt(number=2)
        self.assertEqual(repeat["lesson"]["status"], "existing")
        self.assertEqual(len(c.load_store(self.root)[1]["failures"]), 2)
        self.assertNotIn("sensitive-token", "".join(p.read_text() for p in self.root.rglob("*.json")))
        for path in self.root.rglob("*.json"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_unproven_final_replay_is_held_and_not_counted_as_proven(self):
        replay = c.replay
        def expire_final_replay(args, root, store):
            args.timeout = 0.000001
            return replay(args, root, store)
        with patch.object(c, "replay", side_effect=expire_final_replay):
            result = self.attempt()
        self.assertEqual((result["status"], result["proven"], result["lesson"]), ("SKIPPED", False, None))
        self.assertIn("final lesson replay", result["reason"])

    def test_promotion_failure_is_held_and_not_counted_as_proven(self):
        with patch.object(ci, "promote", side_effect=ci.Skipped("retained observation differs")):
            result = self.attempt()
        self.assertEqual((result["status"], result["red_reproduced"], result["proven"], result["lesson"]),
                         ("SKIPPED", True, False, None))
        self.assertEqual(result["reason"], "retained observation differs")
        self.assertEqual(list((self.root / "ci-work").iterdir()), [])

    def test_typescript_toolchain_outside_system_path(self):
        tools = self.path / "private-home" / "npm-bin"
        runtime = self.path / "private-home" / "node-bin"
        tools.mkdir(parents=True)
        runtime.mkdir()
        tsc = tools / "tsc"
        node = runtime / "node"
        node.write_text("#!/bin/sh\nexec " + sys.executable + ' "$@"\n')
        tsc.write_text('#!/usr/bin/env node\nfrom pathlib import Path\nimport os\n'
                       'if os.environ.get("CI") == "1": assert os.environ.get("PATH") == os.defpath\n'
                       'assert "COLDFIX_TSC" not in os.environ\n'
                       'assert "COLDFIX_NODE" not in os.environ\n'
                       'if "number =" in Path("test.ts").read_text():\n'
                       ' print("test.ts(1,7): error TS2322: Type string is not assignable to type number.")\n'
                       ' raise SystemExit(2)\n')
        for tool in (tsc, node):
            tool.chmod(0o700)
        workflow = self.source / ".github/workflows/verify.yml"
        workflow.write_text(WORKFLOW.replace("python3 -m unittest discover -s tests -v", "tsc --noEmit test.ts"))
        source = self.source / "test.ts"
        source.write_text('const value: number = "bad";\n')
        before = self.commit("TypeScript error")
        with patch.dict(os.environ, PATH=str(tools) + os.pathsep + str(runtime) + os.pathsep + os.defpath):
            plain = subprocess.run(["tsc", "--noEmit", "test.ts"], cwd=self.source,
                                   capture_output=True, text=True, env=dict(os.environ, CI=""))
            self.assertEqual(plain.returncode, 2)
            self.assertIn("TS2322", plain.stdout)
            source.write_text('const value: string = "good";\n')
            after = self.commit("fix TypeScript error")
            result = self.attempt(before, after)
            self.assertEqual((result["status"], result["red_reproduced"], result["proven"]),
                             ("PROVEN", True, True), result)
            candidate = self.candidate(result)
            self.assertEqual((candidate["replay"]["before"]["status"], candidate["replay"]["after"]["status"]),
                             ("FAIL", "PASS"))
            script = candidate["replay"]["check_script"]
            self.assertNotIn(str(self.path), script)
            lesson = c.load_store(self.root)[1]["lessons"][result["lesson"]["id"]]
            self.assertNotIn(str(self.path), lesson["check_script"])
            retained = self.path / "retained-check.py"
            retained.write_text(script)
            for content, expected in [('const value: number = "bad";\n', "FAIL"),
                                      ('const value: string = "good";\n', "PASS")]:
                source.write_text(content)
                replay = subprocess.run([sys.executable, "-I", "-S", str(retained)],
                                        cwd=self.source, capture_output=True, text=True)
                self.assertEqual(json.loads(replay.stdout)["status"], expected, replay.stderr)

    def test_quoted_job_and_workflow_defaults_cannot_prove_root_fix(self):
        for indent, anchor in (("", "jobs:"), ("    ", "    steps:")):
            for quote in ('"', "'"):
                with self.subTest(scope=indent, quote=quote):
                    content = WORKFLOW.replace(anchor,
                        f"{indent}{quote}defaults{quote}:\n{indent}  run:\n"
                        f"{indent}    working-directory: src\n" + anchor)
                    with self.assertRaises(ci.Skipped):
                        ci.workflow_plan(content, job())

    def test_home_python_retained_check_exports(self):
        runtime = self.path / "home/user/bin/python3"
        runtime.parent.mkdir(parents=True)
        runtime.symlink_to(sys.executable)
        with patch.object(ci.sys, "executable", str(runtime)):
            result = self.attempt()
        self.assertTrue(result["proven"], result)
        _, store = c.load_store(self.root)
        lesson = store["lessons"][result["lesson"]["id"]]
        destination = self.path / "portable.md"
        exported = c.export_lessons(argparse.Namespace(lesson=lesson["id"], format="coldlocker",
            output=str(destination), public=False, denylist=None, private_repo=[], git_repo=[],
            release_check=None, release_check_repo=None), self.root, store)
        self.assertEqual(exported, dict(exported=True, lesson_count=1))
        self.assertNotIn(str(runtime), lesson["check_script"])

    def test_retained_check_preserves_wrapper_virtualenv_runtime(self):
        import venv
        runtime = self.path / "selected-runtime"
        venv.EnvBuilder().create(runtime)
        python = runtime / "bin/python3"
        site = subprocess.check_output([str(python), "-c",
            "import sysconfig; print(sysconfig.get_path('purelib'))"], text=True).strip()
        Path(site, "selected_runtime_marker.py").write_text("VALUE = 42\n")
        self.testfile.write_text('import unittest, selected_runtime_marker\n'
            'class T(unittest.TestCase):\n'
            ' def test_runtime(self): self.assertEqual(selected_runtime_marker.VALUE, 42)\n')
        for name in ("python3", f"python{sys.version_info.major}.{sys.version_info.minor}"):
            with self.subTest(runtime=name):
                script = ci.check_script(dict(command=[str(python), "-m", "unittest", "discover", "-s", "tests"],
                                             kind="test", runtime_name=name))
                self.assertNotIn(str(python), script)
                check = self.path / "retained.py"
                check.write_text(script)
                result = subprocess.run([str(python), "-I", "-S", str(check)], cwd=self.source,
                                        text=True, capture_output=True, env=ci.clean_env(self.path))
                self.assertEqual((result.returncode, json.loads(result.stdout)),
                                 (0, {"status": "PASS"}), result.stderr)

        self.testfile.write_text('import unittest, selected_runtime_marker\nfrom pathlib import Path\n'
            'class T(unittest.TestCase):\n'
            ' def test_runtime(self):\n'
            '  self.assertEqual(selected_runtime_marker.VALUE, 42)\n'
            '  self.assertEqual(Path("value").read_text(), "good")\n')
        (self.source / "value").write_text("bad")
        before = self.commit("selected runtime failure")
        (self.source / "value").write_text("good")
        after = self.commit("selected runtime fix")
        with patch.object(ci.sys, "executable", str(python)):
            result = self.attempt(before, after)
        self.assertTrue(result["proven"], result)

    def test_unavailable_selected_runtime_is_held_without_fallback(self):
        with patch.object(ci.sys, "executable", "/removed-collector-venv/bin/python"):
            result = self.attempt()
        self.assertEqual(result["status"], "SKIPPED")
        self.assertFalse(result['proven'])
        plan = dict(command=["/removed/bin/python", "-m", "unittest"], kind="test", runtime_name="python3.12")
        self.assertNotIn("/removed/bin/python", ci.check_script(plan))

    def test_single_pair_promotes_existing_format_and_exports_without_another_ci_incident(self):
        with patch.object(ci.sys, 'executable', sys._base_executable):
            first = self.attempt()
        self.assertEqual(first["lesson"]["verification"], "proven")
        replay = self.candidate(first)['replay']
        self.assertEqual(replay['reversed']['status'], 'FAIL')
        self.assertEqual(replay['receipts']['reversed'], ['/var/lib/vm02/jobs/fixture.json'])
        _, store = c.load_store(self.root)
        lesson = store["lessons"][first["lesson"]["id"]]
        self.assertEqual((lesson["status"], lesson["scope"], len(lesson["evidence"])), ("active", "repo:example/project", 2))
        receipt = store["receipts"][lesson["receipt"]]
        self.assertEqual((receipt["before"]["status"], receipt["after"]["status"]), ("FAIL", "PASS"))
        self.assertNotEqual(store["failures"][lesson["evidence"][0]]["source_ref"], store["failures"][lesson["evidence"][1]]["source_ref"])
        destination = self.path / "lesson.md"
        exported = c.export_lessons(argparse.Namespace(lesson=lesson["id"], format="coldlocker", output=str(destination), public=False,
            denylist=None, private_repo=[], git_repo=[], release_check=None, release_check_repo=None), self.root, store)
        self.assertEqual(exported, dict(exported=True, lesson_count=1))
        body = destination.read_text()
        self.assertIn('"what_went_wrong"', body)
        self.assertIn('"fix"', body)
        self.assertIn("Verification: proven", body)
        self.assertEqual(list((self.root / "ci-work").iterdir()), [])
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_invalidated_lesson_is_revised_and_reproved_instead_of_reused(self):
        first = self.attempt()["lesson"]
        c.atomic_create(self.root / "invalidations.json", c.encoded({first["lesson_id"]: first["id"]}))
        second = self.attempt()["lesson"]
        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual(second["verification"], "proven")
        self.assertIn("receipt_id", second)

    def test_changed_generated_check_revises_and_reproves_existing_lesson(self):
        first = self.attempt()["lesson"]
        original = (self.root / "lessons" / (first["id"] + ".json")).read_bytes()
        with patch.object(ci, "TEST_BRIDGE", ci.TEST_BRIDGE + "\n"):
            second = self.attempt()["lesson"]
        self.assertEqual(second["verification"], "proven")
        self.assertEqual(second["lesson_id"], first["lesson_id"])
        self.assertNotEqual(second["id"], first["id"])
        store = c.load_store(self.root)[1]
        self.assertNotEqual(store["lessons"][second["id"]]["check_script"], store["lessons"][first["id"]]["check_script"])
        self.assertEqual(store["receipts"][second["receipt_id"]]["lesson_revision"], second["id"])
        self.assertEqual(len(store["failures"]), 3)
        for revision in (first, second):
            receipt = store["receipts"][revision["receipt_id"]]
            identity = next(row["id"] for row in store["failures"].values() if row["kind"] == "ci")
            expected_ref = "coldfix:local-replay:" + c.digest(c.encoded([identity, self.before, receipt["check_sha256"]]))
            self.assertIn(expected_ref, [store["failures"][key]["source_ref"] for key in store["lessons"][revision["id"]]["evidence"]])
        self.assertEqual((self.root / "lessons" / (first["id"] + ".json")).read_bytes(), original)

    def test_timeout_infrastructure_and_green_before_do_not_prove(self):
        self.testfile.write_text("import time\ntime.sleep(60)\n")
        sleeping = self.commit("sleep")
        result = self.attempt(sleeping, self.after, timeout=0.15)
        self.assertEqual((result["status"], result["red_reproduced"], result["proven"]), ("SKIPPED", False, False))
        self.assertIn("timed out", result["reason"])
        self.assertEqual(list((self.root / "ci-work").iterdir()), [])
        self.testfile.write_text("import unavailable_dependency\n")
        unavailable = self.commit("missing dependency")
        result = self.attempt(unavailable, self.after)
        self.assertEqual(result["status"], "SKIPPED")
        self.assertIn("infrastructure unavailable", result["reason"])
        result = self.attempt(self.after, self.before)
        self.assertEqual((result["red_reproduced"], result["proven"]), (False, False))
        self.assertFalse((self.root / "lessons").exists())
        self.assertFalse((self.root / "failures").exists())

    def test_nonproof_fail_after_empty_suite_and_unrecognized_error(self):
        (self.source / "value").write_text("bad")
        (self.source / "other").write_text("changed tree")
        still_bad = self.commit("still bad")
        result = self.attempt(self.before, still_bad)
        self.assertEqual((result["red_reproduced"], result["proven"], result["status"]), (True, False, "SKIPPED"))
        self.testfile.write_text("import unittest\n")
        empty = self.commit("no tests")
        result = self.attempt(self.before, empty)
        self.assertEqual((result["red_reproduced"], result["proven"], result["reason"]), (True, False, "no passing tests executed"))
        self.testfile.write_text("raise RuntimeError('unknown failure')\n")
        unknown = self.commit("unknown failure")
        result = self.attempt(unknown, self.after)
        self.assertFalse(result["proven"])
        self.assertFalse((self.root / "failures").exists())
        self.assertEqual(list((self.root / "ci-work").iterdir()), [])

    def test_retained_check_also_filters_environment_when_run_independently(self):
        result = self.attempt()
        script = self.path / "retained.py"
        script.write_text(self.candidate(result)["replay"]["check_script"])
        self.git("checkout", "-q", self.after)
        with patch.dict(os.environ, dict(GH_TOKEN="secret", GITHUB_TOKEN="secret", PRIVATE_SECRET="secret")):
            check = subprocess.run([sys.executable, str(script)], cwd=self.source, capture_output=True, text=True)
        self.assertEqual((check.returncode, json.loads(check.stdout)), (0, dict(status="PASS")))
        self.assertEqual(self.git("status", "--porcelain"), "")
        (self.source / "value").write_text("different failure")
        check = subprocess.run([sys.executable, str(script)], cwd=self.source, capture_output=True, text=True)
        self.assertEqual((check.returncode, json.loads(check.stdout)), (2, dict(status="INCOMPLETE")))
        self.assertIn("diagnostic differs", check.stderr)

    def test_skipped_parent_cannot_supply_successful_subtest_proof(self):
        self.testfile.write_text('import unittest\nfrom pathlib import Path\nclass T(unittest.TestCase):\n def test_value(self):\n  with self.subTest(case="value"):\n   self.assertEqual(Path("value").read_text(), "good")\n  self.skipTest("parent incomplete")\n')
        (self.source / "value").write_text("bad")
        before = self.commit("failing subtest")
        (self.source / "value").write_text("good")
        after = self.commit("passing subtest but skipped parent")
        result = self.attempt(before, after)
        self.assertEqual((result["red_reproduced"], result["proven"], result["status"]), (True, False, "SKIPPED"))
        self.assertFalse((self.root / "lessons").exists())

    def test_all_skipped_suite_does_not_supply_green_proof(self):
        self.testfile.write_text('import unittest\n@unittest.skip("runtime unavailable")\nclass T(unittest.TestCase):\n def test_value(self): pass\n')
        skipped = self.commit("all tests skipped")
        result = self.attempt(self.before, skipped)
        self.assertEqual((result["red_reproduced"], result["proven"], result["reason"]),
                         (True, False, "no passing tests executed"))
        self.assertFalse((self.root / "lessons").exists())

    def test_pytest_display_text_cannot_override_framework_outcomes(self):
        module = self.source / "pytest.py"
        script = self.path / "quiet-check.py"
        argv, kind = ci.supported("python3 -m pytest -qq")
        script.write_text(ci.check_script(dict(command=argv, kind=kind)))
        cases = [("ss [100%]", [], "INCOMPLETE"), (".. [100%]", ["passed", "passed"], "PASS"),
                 ("2 passed in 0.01s", [], "INCOMPLETE"), ("1 passed, 1 xfailed", ["passed", "xfail"], "INCOMPLETE"),
                 ("2 passed", ["passed", "rerun"], "INCOMPLETE")]
        for output, outcomes, expected in cases:
            module.write_text("from types import SimpleNamespace\ndef main(args, plugins):\n print(" + repr(output) + ")\n"
                + " for index, outcome in enumerate(" + repr(outcomes) + "):\n"
                + "  report=SimpleNamespace(nodeid=str(index), when='call', outcome='skipped' if outcome=='xfail' else outcome, passed=outcome=='passed', failed=False)\n"
                + "  if outcome=='xfail': report.wasxfail='known issue'\n"
                + "  plugins[0].pytest_runtest_logreport(report)\n return 0\n")
            result = subprocess.run([sys.executable, str(script)], cwd=self.source, capture_output=True, text=True)
            self.assertEqual(json.loads(result.stdout), dict(status=expected))
            self.assertEqual(result.returncode, 0 if expected == "PASS" else 2)

    def test_expected_failure_is_not_a_proven_fix(self):
        (self.source / "value").write_text("bad")
        self.testfile.write_text(self.testfile.read_text().replace(" def test_value", " @unittest.expectedFailure\n def test_value"))
        expected = self.commit("suppress failure with expectedFailure")
        result = self.attempt(self.before, expected)
        self.assertEqual((result["red_reproduced"], result["proven"], result["reason"]),
                         (True, False, "expected failures are not fix proof"))
        self.assertFalse((self.root / "lessons").exists())

    def test_failed_test_must_pass_instead_of_being_skipped_or_deleted(self):
        body = 'import unittest\nfrom pathlib import Path\nclass T(unittest.TestCase):\n def test_bug(self): self.assertEqual(Path("value").read_text(), "good")\n def test_healthy(self): self.assertTrue(True)\n'
        self.testfile.write_text(body)
        (self.source / "value").write_text("bad")
        before = self.commit("one real failing test and one healthy test")
        variants = [body.replace(" def test_bug", ' @unittest.skip("hidden bug")\n def test_bug'),
                    'import unittest\nclass T(unittest.TestCase):\n def test_healthy(self): self.assertTrue(True)\n']
        for content in variants:
            self.testfile.write_text(content)
            after = self.commit("suppress the failing test")
            result = self.attempt(before, after)
            self.assertEqual((result["red_reproduced"], result["proven"], result["reason"]),
                             (True, False, "failed test did not pass after fix"))
        self.assertFalse((self.root / "lessons").exists())
        self.testfile.write_text(body)
        (self.source / "value").write_text("good")
        fixed = self.commit("fix the value while retaining the failed test")
        self.assertEqual(self.attempt(before, fixed)["lesson"]["verification"], "proven")

    def test_pytest_teardown_skip_removes_previously_passed_identity(self):
        from types import SimpleNamespace
        def pytest_main(flags, plugins):
            for node, when, outcome in [("target", "call", "passed"), ("target", "teardown", "skipped"), ("healthy", "call", "passed")]:
                plugins[0].pytest_runtest_logreport(SimpleNamespace(nodeid=node, when=when, outcome=outcome,
                    passed=outcome == "passed", failed=outcome == "failed", skipped=outcome == "skipped"))
            return 0
        report = self.path / "framework-results.json"
        with patch.dict(sys.modules, pytest=SimpleNamespace(main=pytest_main)), patch.object(sys, "argv", ["bridge", str(report), "pytest"]):
            with self.assertRaises(SystemExit) as stopped:
                exec(ci.TEST_BRIDGE, {})
        self.assertEqual(stopped.exception.code, 0)
        self.assertEqual(json.loads(report.read_text()), dict(passed=["healthy"], failed=[], errors=[], expected=[]))

    def test_real_installed_pytest_reports_drive_a_proven_pair(self):
        available = subprocess.run([sys.executable, "-c", "import pytest"], env=ci.clean_env(self.path), capture_output=True, timeout=10)
        if available.returncode:
            self.skipTest("pytest unavailable in selected clean runtime")
        workflow = self.source / ".github/workflows/verify.yml"
        workflow.write_text(WORKFLOW.replace("python3 -m unittest discover -s tests -v", "python3 -m pytest -q tests"))
        self.testfile.write_text('from pathlib import Path\ndef test_value(): assert Path("value").read_text() == "good"\n')
        (self.source / "value").write_text("bad")
        before = self.commit("pytest failing assertion")
        (self.source / "value").write_text("good")
        after = self.commit("pytest fixed assertion")
        result = self.attempt(before, after)
        self.assertEqual(result['status'], 'PROVEN', result)
        self.assertEqual(result["lesson"]["verification"], "proven")

    def test_replay_git_initialization_is_also_bounded_by_timeout(self):
        tools = self.path / "slow-git"
        tools.mkdir()
        executable = tools / "git"
        executable.write_text('#!/bin/sh\ncase "$*" in\n*rev-parse*) echo ' + self.before
            + ';;\ninit*) for last do :; done; mkdir -p "$last"; sleep 0.4;;\nesac\n')
        executable.chmod(0o700)
        with patch.dict(os.environ, PATH=str(tools) + os.pathsep + os.environ["PATH"]):
            row = c.replay_tree(self.source, self.before, self.path / "slow-checkout",
                "printf '%s' '{\"status\":\"PASS\"}'", ci.EXPECTED, 0.05)
        self.assertEqual(row["status"], "INCOMPLETE")
        self.assertIn("timed out", row["stderr"])

    def test_cli_end_to_end_with_fake_GET_API_and_real_local_git(self):
        tools = self.path / "tools"
        tools.mkdir()
        routes = {
            "actions/runs?per_page=100&page=1&status=completed": dict(total_count=2, workflow_runs=[workflow_run(2, self.after, "success", 2), workflow_run(1, self.before)]),
            "actions/runs/1/attempts/1/jobs?per_page=100&page=1": dict(total_count=1, jobs=[job()]),
            "actions/runs/2/attempts/1/jobs?per_page=100&page=1": dict(total_count=1, jobs=[job(20, "success")]),
            f"compare/{self.before}...{self.after}": dict(status="ahead", merge_base_commit=dict(sha=self.before), base_commit=dict(sha=self.before)),
            "commits?per_page=20&page=1": [],
        }
        gh = tools / "gh"
        gh.write_text("#!" + sys.executable + "\nimport json,sys\n" + "routes=" + repr(routes) + "\n"
            + "assert sys.argv[1:3]==['api','--hostname'] and sys.argv[sys.argv.index('--method')+1]=='GET'\n"
            + "print(json.dumps(routes[sys.argv[-1].split('repos/example/project/')[1]]))\n")
        git = tools / "git"
        git.write_text("#!" + sys.executable + "\nimport os,sys\nargs=sys.argv[1:]\n"
            + "args=[" + repr(self.source.as_uri()) + " if x=='https://github.com/example/project.git' else x for x in args]\n"
            + "os.execv('/usr/bin/git',['git',*args])\n")
        for path in (gh, git):
            path.chmod(0o700)
        env = dict(os.environ, PATH=str(tools) + os.pathsep + os.environ["PATH"], GH_TOKEN="fake-secret")
        launcher = self.path / "fixture_cli.py"
        launcher.write_text("import sys\nfrom pathlib import Path\nsys.path.insert(0, " + repr(str(ROOT)) + ")\n"
            + "import ci_fix_learning as ci\nimport coldfix as c\n"
            + "def fixture_replay(*args, **kwargs):\n"
            + "    paths = kwargs.pop('receipts', None)\n"
            + "    if paths is not None: paths.append('/var/lib/vm02/jobs/fixture.json')\n"
            + "    with ci.environment(ci.clean_env(Path(args[2]).parent)):\n"
            + "        return c.replay_tree(*args, **kwargs)\n"
            + "ci.vm_replay_tree = fixture_replay\nsys.exit(ci.main())\n")
        result = subprocess.run([sys.executable, str(launcher), "--repo", "example/project", "--repo", "example/project",
            "--store", str(self.root), "--timeout", "10"], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual((summary["failures_found"], summary["red_reproduced"], summary["proven"], summary["lessons_promoted"]), (1, 1, 1, 1))
        report = json.loads(Path(summary["report"]).read_text())
        self.assertEqual(report["api_requests"], 5)
        self.assertTrue(report["enumeration_complete"])
        self.assertEqual(report["repositories_selected"], ["example/project"])
        self.assertEqual(report["results"][0]["lesson"]["verification"], "proven")
        self.assertIn("Failures found 1; red 1; proven 1; promoted 1; skipped 0", Path(summary["markdown"]).read_text())
        self.assertEqual(list((self.root / "ci-work").iterdir()), [])

    def test_raw_sensitive_output_is_never_persisted(self):
        self.testfile.write_text(self.testfile.read_text().replace('  self.assertEqual', '  print("token=secret-sensitive-output")\n  self.assertEqual'))
        (self.source / "value").write_text("bad")
        before = self.commit("prints raw diagnostic")
        (self.source / "value").write_text("good")
        after = self.commit("fix with raw output")
        result = self.attempt(before, after)
        self.assertTrue(result["proven"])
        self.assertNotIn("secret-sensitive-output", "".join(p.read_text() for p in self.root.rglob("*.json")))

    def test_clone_failure_preserves_foreign_work_and_removes_owned_temporary(self):
        work = self.root / "ci-work"
        work.mkdir(mode=0o700)
        foreign = work / "foreign"
        foreign.write_text("preserve")
        with patch.object(ci, "run", side_effect=ci.Skipped("read operation unavailable")):
            result = self.attempt()
        self.assertEqual(result["reason"], "read operation unavailable")
        self.assertEqual(list(work.iterdir()), [foreign])
        self.assertEqual(foreign.read_text(), "preserve")


class CliTests(unittest.TestCase):
    def test_cli_rejects_invalid_boundaries_before_network_or_store(self):
        with tempfile.TemporaryDirectory() as name:
            for args in (["--repo", "example/project", "--timeout", "0"], ["--repo", "../other"],
                         ["--repo", "example/project", "--timeout", "nan"], ["--owner", "-invalid"],
                         ["--repo", "example/project", "--max-failures", "0"]):
                result = subprocess.run([sys.executable, str(ROOT / "ci_fix_learning.py"), *args], cwd=name, capture_output=True, text=True)
                self.assertEqual(result.returncode, 2)
                self.assertFalse((Path(name) / ".coldfix").exists())

    def test_private_reports_counts_cost_and_completeness(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / ".coldfix"
            gh = FakeGitHub([workflow_run(1, "a" * 40)], {1: []})
            with patch.object(ci, "GitHub", return_value=gh):
                result = ci.execute(argparse.Namespace(store=str(root), repo=["example/project"], owner=None, max_failures=1, timeout=1))
            report = json.loads(Path(result["report"]).read_text())
            self.assertEqual(report["counts"], dict(failures_found=1, red_reproduced=0, proven=0, skipped=1, lessons_promoted=0))
            self.assertFalse(report["enumeration_complete"])
            self.assertEqual(report["tool_model_calls"], 0)
            self.assertIsNone(report["infrastructure_cost_usd"])
            self.assertIsNone(report["approximate_cost_per_lesson_usd"])
            self.assertIn("RUNNER_STARTUP_UNAVAILABLE: 1", Path(result["markdown"]).read_text())
            self.assertEqual(Path(result["report"]).stat().st_mode & 0o777, 0o600)

    def test_approval_held_run_is_reported_as_its_own_bucket(self):
        with tempfile.TemporaryDirectory() as name:
            gh = FakeGitHub([workflow_run(1, "a" * 40, "action_required")], {1: []})
            with patch.object(ci, "GitHub", return_value=gh):
                result = ci.execute(argparse.Namespace(store=str(Path(name) / ".coldfix"), repo=["example/project"], owner=None, max_failures=1, timeout=1))
            report = json.loads(Path(result["report"]).read_text())
            self.assertEqual(report["counts"], dict(failures_found=0, red_reproduced=0, proven=0, skipped=0, lessons_promoted=0))
            self.assertEqual(report["approval_held"], 1)
            self.assertEqual(report["skipped_by_reason"], {})
            self.assertIn("Approval held (never executed): 1", Path(result["markdown"]).read_text())

    def test_per_repo_cap_does_not_omit_later_repositories(self):
        with tempfile.TemporaryDirectory() as name:
            gh = FakeGitHub([workflow_run(1, "a" * 40)], {1: []})
            with patch.object(ci, "GitHub", return_value=gh):
                result = ci.execute(argparse.Namespace(store=str(Path(name) / ".coldfix"),
                    repo=["example/project", "example/another"], owner=None, max_failures=1, timeout=1))
            report = json.loads(Path(result["report"]).read_text())
            self.assertEqual([r["repo"] for r in report["repositories"]], ["example/project", "example/another"])
            self.assertEqual(report["counts"]["failures_found"], 2)
            self.assertEqual(report["skipped_by_reason"], {"RUNNER_STARTUP_UNAVAILABLE": 2})


if __name__ == "__main__":
    unittest.main()
