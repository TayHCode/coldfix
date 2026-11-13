import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import coldfix as c
import coldrecall
import regression_adapter
import replay_isolation
from test_cli import run_main

BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
HEADER = ("import sys\nimport unittest\nfrom pathlib import Path\n"
          "sys.path.insert(0, str(Path(__file__).resolve().parents[1]))\nimport calc\n\n\n"
          "class CalcTests(unittest.TestCase):\n")
REAL_TEST = HEADER + "    def test_add_sums(self):\n        self.assertEqual(calc.add(2, 3), 5)\n"
VACUOUS_TEST = HEADER + "    def test_add_exists(self):\n        self.assertTrue(callable(calc.add))\n"
NO_TEST = HEADER + "    def helper_add_sums(self):\n        self.assertEqual(calc.add(2, 3), 5)\n"
DIAGNOSTIC = "FAIL: test_add_sums"
ERROR = "FAIL: test_add_sums (test_calc.CalcTests.test_add_sums)\nAssertionError: -1 != 5"
SOURCE = "https://github.com/example/calc/pull/7"


class AdmissionCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.cwd = Path(self.temporary.name)
        self.store = self.cwd / ".coldfix"

    def git(self, repo, *args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()

    def repo(self, test_body=REAL_TEST, name="repo"):
        repo = self.cwd / name
        (repo / "tests").mkdir(parents=True)
        self.git(repo, "init", "-q")
        self.git(repo, "config", "user.name", "Fixture")
        self.git(repo, "config", "user.email", "fixture@example.invalid")
        (repo / "calc.py").write_text(BUGGY)
        self.git(repo, "add", ".")
        self.git(repo, "commit", "-qm", "bug")
        before = self.git(repo, "rev-parse", "HEAD")
        (repo / "calc.py").write_text(FIXED)
        (repo / "tests" / "test_calc.py").write_text(test_body)
        self.git(repo, "add", ".")
        self.git(repo, "commit", "-qm", "fix and test")
        return repo, before, self.git(repo, "rev-parse", "HEAD")

    def failure(self, error=ERROR, source=SOURCE):
        row = c.event_record(dict(command="python3 -m unittest", error=error, repo="example/fixture-app", agent="codex",
                                  kind="test", source_ref=source, occurred="2026-10-30T12:00:00Z"), "failure-ingest")
        c.save(self.store, "failures", row)
        return row["id"]

    def cli(self, *args, code=0, env=None):
        result = run_main(("--store", str(self.store), *args), os.getcwd(), env=env)
        self.assertEqual(result.returncode, code, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return result

    def admit(self, failure, repo, before, after, *, diagnostic=DIAGNOSTIC, test_path="tests/test_calc.py", code=0,
              lesson_failure="add subtracted instead of summing.", env=None, rule="Keep add summing its operands."):
        return self.cli("admit", "--failure", failure, "--repo", str(repo), "--before", before, "--after", after,
                        "--test-path", test_path, "--diagnostic", diagnostic,
                        "--title", "Add sums its operands", "--rule", rule,
                        "--lesson-failure", lesson_failure, "--fix", "Return a + b.",
                        "--timeout", "120", code=code, env=env)

    def records(self, collection):
        return [json.loads(path.read_text()) for path in sorted((self.store / collection).glob("*.json"))]

    def proven(self):
        return [row for row in self.records("lessons") if row["verification"] == "proven"]


class AdmissionTests(AdmissionCase):
    def path_without_bwrap(self, fake_bwrap=None):
        tools = self.cwd / "tools"
        tools.mkdir()
        (tools / "git").symlink_to(shutil.which("git"))
        if fake_bwrap is not None:
            (tools / "bwrap").write_text(fake_bwrap)
            (tools / "bwrap").chmod(0o755)
        return dict(os.environ, PATH=str(tools))

    def test_a_rule_that_only_names_the_failing_test_or_assertion_is_not_admitted(self):
        repo, before, after = self.repo()
        failure = self.failure()
        for rule in ("test_add_sums must pass.", "FAIL: test_add_sums should not fail again.",
                     "Keep `self.assertEqual(calc.add(2, 3), 5)` green."):
            result = self.admit(failure, repo, before, after, rule=rule, code=2)
            self.assertIn("rule only names the failing test or assertion", result.stderr)
        self.assertEqual(self.records("lessons"), [])

    def test_admission_without_bwrap_fails_naming_the_missing_sandbox(self):
        repo, before, after = self.repo()
        result = self.admit(self.failure(), repo, before, after, code=2, env=self.path_without_bwrap())
        self.assertEqual(result.stderr, "coldfix: sandbox unavailable: bwrap is not installed; install bubblewrap "
                         "(sudo apt install bubblewrap); on Ubuntu 23.10 or later, let bwrap create user namespaces "
                         "with the AppArmor profile in the README section \"Replay sandbox\"\n")
        self.assertEqual(self.records("receipts"), [])
        self.assertEqual(self.proven(), [])

    def test_admission_where_bwrap_cannot_create_namespaces_fails_with_the_bwrap_error(self):
        repo, before, after = self.repo()
        blocked = "#!/bin/sh\necho 'bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted' >&2\nexit 1\n"
        result = self.admit(self.failure(), repo, before, after, code=2, env=self.path_without_bwrap(blocked))
        self.assertEqual(result.stderr, "coldfix: sandbox unavailable: bwrap cannot create the sandbox "
                         "(bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted); on Ubuntu 23.10 or later, "
                         "let bwrap create user namespaces with the AppArmor profile in the README section "
                         "\"Replay sandbox\"\n")
        self.assertEqual(self.records("receipts"), [])

    def test_admission_rejects_a_shorter_diagnostic_name(self):
        repo, before, after = self.repo()
        failure = self.failure(error='FAIL: test_add_sums_extra (test_calc.CalcTests.test_add_sums_extra)')
        result = self.admit(failure, repo, before, after, diagnostic='FAIL: test_add_sums', code=2)
        self.assertIn('diagnostic is missing from the recorded failure', result.stderr)

    def test_a_correction_needs_its_diagnostic_unless_a_transcript_replay_generated_the_test(self):
        repo, before, after = self.repo()
        for command, message in (('correction', 'diagnostic is missing from the recorded failure'),
                                 ('transcript correction', 'proven only by a generated named unittest')):
            row = c.event_record(dict(command=command, error='The agent subtracted.', repo='example/fixture-app',
                                      agent='codex', kind='correction', source_ref=SOURCE,
                                      occurred='2026-10-30T12:00:00Z'), 'failure-ingest')
            c.save(self.store, 'failures', row)
            self.assertIn(message, self.admit(row['id'], repo, before, after, code=2).stderr)
        self.assertEqual(self.proven(), [])

    def test_existing_check_templates_remain_valid_after_exact_selection_is_added(self):
        for exact in (False, True):
            check, _, digest = regression_adapter.build_existing('tests/test_calc.py', 'a' * 64,
                                                                  'test_add_sums', DIAGNOSTIC, exact=exact)
            self.assertTrue(c.check_pins(check, digest))
            check, _, digest = regression_adapter.build_existing_pair('tests/test_calc.py', ['a' * 64, 'b' * 64],
                                                                       'test_add_sums', DIAGNOSTIC, exact=exact)
            self.assertTrue(c.check_pins(check, digest))

    def test_existing_test_survives_unrelated_private_test_file_change(self):
        repo = self.cwd / 'changed-test'
        (repo / 'tests').mkdir(parents=True)
        self.git(repo, 'init', '-q')
        self.git(repo, 'config', 'user.name', 'Fixture')
        self.git(repo, 'config', 'user.email', 'fixture@example.invalid')
        (repo / 'calc.py').write_text(BUGGY)
        test_source = REAL_TEST + '    def test_add_sums_extra(self):\n        self.assertTrue(True)\n'
        (repo / 'tests/test_calc.py').write_text(test_source)
        self.git(repo, 'add', '.')
        self.git(repo, 'commit', '-qm', 'bug and existing test')
        before = self.git(repo, 'rev-parse', 'HEAD')
        (repo / 'calc.py').write_text(FIXED)
        (repo / 'tests/test_calc.py').write_text(test_source + '\nfixture = "private@example.com"\n')
        self.git(repo, 'add', '.')
        self.git(repo, 'commit', '-qm', 'fix and unrelated test change')
        after = self.git(repo, 'rev-parse', 'HEAD')
        result = json.loads(self.admit(self.failure(), repo, before, after).stdout)
        receipt, = self.records('receipts')
        self.assertEqual([receipt[side]['status'] for side in ('before', 'after', 'reversed')],
                         ['FAIL', 'PASS', 'FAIL'])
        output = self.cwd / ('lesson-' + result['lesson_id'] + '.md')
        self.cli('export', '--lesson', result['id'], '--reviewed', '--output', str(output))
        self.assertNotIn('private@example.com', output.read_text())
        self.assertIn('email', c.privacy_counts('private@example.com'))

    def test_existing_unchanged_unittest_proves_the_fix_and_reverse(self):
        repo = self.cwd / 'existing-test'
        (repo / 'tests').mkdir(parents=True)
        (repo / 'src').mkdir()
        self.git(repo, 'init', '-q')
        self.git(repo, 'config', 'user.name', 'Fixture')
        self.git(repo, 'config', 'user.email', 'fixture@example.invalid')
        (repo / 'src/calc.py').write_text(BUGGY)
        (repo / 'tests/test_calc.py').write_text(REAL_TEST + '    def test_add_sums_extra(self):\n        self.assertTrue(True)\n')
        self.git(repo, 'add', '.')
        self.git(repo, 'commit', '-qm', 'bug and existing test')
        before = self.git(repo, 'rev-parse', 'HEAD')
        (repo / 'src/calc.py').write_text(FIXED)
        self.git(repo, 'add', 'src/calc.py')
        self.git(repo, 'commit', '-qm', 'fix')
        after = self.git(repo, 'rev-parse', 'HEAD')
        result = json.loads(self.admit(self.failure(), repo, before, after).stdout)
        receipt, = self.records('receipts')
        self.assertEqual([receipt[side]['status'] for side in ('before', 'after', 'reversed')],
                         ['FAIL', 'PASS', 'FAIL'])
        self.assertTrue(receipt.get('repo_root'))
        output = self.cwd / ('lesson-' + result['lesson_id'] + '.md')
        self.cli('export', '--lesson', result['id'], '--reviewed', '--output', str(output))
        proof, _, _ = coldrecall.read_rule(output)
        self.assertEqual(proof['replay']['reversed']['status'], 'FAIL')

    def pytest_pair(self, test_source):
        repo = self.cwd / 'pytest-pair'
        (repo / 'tests').mkdir(parents=True)
        self.git(repo, 'init', '-q')
        self.git(repo, 'config', 'user.name', 'Fixture')
        self.git(repo, 'config', 'user.email', 'fixture@example.invalid')
        (repo / 'calc.py').write_text(BUGGY)
        (repo / 'tests/test_calc.py').write_text(test_source)
        self.git(repo, 'add', '.')
        self.git(repo, 'commit', '-qm', 'bug and existing test')
        before = self.git(repo, 'rev-parse', 'HEAD')
        (repo / 'calc.py').write_text(FIXED)
        self.git(repo, 'add', 'calc.py')
        self.git(repo, 'commit', '-qm', 'fix')
        return repo, before, self.git(repo, 'rev-parse', 'HEAD')

    def test_an_existing_test_that_pytest_reported_failing_proves_the_fix_and_reverse(self):
        name = 'test_add_sums_even_when_the_pytest_summary_line_is_wider_than_an_eighty_column_terminal'
        repo, before, after = self.pytest_pair(REAL_TEST.replace('test_add_sums', name))
        diagnostic = 'FAILED tests/test_calc.py::CalcTests::' + name
        failure = self.failure(error='2026-10-30T12:00:00Z ' + diagnostic + ' - AssertionError: -1 != 5\n1 failed in 0.01s')
        result = json.loads(self.admit(failure, repo, before, after, diagnostic=diagnostic).stdout)
        receipt, = self.records('receipts')
        self.assertEqual([receipt[side]['status'] for side in ('before', 'after', 'reversed')], ['FAIL', 'PASS', 'FAIL'])
        lesson, = self.proven()
        self.assertEqual(regression_adapter.payload(lesson['check_script'])['mode'], 'pytest_pair')
        self.assertTrue(c.check_pins(lesson['check_script'], lesson['admission']['test_sha256'], lesson['admission']))
        output = self.cwd / ('lesson-' + result['lesson_id'] + '.md')
        self.cli('export', '--lesson', result['id'], '--reviewed', '--output', str(output))
        proof, _, _ = coldrecall.read_rule(output)
        self.assertEqual(proof['replay']['reversed']['status'], 'FAIL')

    def test_a_parametrized_pytest_case_proves_the_fix_and_reverse(self):
        source = ("import sys\nfrom pathlib import Path\nimport pytest\n"
                  "sys.path.insert(0, str(Path(__file__).resolve().parents[1]))\nimport calc\n\n\n"
                  "@pytest.mark.parametrize('a,b', [(2, 3)])\ndef test_add(a, b):\n    assert calc.add(a, b) == 5\n")
        repo, before, after = self.pytest_pair(source)
        diagnostic = 'FAILED tests/test_calc.py::test_add[2-3]'
        failure = self.failure(error=diagnostic + ' - assert -1 == 5\n1 failed in 0.01s')
        self.admit(failure, repo, before, after, diagnostic=diagnostic)
        receipt, = self.records('receipts')
        self.assertEqual([receipt[side]['status'] for side in ('before', 'after', 'reversed')], ['FAIL', 'PASS', 'FAIL'])

    def test_a_pytest_test_that_changed_between_revisions_proves_nothing(self):
        repo, before, after = self.pytest_pair(REAL_TEST.replace('calc.add(2, 3), 5', 'calc.add(5, 5), 10'))
        (repo / 'tests/test_calc.py').write_text(REAL_TEST)
        self.git(repo, 'commit', '-qam', 'weaken the test')
        after = self.git(repo, 'rev-parse', 'HEAD')
        diagnostic = 'FAILED tests/test_calc.py::CalcTests::test_add_sums'
        failure = self.failure(error=diagnostic + ' - AssertionError: 0 != 10')
        result = self.admit(failure, repo, before, after, diagnostic=diagnostic, code=2)
        self.assertIn("pytest test changed between revisions", result.stderr)
        self.assertEqual(self.records('lessons'), [])

    def test_a_fix_that_only_changes_a_pytest_conftest_proves_nothing(self):
        source = REAL_TEST.replace('calc.add(2, 3), 5', 'calc.add(2, 3), EXPECTED').replace(
            'import unittest\n', 'import unittest\nfrom conftest import EXPECTED\n')
        repo = self.cwd / 'pytest-conftest'
        (repo / 'tests').mkdir(parents=True)
        self.git(repo, 'init', '-q')
        self.git(repo, 'config', 'user.name', 'Fixture')
        self.git(repo, 'config', 'user.email', 'fixture@example.invalid')
        (repo / 'calc.py').write_text(BUGGY)
        (repo / 'tests/test_calc.py').write_text(source)
        (repo / 'tests/conftest.py').write_text('EXPECTED = 5\n')
        self.git(repo, 'add', '.')
        self.git(repo, 'commit', '-qm', 'bug')
        before = self.git(repo, 'rev-parse', 'HEAD')
        (repo / 'tests/conftest.py').write_text('EXPECTED = -1\n')
        self.git(repo, 'commit', '-qam', 'weaken the expectation')
        after = self.git(repo, 'rev-parse', 'HEAD')
        diagnostic = 'FAILED tests/test_calc.py::CalcTests::test_add_sums'
        failure = self.failure(error=diagnostic + ' - AssertionError: -1 != 5')
        result = self.admit(failure, repo, before, after, diagnostic=diagnostic, code=2)
        self.assertIn("pytest test inputs changed between revisions", result.stderr)
        self.assertEqual(self.records('lessons'), [])

    def test_a_pytest_failure_that_is_not_an_assertion_proves_nothing(self):
        broken = REAL_TEST.replace('self.assertEqual(calc.add(2, 3), 5)', 'calc.missing()')
        repo, before, after = self.pytest_pair(broken)
        diagnostic = 'FAILED tests/test_calc.py::CalcTests::test_add_sums'
        failure = self.failure(error=diagnostic + ' - AttributeError: missing')
        result = self.admit(failure, repo, before, after, diagnostic=diagnostic, code=2)
        self.assertIn("recorded pytest failure has no assertion message", result.stderr)
        self.assertEqual(self.proven(), [])

    def test_a_replayed_assertion_that_differs_from_the_recorded_one_proves_nothing(self):
        repo, before, after = self.pytest_pair(REAL_TEST)
        diagnostic = 'FAILED tests/test_calc.py::CalcTests::test_add_sums'
        failure = self.failure(error=diagnostic + ' - AssertionError: 7 != 5\n1 failed in 0.01s')
        result = self.admit(failure, repo, before, after, diagnostic=diagnostic, code=2)
        self.assertIn("red INCOMPLETE, green PASS", result.stderr)
        self.assertEqual(self.proven(), [])

    def test_real_singleton_is_admitted_with_red_green_and_reversed_red(self):
        repo, before, after = self.repo()
        failure = self.failure()
        result = json.loads(self.admit(failure, repo, before, after).stdout)
        lesson, = self.proven()
        receipt, = self.records("receipts")
        self.assertEqual((lesson["version"], lesson["evidence"], lesson["status"]), (4, [failure], "active"))
        self.assertEqual(lesson["receipt"], receipt["id"])
        self.assertEqual((receipt["version"], receipt["status"]), (2, "PASS"))
        self.assertEqual([receipt[side]["status"] for side in ("before", "after", "reversed")], ["FAIL", "PASS", "FAIL"])
        self.assertEqual(len({receipt[side]["tree"] for side in ("before", "after", "reversed")}), 3)
        self.assertEqual(receipt["after"]["commit"], after)
        self.assertEqual(result["test_sha256"], lesson["admission"]["test_sha256"])
        self.assertEqual(receipt["test_sha256"], lesson["admission"]["test_sha256"])
        self.assertTrue(c.check_pins(lesson["check_script"], receipt["test_sha256"]))
        self.assertEqual(lesson["admission"]["source_ref"], SOURCE)
        self.assertEqual(self.cli("lesson").returncode, 0)

    def test_explain_prints_a_ci_lessons_evidence_receipts_scope_and_history(self):
        repo, before, after = self.repo()
        row = c.event_record(dict(command="python3 -m unittest", error=ERROR, repo="example/fixture-app", agent="github-actions",
                                  kind="ci", source_ref="https://github.com/example/calc/actions/runs/7",
                                  occurred="2026-10-30T12:00:00Z"), "coldbox")
        c.save(self.store, "failures", row)
        failure = row["id"]
        self.admit(failure, repo, before, after)
        lesson, = self.proven()
        receipt, = self.records("receipts")
        for name in (lesson["lesson_id"], lesson["id"]):
            shown = json.loads(self.cli("explain", name).stdout)
            self.assertEqual((shown["lesson"]["lesson_id"], shown["lesson"]["verification"]), (lesson["lesson_id"], "proven"))
            self.assertEqual(shown["scope"], lesson["scope"])
            evidence, = shown["evidence"]
            self.assertEqual((evidence["id"], evidence["source_ref"], evidence["repo"], evidence["kind"]),
                             ("coldfix:failure:" + failure, "https://github.com/example/calc/actions/runs/7", "example/fixture-app", "ci"))
            shown_receipt, = shown["receipts"]
            self.assertEqual((shown_receipt["id"], shown_receipt["status"]), ("coldfix:replay:" + receipt["id"], "PASS"))
            self.assertEqual([shown_receipt[side]["status"] for side in ("before", "after", "reversed")], ["FAIL", "PASS", "FAIL"])
            self.assertEqual(shown_receipt["after"]["commit"], after)
            origin, = [row for row in self.records("lessons") if row["id"] == lesson["previous"]]
            self.assertEqual(shown["history"], [
                dict(id=origin["id"], created=origin["created"], status="proposed", verification="unproven", receipt=None),
                dict(id=lesson["id"], created=lesson["created"], status="active", verification="proven", receipt=receipt["id"])])
        self.assertIn("unknown lesson", self.cli("explain", "0" * 32, code=2).stderr)

    def test_admitted_lesson_exports_a_schema_2_proof_that_recall_reads(self):
        repo, before, after = self.repo()
        self.admit(self.failure(), repo, before, after)
        lesson, = self.proven()
        output = self.cwd / "exports" / ("lesson-" + lesson["lesson_id"] + ".md")
        output.parent.mkdir()
        self.cli("export", "--lesson", lesson["id"], "--reviewed", "--output", str(output))
        proof, _, _ = coldrecall.read_rule(output)
        self.assertEqual(proof["schema_version"], 2)
        self.assertEqual(sorted(proof["replay"]), ["after", "before", "check_sha256", "expected_failure",
                                                   "receipt", "reversed", "test_sha256"])
        self.assertEqual(proof["replay"]["reversed"]["status"], "FAIL")
        admission = lesson["admission"]
        self.assertEqual(proof["admission"], dict(
            kind="verified_incident", diagnostic=admission["diagnostic"], test_sha256=admission["test_sha256"],
            source_ref_sha256=c.digest(SOURCE), test_paths_sha256=[c.digest("tests/test_calc.py")]))
        self.assertNotIn(SOURCE, output.read_text())
        self.assertEqual(len(proof["source_evidence"]), 1)

    def test_vacuous_test_is_rejected(self):
        repo, before, after = self.repo(VACUOUS_TEST)
        result = self.admit(self.failure(), repo, before, after, code=2)
        self.assertIn("incident not admitted; red PASS", result.stderr)
        self.assertEqual(self.proven(), [])
        output = self.cwd / "lesson.md"
        for lesson in self.records("lessons"):
            self.cli("export", "--lesson", lesson["id"], "--reviewed", "--output", str(output), code=2)
        self.assertFalse(output.exists())

    def test_credential_in_the_regression_test_is_blocked_at_admission_and_export(self):
        secret = "ghp_" + "a1B2c3D4e5F6g7H8i9" + "J0k1L2m3N4o5P6q7R8"
        repo, before, after = self.repo(REAL_TEST + "\n# token=" + secret + "\n")
        result = self.admit(self.failure(), repo, before, after, code=2)
        self.assertIn("privacy screen blocked", result.stderr)
        self.assertNotIn(secret, result.stderr)
        self.assertEqual(self.records("lessons"), [])

    def test_private_term_inside_the_encoded_patch_is_blocked_at_export(self):
        repo, before, after = self.repo(REAL_TEST + "\n# zebrafruit\n")
        self.admit(self.failure(), repo, before, after)
        lesson, = self.proven()
        denylist = self.cwd / "denylist.txt"
        denylist.write_text("zebrafruit\n")
        output = self.cwd / "tainted.md"
        result = self.cli("export", "--lesson", lesson["id"], "--reviewed", "--denylist", str(denylist),
                          "--output", str(output), code=2)
        self.assertIn("privacy screen blocked", result.stderr)
        self.assertFalse(output.exists())
        self.cli("export", "--lesson", lesson["id"], "--reviewed", "--output", str(output))

    def test_private_term_in_encoded_test_filename_is_blocked_at_export(self):
        repo, before, _ = self.repo()
        self.git(repo, "mv", "tests/test_calc.py", "tests/zebrafruit.py")
        self.git(repo, "commit", "-qm", "rename test")
        after = self.git(repo, "rev-parse", "HEAD")
        self.admit(self.failure(), repo, before, after, test_path="tests/zebrafruit.py")
        lesson, = self.proven()
        denylist = self.cwd / "denylist.txt"
        denylist.write_text("zebrafruit\n")
        output = self.cwd / "tainted.md"
        result = self.cli("export", "--lesson", lesson["id"], "--reviewed", "--denylist", str(denylist),
                          "--output", str(output), code=2)
        self.assertIn("privacy screen blocked", result.stderr)
        self.assertFalse(output.exists())
        self.cli("export", "--lesson", lesson["id"], "--reviewed", "--output", str(output))

    def test_denylisted_filename_with_extension_is_blocked_at_export(self):
        repo, before, after = self.repo(REAL_TEST + "\n# see helpers.py\n")
        self.admit(self.failure(), repo, before, after)
        lesson, = self.proven()
        denylist = self.cwd / "denylist.txt"
        denylist.write_text("helpers.py\n")
        output = self.cwd / "tainted.md"
        result = self.cli("export", "--lesson", lesson["id"], "--reviewed", "--denylist", str(denylist),
                          "--output", str(output), code=2)
        self.assertIn("privacy screen blocked", result.stderr)
        self.assertFalse(output.exists())

    def test_hostname_with_file_extension_is_blocked_at_admission(self):
        for endpoint in ("https://confidential.md", "wss://confidential.sh/events",
                         "ftp://confidential.py/file", "//confidential.md/path"):
            with self.subTest(endpoint=endpoint):
                self.setUp()
                repo, before, after = self.repo(REAL_TEST + "\n# endpoint: " + endpoint + "\n")
                result = self.admit(self.failure(), repo, before, after, code=2)
                self.assertIn("privacy screen blocked", result.stderr)

    def test_python_attribute_access_and_the_unittest_id_are_not_hostnames(self):
        body = REAL_TEST.replace("import sys\n", "import subprocess\nimport sys\n") + (
            "\n    def test_tools_resolve(self):\n        run = subprocess.run\n        home = Path.home\n"
            "        self.assertTrue(callable(run) and callable(home))\n")
        repo, before, after = self.repo(body)
        self.admit(self.failure(), repo, before, after, lesson_failure=ERROR.splitlines()[0])
        self.assertEqual(len(self.proven()), 1)

    def test_a_unittest_id_outside_the_admitted_test_file_is_still_screened_as_a_hostname(self):
        body = REAL_TEST.replace("import sys\n", "import subprocess\nimport sys\n") + (
            "\n    def test_tools_resolve(self):\n        run = subprocess.run\n"
            "        self.assertTrue(callable(run))\n")
        repo, before, after = self.repo(body)
        result = self.admit(self.failure(), repo, before, after, code=2,
                            lesson_failure="FAIL: test_x (secret.internal.test_x)")
        self.assertIn("privacy screen blocked", result.stderr)
        self.assertEqual(self.records("lessons"), [])

    def test_a_hostname_in_a_string_is_blocked_even_beside_python_attribute_access(self):
        body = REAL_TEST.replace("import sys\n", "import subprocess\nimport sys\n") + (
            "\n    def test_tools_resolve(self):\n        run = subprocess.run\n"
            "        self.assertTrue(callable(run) and \"build.internal.example.com\")\n")
        repo, before, after = self.repo(body)
        result = self.admit(self.failure(), repo, before, after, code=2)
        self.assertIn("privacy screen blocked", result.stderr)
        self.assertEqual(self.records("lessons"), [])

    def test_a_mock_target_string_is_not_a_hostname_but_a_url_host_is(self):
        body = REAL_TEST.replace("import sys\n", "import subprocess\nimport sys\n") + (
            "\n    def test_tools_resolve(self):\n        run = subprocess.run\n"
            "        self.assertTrue(callable(run) and \"fix_vm.subprocess.run\" and \"user.name\")\n")
        repo, before, after = self.repo(body)
        self.admit(self.failure(), repo, before, after)
        self.assertEqual(len(self.proven()), 1)
        self.setUp()
        repo, before, after = self.repo(body.replace("\"user.name\"", "\"https://deploy.run/hook\""))
        result = self.admit(self.failure(), repo, before, after, code=2)
        self.assertIn("privacy screen blocked", result.stderr)

    def test_source_file_names_pass_but_real_hostnames_in_the_patch_are_blocked(self):
        named = REAL_TEST + "\n# see helpers.py and notes.md\n"
        repo, before, after = self.repo(named)
        self.admit(self.failure(), repo, before, after)
        self.assertEqual(len(self.proven()), 1)
        self.setUp()
        repo, before, after = self.repo(REAL_TEST + "\n# fetched from build.internal.example.com\n")
        result = self.admit(self.failure(), repo, before, after, code=2)
        self.assertIn("privacy screen blocked", result.stderr)
        self.assertEqual(self.records("lessons"), [])

    def test_corrected_retry_after_a_failed_admission_is_admitted_once(self):
        repo, before, after = self.repo()
        failure = self.failure()
        self.admit(failure, repo, before, after, diagnostic="FAIL: test_other", code=2)
        self.assertEqual(self.proven(), [])
        self.admit(failure, repo, before, after)
        self.assertEqual(len(self.proven()), 1)
        again = self.admit(failure, repo, before, after, code=2)
        self.assertIn("incident already has a lesson", again.stderr)
        self.assertEqual(len(self.proven()), 1)

    def test_retry_after_a_diagnostic_missing_from_the_test_output_is_admitted_once(self):
        repo, before, after = self.repo()
        failure = self.failure(error=ERROR + "\nnote: only in the log")
        first = self.admit(failure, repo, before, after, diagnostic="note: only in the log", code=2)
        self.assertIn("incident not admitted", first.stderr)
        self.assertEqual(self.proven(), [])
        self.admit(failure, repo, before, after)
        lesson, = self.proven()
        self.assertEqual(lesson["admission"]["diagnostic"], DIAGNOSTIC)
        again = self.admit(failure, repo, before, after, code=2)
        self.assertIn("incident already has a lesson", again.stderr)
        self.assertEqual(len(self.proven()), 1)

    def test_retry_after_a_vacuous_attempt_is_admitted_with_a_real_test(self):
        repo, before, after = self.repo(VACUOUS_TEST)
        failure = self.failure()
        self.admit(failure, repo, before, after, code=2)
        (repo / "tests" / "test_calc.py").write_text(REAL_TEST)
        self.git(repo, "commit", "-qam", "real test")
        self.admit(failure, repo, before, self.git(repo, "rev-parse", "HEAD"))
        self.assertEqual(len(self.proven()), 1)

    def test_failed_admission_cannot_be_replayed_after_corrected_retry(self):
        replay_isolation.require_sandbox()
        repo, before, after = self.repo()
        (repo / "calc.py").write_text("def add(a, b):\n    return a - b + 0\n")
        self.git(repo, "commit", "-qam", "still broken")
        broken = self.git(repo, "rev-parse", "HEAD")
        failure = self.failure()
        self.admit(failure, repo, before, broken, code=2)
        failed, = [row for row in self.records("lessons") if row["receipt"] is not None]
        self.admit(failure, repo, before, after)
        result = self.cli("replay", "--lesson", failed["id"], "--repo", str(repo), "--before", before,
                          "--after", after, "--status", "active", code=2)
        self.assertIn("incident already has a lesson", result.stderr)
        self.assertEqual(len(self.proven()), 1)

    def test_retry_of_an_admission_that_fails_before_a_receipt_adds_no_lesson(self):
        repo, before, _ = self.repo()
        (repo / "tests" / "test_calc.py").write_text(REAL_TEST + "    def test_add_zero(self):\n        self.assertTrue(True)\n")
        self.git(repo, "commit", "-qam", "test only")
        after = self.git(repo, "rev-parse", "HEAD")
        failure = self.failure()
        first = self.admit(failure, repo, after + "~1", after, code=2)
        lessons = self.records("lessons")
        self.assertEqual([row["receipt"] for row in lessons], [None])
        again = self.admit(failure, repo, after + "~1", after, code=2)
        self.assertEqual(again.stderr, first.stderr)
        self.assertEqual(self.records("lessons"), lessons)

    def test_test_that_runs_nothing_is_rejected(self):
        repo, before, after = self.repo(NO_TEST)
        result = self.admit(self.failure(), repo, before, after, code=2)
        self.assertIn("incident not admitted; red INCOMPLETE", result.stderr)
        self.assertEqual(self.proven(), [])

    def test_duplicate_incident_is_rejected(self):
        repo, before, after = self.repo()
        failure = self.failure()
        self.admit(failure, repo, before, after)
        counts = [len(self.records(name)) for name in ("lessons", "receipts")]
        again = self.admit(failure, repo, before, after, code=2)
        self.assertIn("already has a lesson", again.stderr)
        self.assertEqual([len(self.records(name)) for name in ("lessons", "receipts")], counts)
        self.assertEqual(len(self.proven()), 1)

    def test_fabricated_recurrence_is_rejected(self):
        repo, before, after = self.repo()
        first = self.failure()
        self.failure()
        result = self.admit(first, repo, before, after, code=2)
        self.assertIn("a repeated import is not a recurrence", result.stderr)
        self.assertEqual(self.records("lessons"), [])

    def test_recurring_signature_is_not_a_singleton(self):
        repo, before, after = self.repo()
        first = self.failure()
        self.failure(source="https://github.com/example/calc/pull/8")
        result = self.admit(first, repo, before, after, code=2)
        self.assertIn("recurring failure group", result.stderr)
        self.assertEqual(self.records("lessons"), [])

    def test_missing_diagnostic_is_rejected(self):
        repo, before, after = self.repo()
        result = self.admit(self.failure(), repo, before, after, diagnostic="FAIL: test_other", code=2)
        self.assertIn("diagnostic is missing", result.stderr)
        self.assertEqual(self.records("lessons"), [])

    def test_failure_without_a_source_reference_is_rejected(self):
        repo, before, after = self.repo()
        captured = json.loads(self.cli("capture", "--command", "python3 -m unittest", "--repo", "example",
                                       "--agent", "codex", "--error", ERROR).stdout)
        result = self.admit(captured["failure_id"], repo, before, after, code=2)
        self.assertIn("version 2 failure with a source reference", result.stderr)
        self.assertEqual(self.records("lessons"), [])

    def test_changed_check_is_not_replayed_or_exported(self):
        repo, before, after = self.repo()
        self.admit(self.failure(), repo, before, after)
        lesson, = self.proven()
        changed = self.cwd / "changed.py"
        changed.write_text('import json\nprint(json.dumps({"status": "FAIL", "failure": "regression:x"}))\n')
        revised = json.loads(self.cli("lesson", "--revise", lesson["id"], "--check-file", str(changed),
                                      "--expected-failure", "regression:x").stdout)
        replay = self.cli("replay", "--lesson", revised["id"], "--repo", str(repo), "--before", before,
                          "--after", after, "--status", "active", code=2)
        self.assertIn("check changed since admission", replay.stderr)
        self.assertEqual([row["id"] for row in self.proven()], [lesson["id"]])
        output = self.cwd / "revised.md"
        self.cli("export", "--lesson", revised["id"], "--reviewed", "--output", str(output), code=2)
        self.assertFalse(output.exists())

    def test_changed_check_with_original_digest_cannot_be_replayed(self):
        repo, before, after = self.repo()
        self.admit(self.failure(), repo, before, after)
        lesson, = self.proven()
        changed = self.cwd / "changed.py"
        fake = ("ok = 'return a + b' in open('calc.py').read()\n"
                "print(json.dumps({'status': 'PASS'} if ok else {'status': 'FAIL', 'failure': EXPECTED_FAILURE}))\n"
                "sys.exit(0 if ok else 1)")
        changed.write_text(lesson["check_script"].replace("sys.exit(main())", fake))
        revised = json.loads(self.cli("lesson", "--revise", lesson["id"], "--check-file", str(changed)).stdout)
        replay = self.cli("replay", "--lesson", revised["id"], "--repo", str(repo), "--before", before,
                          "--after", after, "--status", "active", code=2)
        self.assertIn("check changed since admission", replay.stderr)
        self.assertEqual([row["id"] for row in self.proven()], [lesson["id"]])

    def test_changed_command_cannot_bypass_the_admitted_check(self):
        repo, before, after = self.repo()
        self.admit(self.failure(), repo, before, after)
        lesson, = self.proven()
        revised = json.loads(self.cli("lesson", "--revise", lesson["id"],
                                      "--check-command", "python3 {check} # changed").stdout)
        replay = self.cli("replay", "--lesson", revised["id"], "--repo", str(repo), "--before", before,
                          "--after", after, "--status", "active", code=2)
        self.assertIn("check changed since admission", replay.stderr)

    def test_reversed_replay_without_a_product_change_is_rejected(self):
        repo, before, _ = self.repo()
        (repo / "tests" / "test_calc.py").write_text(REAL_TEST + "\n")
        self.git(repo, "commit", "-qam", "tests only")
        tests_only = self.git(repo, "rev-parse", "HEAD")
        result = self.admit(self.failure(), repo, tests_only, tests_only, code=2)
        self.assertIn("no change between the replay revisions", result.stderr)
        self.assertEqual(self.proven(), [])


class LegacyTests(AdmissionCase):
    def test_legacy_lesson_and_receipt_still_validate_and_export(self):
        repo, before, after = self.repo()
        for _ in range(2):
            self.cli("capture", "--command", "python3 -m unittest", "--repo", "example", "--agent", "codex",
                     "--error", "AssertionError: -1 != 5")
        group = json.loads(self.cli("propose").stdout)["groups"][0]["group_id"]
        check = self.cwd / "check.py"
        check.write_text("import json, sys\nfrom pathlib import Path\n"
                         "ok = 'a + b' in Path('calc' + chr(46) + 'py').read_text()\n"
                         "print(json.dumps({'status': 'PASS'} if ok else {'status': 'FAIL', 'failure': 'sum'}))\n"
                         "sys.exit(0 if ok else 1)\n")
        authored = json.loads(self.cli("lesson", "--group", group, "--grouping", "exact", "--kind", "test", "--level", "test",
            "--title", "Sum operands", "--rule", "Keep add summing its operands.", "--failure", "add subtracted.",
            "--fix", "Return a + b.", "--check-command", "python3 {check}", "--check-file", str(check),
            "--expected-failure", "sum").stdout)
        self.cli("replay", "--lesson", authored["id"], "--repo", str(repo), "--before", before, "--after", after,
                 "--status", "active")
        lesson, = self.proven()
        receipt, = self.records("receipts")
        self.assertEqual((lesson["version"], receipt["version"]), (3, 1))
        self.assertNotIn("reversed", receipt)
        output = self.cwd / "legacy.md"
        self.cli("export", "--lesson", lesson["id"], "--reviewed", "--output", str(output))
        proof, _, _ = coldrecall.read_rule(output)
        self.assertEqual(proof["schema_version"], 1)
        self.assertNotIn("reversed", proof["replay"])
        self.cli("lesson")


class AdapterTests(unittest.TestCase):
    def test_check_is_pinned_to_the_test_digest(self):
        patch = "diff --git a/t.py b/t.py\n+    def test_a(self):\n"
        script, expected, digest = regression_adapter.build("tests/t.py", patch, ["test_a"], "FAIL: test_a")
        self.assertEqual(digest, regression_adapter.test_digest(patch))
        self.assertEqual(expected, "regression:" + digest[:16])
        self.assertTrue(c.check_pins(script, digest))
        self.assertFalse(c.check_pins(script.replace(digest, "0" * 64), digest))

    def test_tampered_patch_is_incomplete(self):
        patch = ("diff --git a/tests/test_ok.py b/tests/test_ok.py\nnew file mode 100644\n"
                 "--- /dev/null\n+++ b/tests/test_ok.py\n@@ -0,0 +1,4 @@\n+import unittest\n"
                 "+class T(unittest.TestCase):\n+    def test_ok(self):\n+        pass\n")
        script, _, digest = regression_adapter.build("tests/test_ok.py", patch, ["test_ok"], "FAIL: test_ok")
        tampered = script.replace(digest, "0" * 64)
        with tempfile.TemporaryDirectory() as temporary:
            outcomes = []
            for name, body in (("genuine.py", script), ("tampered.py", tampered)):
                check = Path(temporary) / name
                check.write_text(body)
                result = subprocess.run([sys.executable, str(check)], cwd=temporary, capture_output=True, text=True)
                outcomes.append((result.returncode, result.stdout.strip()))
        self.assertEqual(outcomes, [(0, '{"status": "PASS"}'), (2, "")])


if __name__ == "__main__":
    unittest.main()
