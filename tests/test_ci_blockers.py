import unittest

import ci_blockers as blockers
from test_ci_candidates import FIT, INSTALL, MATRIX, NODE, SECRETS, WINDOWS

SETUP = FIT.replace("      - name: Unit tests",
                    "      - uses: actions/setup-node@v4\n        with:\n          node-version: 20\n      - name: Unit tests")
MULTILINE = FIT.replace("      - name: Unit tests\n        run: python3 -m unittest discover -s tests\n",
                        "      - run: |\n          python3 -m unittest discover -s tests\n          echo done\n")
TWO_JOBS = FIT + "  lint:\n    runs-on: macos-latest\n    steps:\n      - run: ruff check .\n"


class ClassifyTests(unittest.TestCase):
    def tokens(self, text):
        return blockers.classify(text)["test"]

    def test_plain_job_has_no_blockers(self):
        self.assertEqual(self.tokens(FIT), [])

    def test_each_kind_is_named(self):
        cases = dict(matrix=(MATRIX, "matrix"), windows=(WINDOWS, "runner:windows"), install=(INSTALL, "run:pip"),
                     secrets=(SECRETS, "secrets"), setup=(SETUP, "action:actions/setup-node"),
                     multiline=(MULTILINE, "run:echo"))
        for name, (text, token) in cases.items():
            with self.subTest(name):
                self.assertIn(token, self.tokens(text))

    def test_job_without_a_check_command_is_marked(self):
        self.assertIn("no-check-step", self.tokens(NODE))

    def test_jobs_are_reported_separately(self):
        jobs = blockers.classify(TWO_JOBS)
        self.assertEqual(jobs["test"], [])
        self.assertIn("runner:macos", jobs["lint"])

    def test_workflow_without_push_trigger_is_marked(self):
        self.assertIn("trigger:no-push", self.tokens(FIT.replace("[push]", "[pull_request]")))


class RankTests(unittest.TestCase):
    def report(self):
        return blockers.rank({
            "a/plain": {"w.yml": FIT},
            "b/matrix": {"w.yml": MATRIX},
            "c/matrix": {"w.yml": MATRIX},
            "d/both": {"w.yml": MATRIX.replace("ubuntu-latest", "windows-latest")},
            "e/none": {"w.yml": "name: x\n"},
        })

    def test_counts_repositories_not_jobs(self):
        report = self.report()
        self.assertEqual((report["repositories"], report["repositories_without_a_workflow_job"], report["usable_today"]), (5, 1, 1))

    def test_ranking_orders_by_repositories_blocked(self):
        ranking = {row["blocker"]: row for row in self.report()["ranking"]}
        self.assertEqual(ranking["matrix"]["repos_blocked"], 3)
        self.assertEqual(ranking["matrix"]["repos_unlocked_alone"], 2)
        self.assertEqual(ranking["runner:windows"]["repos_unlocked_alone"], 0)
        self.assertEqual(self.report()["ranking"][0]["blocker"], "matrix")

    def test_cumulative_curve_counts_newly_usable_repositories(self):
        curve = self.report()["cumulative_by_blocked_count"]
        self.assertEqual([row["repos_usable"] for row in curve], [3, 4])

    def test_markdown_lists_every_blocker(self):
        text = blockers.markdown(self.report(), "fixtures")
        self.assertIn("| 1 | `matrix` | 3 | 2 |", text)
        self.assertIn("runner:windows", text)


if __name__ == "__main__":
    unittest.main()
