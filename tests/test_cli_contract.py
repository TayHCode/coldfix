"""The CLI contract: --help states it, --json results are versioned, and exit codes are 0, 1 or 2."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import coldfix as c

CLI = Path(c.__file__).resolve()
USAGE = "coldfix: invalid arguments; see --help\n"


class CliContractTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.cwd = Path(temp.name)

    def cli(self, *args):
        return subprocess.run([sys.executable, str(CLI), *args], cwd=self.cwd, text=True, capture_output=True,
                              timeout=120)

    def test_version_flag_prints_package_version_and_exits_zero(self):
        match = re.search(r'^version = "([^"]+)"$', (CLI.parent / "pyproject.toml").read_text(), re.M)
        self.assertIsNotNone(match)
        expected = f"coldfix {match.group(1)}\n"
        for flag in ("--version", "-V"):
            with self.subTest(flag=flag):
                done = self.cli(flag)
                self.assertEqual((done.returncode, done.stdout, done.stderr), (0, expected, ""))

    def test_help_states_the_exit_and_json_contract(self):
        for args in (("--help",), ("check", "--help"), ("status", "--help")):
            with self.subTest(args=args):
                done = self.cli(*args)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertIn("Exit codes: 0 pass, 1 finding or failed check, 2 usage or error. With --json, stdout is one "
                              "object with schema_version 1 and exit_code.", " ".join(done.stdout.split()))

    def test_bad_input_exits_two_without_echoing_values(self):
        for args, versioned in ((("secret-unknown-command",), False), (("status", "--secret-flag"), False),
                                (("status", "--json", "--secret-flag"), True), (("check", "--js", "--secret-flag"), True),
                                (("status", "--json=secret"), True), (("check", "--", "--json", "secret"), False)):
            with self.subTest(args=args):
                done = self.cli(*args)
                self.assertEqual((done.returncode, done.stderr), (2, USAGE))
                self.assertEqual(done.stdout and json.loads(done.stdout),
                                 {"schema_version": 1, "exit_code": 2} if versioned else "")
                self.assertNotIn("secret", done.stdout + done.stderr)

    def test_json_results_are_versioned_and_match_the_exit_code(self):
        for args in (("status", "--json"), ("check", "--json")):
            with self.subTest(args=args):
                done = self.cli(*args)
                result = json.loads(done.stdout)
                self.assertIn(done.returncode, (0, 1), done.stderr)
                self.assertEqual((result["schema_version"], result["exit_code"]), (1, done.returncode))
        attempt = self.cwd / ".coldfix" / "runs" / "run-corrupt"
        attempt.mkdir(parents=True)
        for folder in (attempt.parent.parent, attempt.parent, attempt):
            folder.chmod(0o700)
        (attempt / "run.json").write_text("{broken")
        done = self.cli("status", "--json")
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertEqual(json.loads(done.stdout), {"schema_version": 1, "exit_code": 2})
        self.assertTrue(done.stderr.startswith("coldfix: "), done.stderr)
        (self.cwd / "loop").symlink_to(self.cwd / "loop")
        done = self.cli("check", "--json", "loop")
        self.assertEqual((done.returncode, done.stderr), (2, "coldfix: check needs a directory to scan\n"))
        self.assertEqual(json.loads(done.stdout), {"schema_version": 1, "exit_code": 2})

    def test_absent_fleet_cli_is_off_and_names_the_standalone_route(self):
        import replay_isolation
        with patch.dict(os.environ, {"COLDFIX_FLEET_CLI": "coldfix-absent-tool", "PATH": str(self.cwd)}), \
                patch.object(replay_isolation, "require_sandbox", return_value={"sandbox": "available"}):
            self.assertEqual(c.doctor()["fleet_cli"],
                             "off: coldfix-absent-tool is not installed; install it or set COLDFIX_MODEL_CMD to run standalone")

    def test_doctor_json_reports_success_and_runtime_error(self):
        with patch.dict(os.environ, {"COLDFIX_REPLAY_RUNNER": "sh -c {script}"}):
            done = self.cli("doctor", "--json")
        self.assertEqual(done.returncode, 0, done.stderr)
        result = json.loads(done.stdout)
        self.assertEqual((result["schema_version"], result["exit_code"], result["sandbox"]), (1, 0, "off"))

        with patch.dict(os.environ, {"COLDFIX_REPLAY_RUNNER": "sh"}):
            done = self.cli("doctor", "--json")
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertEqual(json.loads(done.stdout), {"schema_version": 1, "exit_code": 2})
        self.assertIn("COLDFIX_REPLAY_RUNNER must contain {script}", done.stderr)


if __name__ == "__main__":
    unittest.main()
