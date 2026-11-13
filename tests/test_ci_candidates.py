import base64
import json
import sys
import unittest
from unittest.mock import patch

import ci_candidates as cand
import ci_fix_learning as ci

FIT = '''name: ci
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Unit tests
        run: python3 -m unittest discover -s tests
'''
MATRIX = FIT.replace("    runs-on: ubuntu-latest\n", "    runs-on: ubuntu-latest\n    strategy:\n      matrix:\n        v: [1]\n")
WINDOWS = FIT.replace("ubuntu-latest", "windows-latest")
INSTALL = FIT.replace("      - name: Unit tests", "      - run: pip install -r requirements.txt\n      - name: Unit tests")
SECRETS = FIT.replace("python3 -m unittest discover -s tests", "python3 -m unittest\n        env:\n          TOKEN: ${{ secrets.T }}")
NODE = FIT.replace("python3 -m unittest discover -s tests", "npm test")
UNNAMED_BLOCK = FIT.replace("      - name: Unit tests\n        run: python3 -m unittest discover -s tests\n",
                            "      - run: |\n          python3 -m unittest discover -s tests\n")
UNNAMED_STRIP = UNNAMED_BLOCK.replace("run: |", "run: |-")
UNNAMED_FOLDED = UNNAMED_BLOCK.replace("run: |", "run: >")
UNNAMED_MULTILINE = UNNAMED_BLOCK.replace("tests\n", "tests\n          echo done\n")
QUOTED_JOB = FIT.replace("  test:", '  "test":')
SINGLE_QUOTED_JOB = FIT.replace("  test:", "  'test':")
QUOTED_NAME = FIT.replace("  test:\n", "  test:\n    'name': Quoted job\n").replace("      - name:", '      - "name":')
QUOTED_RUN = FIT.replace("      - name: Unit tests\n        run:", "      - 'run':")
QUOTED_BLOCK_RUN = UNNAMED_BLOCK.replace("run: |", '"run": |')
QUOTED_COMPLEX_KEY = FIT.replace("  test:", '  "te\\u0073t":')


def encoded(text):
    return dict(encoding="base64", content=base64.b64encode(text.encode()).decode())


class FitTests(unittest.TestCase):
    def test_plain_ubuntu_unittest_job_fits(self):
        self.assertEqual(cand.fitting_job(FIT), "test")

    def test_unnamed_block_scalar_run_step_fits(self):
        for name, text in dict(literal=UNNAMED_BLOCK, strip=UNNAMED_STRIP).items():
            with self.subTest(name):
                self.assertEqual(cand.fitting_job(text), "test")

    def test_block_scalars_workflow_plan_rejects_do_not_fit(self):
        for name, text in dict(folded=UNNAMED_FOLDED, multiline=UNNAMED_MULTILINE).items():
            with self.subTest(name):
                self.assertIsNone(cand.fitting_job(text))

    def test_quoted_mapping_keys_fit(self):
        cases = dict(job_id=QUOTED_JOB, single_quoted_job_id=SINGLE_QUOTED_JOB, name=QUOTED_NAME, run=QUOTED_RUN,
                     block_run=QUOTED_BLOCK_RUN)
        for name, text in cases.items():
            with self.subTest(name):
                self.assertEqual(cand.fitting_job(text), "Quoted job" if name == "name" else "test")

    def test_escaped_mapping_keys_do_not_fit(self):
        self.assertIsNone(cand.fitting_job(QUOTED_COMPLEX_KEY))

    def test_unreplayable_workflows_do_not_fit(self):
        for name, text in dict(windows=WINDOWS, secrets=SECRETS, node=NODE).items():
            with self.subTest(name):
                self.assertIsNone(cand.fitting_job(text))

    def test_simple_matrix_and_pip_install_workflows_fit(self):
        for name, text in dict(matrix=MATRIX, install=INSTALL).items():
            with self.subTest(name):
                self.assertEqual(cand.fitting_job(text), "test")

    def test_expanded_matrix_job_and_step_identities_fit(self):
        matrix = MATRIX.replace("v: [1]", 'python: ["3.10", "3.11"]')
        named = matrix.replace("  test:\n", "  test:\n    name: Python ${{ matrix.python }}\n")
        cases = dict(
            job=named,
            job_and_step=named.replace("name: Unit tests", "name: Tests ${{ matrix.python }}"),
            step=matrix.replace("name: Unit tests", "name: Tests ${{ matrix.python }}"),
            unnamed_run=named.replace("      - name: Unit tests\n        run: python3 -m unittest discover -s tests",
                                      "      - run: python3 -m unittest tests.test_${{ matrix.python }}"),
        )
        for case, text in cases.items():
            with self.subTest(case):
                expected = "test" if case == "step" else "Python 3.10"
                step = ("Tests 3.10" if case in ("step", "job_and_step") else
                        "Run python3 -m unittest tests.test_3.10" if case == "unnamed_run" else "Unit tests")
                plan = ci.workflow_plan(text, dict(name=expected, steps=[dict(name=step, conclusion="failure")]))
                self.assertEqual(plan["kind"], "test")
                self.assertEqual(cand.fitting_job(text), expected)

    def test_matrix_can_discover_a_supported_runner_after_an_unsupported_entry(self):
        text = MATRIX.replace("ubuntu-latest", "${{ matrix.os }}").replace(
            "v: [1]", 'os: [windows-latest, ubuntu-latest]\n        python: ["3.10", "3.11"]')
        self.assertEqual(cand.fitting_job(text), "test (ubuntu-latest, 3.10)")


class FakeApi:
    def __init__(self, files, failures=1, successes=1):
        self.files, self.counts = files, dict(failure=failures, success=successes)

    def __call__(self, github, endpoint):
        if endpoint == "contents/.github/workflows":
            return [dict(type="file", name=name) for name in self.files]
        if endpoint.startswith("contents/.github/workflows/"):
            return encoded(self.files[endpoint.rsplit("/", 1)[1]])
        status = endpoint.split("status=")[1].split("&")[0]
        return dict(total_count=self.counts[status])


class InspectTests(unittest.TestCase):
    def inspect(self, files, **counts):
        with patch.object(ci.GitHub, "api", lambda github, endpoint: FakeApi(files, **counts)(github, endpoint)):
            return cand.inspect("example/project")

    def test_fitting_workflow_with_red_and_green_push_runs(self):
        row, reason, requests = self.inspect({"ci.yml": FIT})
        self.assertEqual((row["repo"], row["workflow"], row["job"], reason), ("example/project", "ci.yml", "test", "fit"))
        self.assertEqual(requests, 0)

    def test_other_workflow_files_are_ignored_and_fit_is_found(self):
        self.assertEqual(self.inspect({"lint.yml": WINDOWS, "ci.yml": FIT})[0]["workflow"], "ci.yml")

    def test_requires_both_a_failed_and_a_successful_push_run(self):
        for counts in (dict(failures=0), dict(successes=0)):
            self.assertEqual(self.inspect({"ci.yml": FIT}, **counts)[:2], (None, "no_red_and_green_push_runs"))
        self.assertEqual(self.inspect({"ci.yml": WINDOWS})[:2], (None, "no_fitting_workflow"))

    def test_unreadable_workflows_do_not_fit(self):
        def unavailable(github, endpoint):
            raise ci.Skipped("read operation unavailable")
        with patch.object(ci.GitHub, "api", unavailable):
            self.assertEqual(cand.inspect("example/project"), (None, "workflows_unreadable", 0))


def code_page(*names):
    return json.dumps(dict(items=[dict(repository=dict(full_name=n, private=n == "o/r1")) for n in names])).encode()


class SearchTests(unittest.TestCase):
    def test_stops_after_wanted_fits_and_skips_private_rows(self):
        with patch.object(ci, "run", return_value=code_page("o/r0", "o/r1", "o/r2", "o/r3")), \
                patch.object(cand, "inspect", lambda repo: (dict(repo=repo), "fit", 3)):
            result = cand.candidates(["q"], 2, 50)
        self.assertEqual([r["repo"] for r in result["candidates"]], ["o/r0", "o/r2"])
        self.assertEqual((result["scanned"], result["outcomes"], result["api_requests"]), (2, dict(fit=2), 7))

    def test_names_the_tool_rejects_are_skipped_not_fatal(self):
        with patch.object(ci, "run", return_value=code_page("_bad/repo", "Good/Repo")):
            self.assertEqual(cand.search("q", 1), ["good/repo"])

    def test_scan_bound_stops_search(self):
        pages = iter([code_page(*[f"o/a{n}" for n in range(5)]), code_page(*[f"o/b{n}" for n in range(5)])])
        with patch.object(ci, "run", lambda *a, **k: next(pages)), patch.object(cand, "inspect", lambda repo: (None, "no_fitting_workflow", 2)):
            result = cand.candidates(["q"], 2, 7)
        self.assertEqual((result["scanned"], result["candidates"], result["outcomes"]), (7, [], dict(no_fitting_workflow=7)))
        self.assertEqual(result["api_requests"], 2 + 7 * 2)

    def test_rate_limited_search_keeps_earlier_finds(self):
        calls = iter([code_page("o/a0")])

        def run(*args, **kwargs):
            try:
                return next(calls)
            except StopIteration:
                raise ci.Skipped("read operation unavailable") from None
        with patch.object(ci, "run", run), patch.object(cand, "inspect", lambda repo: (dict(repo=repo), "fit", 3)):
            result = cand.candidates(["q"], 5, 50)
        self.assertEqual([r["repo"] for r in result["candidates"]], ["o/a0"])


if __name__ == "__main__":
    unittest.main()
