import fcntl
import importlib.util
import json
import re
import shlex
import subprocess
import sys
import unittest

from test_cli import CLI, CliCase


class CliLessonTests(CliCase):
    def test_one_lesson_id_runs_from_propose_through_replay_to_export(self):
        self.recurring()
        group = json.loads(self.run_cli("propose").stdout)["groups"][0]["group_id"]
        check = self.cwd / "check.py"
        check.write_text("import json, sys\nfrom pathlib import Path\n"
                         "ok = Path('count.txt').read_text() == '2'\n"
                         "print(json.dumps({'status': 'PASS'} if ok else {'status': 'FAIL', 'failure': 'count-mismatch'}))\n"
                         "sys.exit(0 if ok else 1)\n")
        lesson_id = json.loads(self.run_cli(
            "lesson", "--group", group, "--kind", "test", "--level", "test", "--title", "Verify the count",
            "--rule", "Require the expected count.", "--failure", "The count was wrong.",
            "--fix", "Write the expected count.", "--check-file", str(check),
            "--check-command", "python3 {check}", "--expected-failure", "count-mismatch").stdout)["lesson_id"]
        repo, before, after = self.history()
        first = json.loads(self.run_replay(lesson_id, repo, before, after).stdout)
        self.assertEqual((first["lesson_id"], first["verification"]), (lesson_id, "proven"))
        self.assertNotIn(first["id"], first["next"])
        activated = subprocess.run(shlex.split(first["next"]), cwd=self.cwd, text=True, capture_output=True)
        self.assertEqual(activated.returncode, 0, activated.stderr)
        second = json.loads(activated.stdout)
        self.assertEqual((second["lesson_id"], second["verification"]), (lesson_id, "proven"))
        self.assertNotIn("next", second)
        self.assertEqual(self.lesson_record(second["id"])["status"], "active")
        self.run_cli("export", "--lesson", lesson_id, "--output", "lesson.md")
        self.assertIn("coldfix:lesson:" + lesson_id + ":" + second["id"], (self.cwd / "lesson.md").read_text())
        stale = self.run_cli("export", "--lesson", first["id"], "--output", "stale.md", code=2)
        self.assertIn("stale lesson revision", stale.stderr)
        self.run_replay(first["id"], repo, before, after, code=2)

    def test_a_rule_that_only_names_the_failing_test_or_assertion_is_refused(self):
        revision = self.author_lesson()
        for rule in ("test_count_matches must pass.", "Make tests/test_count.py::test_count_matches green again.",
                     "FAIL: test_count_matches", "Keep `assert count == 2` passing.",
                     "AssertionError: 1 != 2 should not fail.", "test_count_matches must pass on every build."):
            refused = self.run_cli("lesson", "--revise", revision, "--rule", rule, code=2)
            self.assertIn("rule only names the failing test or assertion; state the cause and the preventative behavior",
                          refused.stderr)
        quoted = json.loads(self.run_cli("lesson", "--revise", revision, "--rule", "Ensure `timeout` is not `None`.").stdout)
        named = json.loads(self.run_cli("lesson", "--revise", quoted["id"], "--rule",
                                        "Write the count the reader expects; test_count_matches guards it.").stdout)
        self.assertEqual(self.lesson_record(named["id"])["rule"],
                         "Write the count the reader expects; test_count_matches guards it.")

        stored = self.store / "lessons" / (named["id"] + ".json")
        stored.write_text(json.dumps(dict(json.loads(stored.read_text()), rule="test_count_matches must pass.")))
        retired = json.loads(self.run_cli("lesson", "--revise", named["id"], "--status", "retired").stdout)
        self.assertEqual(retired["status"], "retired")

    def test_lesson_normalized_scope_ignores_cache_and_case_only_repo_changes(self):
        for repos, expected in ((("Example", "EXAMPLE"), "repo:example"),
                                (("Example", "Other"), "repo:example")):
            with self.subTest(repos=repos):
                revision = self.author_lesson(repos=repos)
                record = self.lesson_record(revision)
                self.assertEqual(record["scope"], expected)
                self.assertEqual(record["status"], "proposed")
                self.assertEqual(record["verification"], "unproven")
                snapshot = self.store / "groups.json"
                snapshot.write_text('{"groups":[{"members":["private-cache-canary"]}]}')
                updated = json.loads(self.run_cli("lesson", "--revise", revision, "--rule", "Check explicit counts.").stdout)["id"]
                self.assertEqual(self.lesson_record(updated)["scope"], record["scope"])
                self.assertNotIn("private-cache-canary", (self.store / "lessons" / (updated + ".json")).read_text())

    def test_lesson_every_level_and_legacy_proposal_conversion(self):
        group, _ = self.recurring()
        proposal = self.proposal(group)
        original = (self.store / "proposals" / (proposal + ".json")).read_bytes()
        check = self.cwd / "check.py"
        check.write_text("print('not executed')")
        for level in ("architecture", "types", "lint", "test", "doc"):
            result = self.run_cli("lesson", "--from-proposal", proposal, "--level", level,
                                  "--check-file", str(check), "--check-command", "python3 {check}",
                                  "--expected-failure", "count-mismatch")
            record = self.lesson_record(json.loads(result.stdout)["id"])
            self.assertEqual((record["kind"], record["level"], record["scope"]), ("test", level, "repo:example"))
        self.assertEqual((self.store / "proposals" / (proposal + ".json")).read_bytes(), original)
        self.export(proposal)
        exported = (self.cwd / "rules/count-check.md").read_text()
        self.assertIn("Status: proposed\n\nVerification: unproven", exported)
        self.run_cli("export", "--proposal", proposal, "--format", "briefing", "--reviewed", "--output", "briefing.md", code=2)

    def test_replaying_same_proof_does_not_add_a_revision(self):
        revision = self.author_lesson()
        repo, before, after = self.history()
        first = json.loads(self.run_replay(revision, repo, before, after, "--status", "active").stdout)
        second = json.loads(self.run_replay(first["id"], repo, before, after).stdout)
        self.assertEqual(second, first)
        self.assertEqual(len(list((self.store / "lessons").glob("*.json"))), 2)
        self.assertEqual(len(list((self.store / "receipts").glob("*.json"))), 1)
        (self.store / "invalidations.json").write_text(json.dumps({first["lesson_id"]: first["id"]}))
        recovered = json.loads(self.run_replay(second["id"], repo, before, after).stdout)
        self.assertNotEqual(recovered["id"], first["id"])
        self.run_cli("export", "--lesson", recovered["id"], "--reviewed", "--output", "recovered.md")

    def test_replay_real_commits_proves_same_authored_check_and_preserves_source(self):
        self.cwd = self.cwd / "directory with spaces"
        self.cwd.mkdir()
        self.store = self.cwd / ".coldfix"
        revision = self.author_lesson()
        before_bytes = (self.store / "lessons" / (revision + ".json")).read_bytes()
        repo, before, after = self.history()
        source = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True).stdout
        result = self.run_replay(revision, repo, before, after, "--status", "active")
        receipt_ids = json.loads(result.stdout)
        self.assertEqual((receipt_ids["status"], receipt_ids["verification"]), ("PASS", "proven"))
        proven = self.lesson_record(receipt_ids["id"])
        receipt = json.loads((self.store / "receipts" / (proven["receipt"] + ".json")).read_text())
        self.assertEqual(proven["previous"], revision)
        self.assertEqual(proven["lesson_id"], revision)
        self.assertEqual(proven["status"], "active")
        self.assertEqual(receipt["lesson_revision"], proven["id"])
        self.assertEqual(receipt["before"]["commit"], before)
        self.assertEqual(receipt["after"]["commit"], after)
        self.assertEqual(receipt["before"]["status"], "FAIL")
        self.assertEqual(receipt["before"]["exit"], 1)
        self.assertEqual(receipt["after"]["status"], "PASS")
        self.assertEqual(receipt["after"]["exit"], 0)
        self.assertNotEqual(receipt["before"]["tree"], receipt["after"]["tree"])
        for label, commit in (("before", before), ("after", after)):
            tree = subprocess.run(["git", "-C", str(repo), "rev-parse", commit + "^{tree}"],
                                  check=True, capture_output=True, text=True).stdout.strip()
            self.assertEqual(receipt[label]["tree"], tree)
        self.assertEqual((self.store / "lessons" / (revision + ".json")).read_bytes(), before_bytes)
        self.assertEqual(subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True).stdout, source)
        self.assertEqual((repo / "count.txt").read_text(), "uncommitted")
        self.assertEqual((repo / "unfamiliar.txt").read_text(), "preserve this")
        self.assertNotIn(str(repo), result.stdout + result.stderr)
        self.assertEqual(list(self.store.glob(".replay-*")), [])
        self.assertEqual({p.stat().st_mode & 0o777 for p in self.store.rglob("*.json")}, {0o600})
        self.assertTrue(all((self.store / folder).stat().st_mode & 0o777 == 0o700 for folder in ("lessons", "receipts")))
        proven = dict(id=self.prove_elsewhere(proven["id"]))
        self.run_cli("export", "--lesson", proven["id"], "--reviewed", "--output", "proven.md")
        self.run_cli("export", "--format", "briefing", "--reviewed", "--output", "briefing.md")
        coldlocker = (self.cwd / "proven.md").read_text()
        briefing = (self.cwd / "briefing.md").read_text()
        self.assertTrue(coldlocker.startswith("<!-- coldlocker:lesson -->\n"))
        self.assertEqual(coldlocker.count("<!-- coldlocker:lesson -->"), 1)
        self.assertNotIn("<!-- coldlocker:lesson -->", briefing)
        self.assertIn("Status: active\n\nVerification: proven", coldlocker)
        self.assertIn("Require the expected count.", briefing)
        self.assertIn("<!-- lessons:begin -->", briefing)
        self.assertIn("<!-- lessons:end -->", briefing)
        for exported in (coldlocker, briefing):
            self.assertIn("coldfix:lesson:" + revision + ":" + proven["id"], exported)
            for private in ("private-evidence-canary", "thread:private-example", str(repo)):
                self.assertNotIn(private, exported)

    def test_replay_fail_before_and_unexpected_pass_do_not_prove(self):
        revision = self.author_lesson()
        repo, before, after = self.history()
        first = json.loads(self.run_replay(revision, repo, before, before, "--status", "active", code=1).stdout)
        self.assertEqual((first["status"], first["verification"]), ("FAIL", "fail_before"))
        self.assertEqual(self.lesson_record(first["id"])["status"], "proposed")
        second = json.loads(self.run_replay(first["id"], repo, after, after, code=1).stdout)
        self.assertEqual((second["status"], second["verification"]), ("FAIL", "unproven"))
        third = json.loads(self.run_replay(second["id"], repo, before, after).stdout)
        self.assertEqual(third["verification"], "proven")
        self.run_cli("export", "--lesson", third["id"], "--reviewed", "--output", "still-proposed.md", code=2)

    def test_exported_lesson_check_runs_without_private_store(self):
        revision = self.author_lesson()
        check = self.cwd / 'authored-check.py'
        check.write_bytes(check.read_bytes().replace(b'\n', b'\r\n'))
        revision = json.loads(self.run_cli('lesson', '--revise', revision, '--check-file', str(check),
            '--rule', 'Require the expected count.\r\nKeep ``` examples portable.').stdout)['id']
        self.assertNotIn('\r', self.lesson_record(revision)['check_script'])
        self.assertNotIn('\r', self.lesson_record(revision)['rule'])
        repo, before, after = self.history()
        proven = json.loads(self.run_replay(revision, repo, before, after, "--status", "active").stdout)["id"]
        self.run_cli("export", "--lesson", proven, "--reviewed", "--output", "runnable.md")
        exported = (self.cwd / "runnable.md").read_text()
        proof = re.search(r"^(`{3,})json\n(.*?)^\1$", exported, re.M | re.S)
        self.assertEqual(len(proof[1]), 4)
        self.assertIn('```', json.loads(proof[2])['rule'])
        command = re.search(r"```sh\n(.*?)\n```", exported, re.S)[1]
        outside = self.cwd / "consumer"
        outside.mkdir()
        for value, exit_code, expected in (("1", 1, {"status": "FAIL", "failure": "count-mismatch"}),
                                            ("2", 0, {"status": "PASS"})):
            (outside / "count.txt").write_text(value)
            result = subprocess.run(["sh", "-c", command], cwd=outside, capture_output=True, text=True)
            self.assertEqual(result.returncode, exit_code, result.stderr)
            self.assertEqual(json.loads(result.stdout), expected)

    def test_export_holds_lesson_text_that_breaks_the_coldlocker_rule_file_format(self):
        latest = self.author_lesson()
        repo, before, after = self.history()
        for rule in ("Require the expected count.\n## Verify\nSecond section.",
                     "Require the expected count.\nLesson: coldfix:lesson:forged",
                     "Require the expected count.\n- coldfix:failure:" + "0" * 32):
            with self.subTest(rule=rule):
                revision = json.loads(self.run_cli("lesson", "--revise", latest, "--rule", rule).stdout)["id"]
                latest = json.loads(self.run_replay(revision, repo, before, after, "--status", "active").stdout)["id"]
                result = self.run_cli("export", "--lesson", latest, "--reviewed", "--output", "format.md", code=2)
                self.assertIn("Coldlocker rule-file format", result.stderr)
                self.assertFalse((self.cwd / "format.md").exists())

    def test_exported_lesson_preserves_scope_without_private_repo_labels(self):
        repo, before, after = self.history()
        for repos, scope in ((('private-scope-one', 'private-scope-two'), 'repository-specific'),
                             (('private-scope-one', 'private-scope-two'), 'global')):
            with self.subTest(scope=scope):
                revision = self.author_lesson(repos=repos)
                proven = json.loads(self.run_replay(revision, repo, before, after, '--status', 'active').stdout)['id']
                if scope == 'global':
                    proven = self.prove_elsewhere(proven)
                output = scope + '.md'
                self.run_cli('export', '--lesson', proven, '--reviewed', '--output', output)
                exported = (self.cwd / output).read_text()
                self.assertIn('Scope: ' + scope + '\n', exported)
                for label in repos:
                    self.assertNotIn(label, exported)

    def test_replay_resolves_commits_retained_only_in_reflog(self):
        revision = self.author_lesson()
        repo, before, after = self.history()
        def git(*args):
            return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()
        root = git("commit-tree", after + "^{tree}", "-m", "Example replacement history")
        git("update-ref", "HEAD", root)
        self.assertEqual(git("branch", "--contains", before), "")
        self.assertEqual(git("cat-file", "-t", before), "commit")
        result = json.loads(self.run_replay(revision, repo, before, after).stdout)
        self.assertEqual(result["verification"], "proven")

    def test_export_serializes_with_retirement_and_reloads_latest_revision(self):
        revision = self.author_lesson()
        repo, before, after = self.history()
        proven = json.loads(self.run_replay(revision, repo, before, after, "--status", "active").stdout)["id"]
        spec = importlib.util.spec_from_file_location("coldfix", CLI)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        root, store = module.load_store(self.store)
        with (self.store / ".lessons.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            runner = ("import fcntl, runpy, sys\n"
                      "original = fcntl.flock\n"
                      "def notified(fd, operation):\n"
                      "    if operation == fcntl.LOCK_EX: print('waiting', flush=True)\n"
                      "    return original(fd, operation)\n"
                      "fcntl.flock = notified\n"
                      "sys.argv = sys.argv[1:]\n"
                      "runpy.run_path(sys.argv[0], run_name='__main__')\n")
            command = [sys.executable, "-c", runner, str(CLI), "--store", str(self.store), "export", "--format", "briefing",
                       "--reviewed", "--output", str(self.cwd / "racing.md")]
            with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as child:
                try:
                    self.assertEqual(child.stdout.readline(), "waiting\n")
                    args = module.parser().parse_args(["lesson", "--revise", proven, "--status", "retired"])
                    module.lesson(args, root, store)
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)
                stdout, stderr = child.communicate(timeout=10)
                self.assertEqual(child.returncode, 0, stderr)
                self.assertEqual(json.loads(stdout)["lesson_count"], 0)
        self.assertNotIn("coldfix:lesson:", (self.cwd / "racing.md").read_text())

    def test_replay_errors_missing_history_and_wrong_failure_are_incomplete(self):
        repo, before, after = self.history()
        scripts = ["raise RuntimeError('private-runtime-canary')\n",
                   "import sys\nprint('{\"status\":\"FAIL\",\"failure\":\"wrong\"}')\nsys.exit(1)\n",
                   "print('{\"status\":\"PASS\"}')\nraise SystemExit(1)\n",
                   "print('{\"status\":\"FAIL\",\"failure\":\"count-mismatch\"}')\nraise SystemExit(2)\n",
                   "print('not JSON')\n",
                   "print('{\"status\":\"PASS\",\"status\":\"PASS\"}')\n"]
        for script in scripts:
            with self.subTest(script=script):
                revision = self.author_lesson(script)
                result = self.run_replay(revision, repo, before, after, "--status", "active", code=2)
                value = json.loads(result.stdout)
                self.assertEqual((value["status"], value["verification"]), ("INCOMPLETE", "unproven"))
                self.assertNotIn("private-runtime-canary", result.stdout + result.stderr)
                self.assertEqual(self.lesson_record(value["id"])["status"], "proposed")
                self.run_cli("export", "--lesson", value["id"], "--reviewed", "--output", "refused.md", code=2)
        revision = self.author_lesson()
        missing_tool = json.loads(self.run_cli("lesson", "--revise", revision, "--check-command", "coldfix_missing_tool {check}").stdout)["id"]
        missing_tool = json.loads(self.run_replay(missing_tool, repo, before, after, code=2).stdout)
        self.assertEqual(missing_tool["status"], "INCOMPLETE")
        receipt = json.loads((self.store / "receipts" / (missing_tool["receipt_id"] + ".json")).read_text())
        self.assertEqual(receipt["before"]["exit"], 127)
        revision = self.author_lesson()
        result = json.loads(self.run_replay(revision, repo, "private-missing-revision", after, code=2).stdout)
        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertEqual(json.loads(self.run_cli("lesson").stdout)["lessons"][-1]["status"], "proposed")

    def test_incomplete_replay_reason_names_side_exit_and_check_contract(self):
        repo, before, after = self.history()
        plain_text = ("import sys\nfrom pathlib import Path\nok = Path('count.txt').read_text() == '2'\n"
                      "print('count ok' if ok else 'count wrong private-output-canary')\nsys.exit(0 if ok else 1)\n")
        result = self.run_replay(self.author_lesson(plain_text), repo, before, after, code=2)
        value = json.loads(result.stdout)
        self.assertEqual(value["status"], "INCOMPLETE")
        self.assertIn("before check exited 1 with stdout that is not one JSON object", value["reason"])
        self.assertIn("after check exited 0 with stdout that is not one JSON object", value["reason"])
        for shape in ('{"status":"PASS"} with exit 0', '{"status":"FAIL","failure":"count-mismatch"} with exit 1',
                      '{"status":"INCOMPLETE"} with exit 2'):
            self.assertIn(shape, value["reason"])
        self.assertNotIn("private-output-canary", result.stdout + result.stderr)
        after_only = ("import json, sys\nfrom pathlib import Path\nok = Path('count.txt').read_text() == '2'\n"
                      "print('done' if ok else json.dumps({'status': 'FAIL', 'failure': 'count-mismatch'}))\n"
                      "sys.exit(0 if ok else 1)\n")
        reason = json.loads(self.run_replay(self.author_lesson(after_only), repo, before, after, code=2).stdout)["reason"]
        self.assertTrue(reason.startswith("after check exited 0 with stdout that is not one JSON object"), reason)
        self.assertNotIn("before check", reason)
        wrong_failure = "import json\nprint(json.dumps({'status': 'FAIL', 'failure': 'private-id-canary'}))\nraise SystemExit(1)\n"
        reason = json.loads(self.run_replay(self.author_lesson(wrong_failure), repo, before, after, code=2).stdout)["reason"]
        self.assertIn("before check exited 1 with a failure other than 'count-mismatch'", reason)
        self.assertNotIn("private-id-canary", reason)
        missing = json.loads(self.run_replay(self.author_lesson(), repo, "private-missing-revision", after, code=2).stdout)
        self.assertTrue(missing["reason"].startswith("before revision could not be checked out"), missing["reason"])
        proven = json.loads(self.run_replay(self.author_lesson(), repo, before, after).stdout)
        self.assertNotIn("reason", proven)

    def test_replay_and_lesson_help_state_the_check_contract(self):
        for action in ("replay", "lesson"):
            with self.subTest(action=action):
                text = " ".join(self.run_cli(action, "--help").stdout.split())
                for shape in ('{"status":"PASS"} with exit 0', '{"status":"FAIL","failure":"<expected_failure>"} with exit 1',
                              '{"status":"INCOMPLETE"} with exit 2'):
                    self.assertIn(shape, text)


if __name__ == "__main__":
    unittest.main()
