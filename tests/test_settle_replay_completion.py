"""Replay admission requires a completed, nonempty successful unittest run."""

from pathlib import Path
import os
import subprocess
import tempfile
import unittest

from settle_codex_lessons import replay, run_tests


class ReplayCompletionTests(unittest.TestCase):
    def run_fixture(self, source):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        tree = Path(temporary.name)
        (tree / "tests").mkdir()
        (tree / "tests" / "test_replay.py").write_text(source)
        return run_tests(tree, ["tests.test_replay"])

    def test_runner_reports_a_passing_test_inside_an_overall_failed_suite(self):
        result, reason = self.run_fixture(
            "import unittest\n"
            "class Replay(unittest.TestCase):\n"
            " def test_pass_marker(self): self.assertTrue(True)\n"
            " def test_failure(self): self.fail('overall run failed')\n"
        )

        self.assertEqual(reason, "")
        self.assertFalse(result["successful"])
        self.assertEqual(result["tests_run"], 2)
        self.assertTrue(any(state == "ok" for state, _ in result["results"].values()))

    def test_zero_executed_tests_cannot_pass_from_an_empty_result_marker(self):
        result, reason = self.run_fixture("import unittest\nclass Replay(unittest.TestCase): pass\n")

        self.assertIsNone(result)
        self.assertEqual(reason, "replay-no-tests")

    def test_complete_passing_suite_remains_admissible(self):
        result, reason = self.run_fixture(
            "import unittest\n"
            "class Replay(unittest.TestCase):\n"
            " def test_first(self): self.assertEqual(2, 2)\n"
            " def test_second(self): self.assertTrue(True)\n"
        )

        self.assertEqual(reason, "")
        self.assertIsNotNone(result)
        self.assertEqual(result["tests_run"], 2)
        self.assertTrue(result["successful"])

    def test_passing_target_does_not_prove_a_failed_fixed_suite(self):
        proof, reason = self.replay_fixture(
            "def get(): return 1\n",
            "def get(): return 2\n",
            " def test_target(self): self.assertEqual(value.get(), 2)\n"
            " def test_unrelated(self): self.assertTrue(True)\n",
            " def test_target(self): self.assertEqual(value.get(), 2)\n"
            " def test_unrelated(self): self.fail('fixed suite still fails')\n",
        )

        self.assertIsNone(proof)
        self.assertEqual(reason, "replay-failed")

    def test_empty_fixed_suite_cannot_supply_replay_proof(self):
        proof, reason = self.replay_fixture(
            "def get(): return 1\n",
            "def get(): return 2\n",
            " def test_target(self): self.assertEqual(value.get(), 2)\n",
            " pass\n",
        )

        self.assertIsNone(proof)
        self.assertEqual(reason, "replay-no-tests")

    def replay_fixture(self, bad_value, fixed_value, bad_tests, fixed_tests):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"
            repo.mkdir()
            self.git(repo, "init", "-q")
            self.git(repo, "config", "user.name", "fixture")
            self.git(repo, "config", "user.email", "fixture@example.invalid")
            (repo / "value.py").write_text(bad_value)
            tests = repo / "tests"
            tests.mkdir()
            testfile = tests / "test_value.py"
            testfile.write_text("import unittest, value\nclass Replay(unittest.TestCase):\n" + bad_tests)
            self.git(repo, "add", ".")
            self.git(repo, "commit", "-qm", "bad")
            bad = self.git(repo, "rev-parse", "HEAD")
            (repo / "value.py").write_text(fixed_value)
            testfile.write_text("import unittest, value\nclass Replay(unittest.TestCase):\n" + fixed_tests)
            self.git(repo, "add", ".")
            self.git(repo, "commit", "-qm", "fixed")
            fixed = self.git(repo, "rev-parse", "HEAD")

            return replay(repo, bad, fixed, root / "scratch")

    @staticmethod
    def git(repo, *args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                              text=True, env=dict(os.environ, GIT_CONFIG_NOSYSTEM="1")).stdout.strip()


if __name__ == "__main__":
    unittest.main()
