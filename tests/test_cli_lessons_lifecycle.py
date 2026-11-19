import concurrent.futures
import importlib.util
import json
import subprocess
import sys
import unittest
from unittest.mock import patch

from test_cli import CLI, CliCase


class CliLessonLifecycleTests(CliCase):
    def test_changed_lessons_reset_proof_retirement_and_stale_ids_cannot_resurrect(self):
        revision = self.author_lesson()
        repo, before, after = self.history()
        proven = json.loads(self.run_replay(revision, repo, before, after, "--status", "active").stdout)["id"]
        for field, value in (("--rule", "Updated count rule."), ("--title", "Updated title"),
                             ("--level", "lint"), ("--check-command", "python3 -u {check}")):
            with self.subTest(field=field):
                updated = json.loads(self.run_cli("lesson", "--revise", proven, field, value).stdout)["id"]
                record = self.lesson_record(updated)
                self.assertEqual((record["status"], record["verification"], record["receipt"]), ("proposed", "unproven", None))
                self.run_cli("export", "--lesson", proven, "--reviewed", "--output", "old.md", code=2)
                self.run_cli("lesson", "--revise", proven, "--rule", "Branch attempt", code=2)
                self.run_replay(proven, repo, before, after, code=2)
                proven = json.loads(self.run_replay(updated, repo, before, after, "--status", "active").stdout)["id"]
        self.run_cli("lesson", "--revise", proven, "--status", "conditional", code=2)
        narrowed = json.loads(self.run_cli("lesson", "--revise", proven, "--rule", "Count rule for one store.").stdout)["id"]
        proven = json.loads(self.run_replay(narrowed, repo, before, after, "--status", "conditional").stdout)["id"]
        self.assertEqual(tuple(self.lesson_record(proven)[key] for key in ("status", "verification")),
                         ("conditional", "proven"))
        self.run_cli("export", "--lesson", proven, "--reviewed", "--output", "conditional.md")
        self.assertIn("Status: conditional", (self.cwd / "conditional.md").read_text())
        retired = json.loads(self.run_cli("lesson", "--revise", proven, "--status", "retired").stdout)["id"]
        self.assertEqual(self.lesson_record(retired)["status"], "retired")
        self.run_cli("export", "--format", "briefing", "--reviewed", "--output", "retired.md")
        self.assertNotIn("Updated title", (self.cwd / "retired.md").read_text())
        self.assertNotIn("coldfix:lesson:", (self.cwd / "retired.md").read_text())
        replayed = json.loads(self.run_replay(retired, repo, before, after).stdout)["id"]
        self.assertEqual(self.lesson_record(replayed)["status"], "retired")
        self.run_cli("export", "--lesson", replayed, "--reviewed", "--output", "retired-rule.md", code=2)

    def test_briefing_filters_repo_scope_enforced_and_unproven_without_approval(self):
        repo, before, after = self.history()
        local = self.author_lesson(repos=("example", "EXAMPLE"))
        local = json.loads(self.run_replay(local, repo, before, after, "--status", "active").stdout)["id"]
        enforced = self.author_lesson()
        enforced = json.loads(self.run_replay(enforced, repo, before, after, "--status", "enforced").stdout)["id"]
        self.author_lesson()
        self.run_cli("export", "--lesson", enforced, "--reviewed", "--output", "enforced.md")
        self.run_cli("export", "--format", "briefing", "--output", "without-approval.md")
        self.run_cli("export", "--lesson", local, "--output", "local.md")
        self.run_cli("export", "--format", "briefing", "--reviewed", "--output", "briefing.md")
        self.assertNotIn("coldfix:lesson:", (self.cwd / "briefing.md").read_text())

    def test_briefing_skips_incomplete_and_invalidated_lessons(self):
        repo, before, after = self.history()
        refs = []
        for summary in ('', 'Complete failure.', 'Invalidated failure.'):
            revision = self.author_lesson()
            revision = json.loads(self.run_cli('lesson', '--revise', revision, '--failure', summary).stdout)['id']
            proven = json.loads(self.run_replay(revision, repo, before, after, '--status', 'active').stdout)['id']
            refs.append(self.prove_elsewhere(proven))
        invalid = self.lesson_record(refs[2])
        (self.store / 'invalidations.json').write_text(json.dumps({invalid['lesson_id']: refs[2]}))
        self.run_cli('export', '--format', 'briefing', '--output', 'complete-only.md')
        body = (self.cwd / 'complete-only.md').read_text()
        self.assertIn(refs[1], body)
        self.assertNotIn(refs[0], body)
        self.assertNotIn(refs[2], body)

    def test_briefing_screens_embedded_captured_credential_in_failure_summary(self):
        revision = self.author_lesson()
        self.capture(error='token=private-captured-word')
        revised = json.loads(self.run_cli('lesson', '--revise', revision,
            '--failure', 'prefixprivate-captured-wordsuffix').stdout)['id']
        repo, before, after = self.history()
        proven = json.loads(self.run_replay(revised, repo, before, after, '--status', 'active').stdout)['id']
        self.prove_elsewhere(proven)
        result = self.run_cli('export', '--format', 'briefing', '--output', 'briefing.md', code=2)
        self.assertIn('captured_credential', result.stderr)
        self.assertFalse((self.cwd / 'briefing.md').exists())

    def export_briefing_after_capture(self, error, failure, code):
        revision = self.author_lesson()
        self.capture(error=error)
        revised = json.loads(self.run_cli('lesson', '--revise', revision, '--failure', failure).stdout)['id']
        repo, before, after = self.history()
        proven = json.loads(self.run_replay(revised, repo, before, after, '--status', 'active').stdout)['id']
        self.prove_elsewhere(proven)
        return self.run_cli('export', '--format', 'briefing', '--output', 'briefing.md', code=code)

    def test_briefing_refuses_captured_credential_wherever_it_appears(self):
        for error, value in (('token=Wq7rT2pZx9', 'Wq7rT2pZx9'), ('password: Ab9z', 'Ab9z'), ('password=Ab9\\', 'Ab9\\')):
            for failure in (value, 'prefix' + value + 'suffix', 'https://example.com/cb?state=' + value + '&page=2'):
                with self.subTest(failure=failure):
                    self.setUp()
                    result = self.export_briefing_after_capture(error, failure, 2)
                    self.assertIn('captured_credential', result.stderr)
                    self.assertNotIn(value, result.stdout + result.stderr)
                    self.assertFalse((self.cwd / 'briefing.md').exists())

    def test_briefing_refuses_credential_captured_behind_an_escaped_quote(self):
        for value in ('Wq7rT2pZx9', 'abc def', "O'Reilly"):
            with self.subTest(value=value):
                self.setUp()
                result = self.export_briefing_after_capture('{"cmd": "login password=\\"' + value + '\\""}',
                                                            'Login with ' + value + ' failed.', 2)
                self.assertIn('captured_credential', result.stderr)
                self.assertFalse((self.cwd / 'briefing.md').exists())

    def test_briefing_refuses_value_after_every_credential_keyword(self):
        value = 'Wq7rT2pZx9'
        for shape in ('bearer {}', 'authorization: {}', 'token={}', 'password={}', 'password="{}"',
                      "password: '{}' was rejected", 'password={} was rejected', 'https://example.com/cb?token={}',
                      '{{"cmd": "login password=\\"{}\\""}}', '--token {} for the client'):
            error = shape.format(value)
            with self.subTest(error=error):
                self.setUp()
                result = self.export_briefing_after_capture(error, 'Login with ' + value + ' failed.', 2)
                self.assertIn('captured_credential', result.stderr)
                self.assertFalse((self.cwd / 'briefing.md').exists())

    def test_briefing_refuses_plain_word_value_that_ends_its_clause(self):
        for error in ('password=letmein', 'password: letmein, then retry', 'token: letmein, then retry', 'token=1234',
                      'password=letmein user=guest', 'password=letmein user-name=guest', 'password=letmein failed@localhost',
                      'password=letmein user.name', 'password=letmein foo/bar', 'password=letmein user\\"x',
                      'token=letmein user=guest', 'token=letmein user-name=guest', 'token=letmein failed@localhost',
                      'token=letmein user.name', 'token=letmein foo/bar', 'token=letmein user\\"x'):
            with self.subTest(error=error):
                self.setUp()
                result = self.export_briefing_after_capture(error, 'Use letmein or 1234 to log in.', 2)
                self.assertIn('captured_credential', result.stderr)
                self.assertFalse((self.cwd / 'briefing.md').exists())

    def test_briefing_refuses_password_like_value_whatever_follows_it(self):
        for error in ('password=letmein was rejected', 'secret: letmein for now', '--password 1234 by default',
                      'credentials: letmein were sent', 'password_token: letmein in the log'):
            with self.subTest(error=error):
                self.setUp()
                result = self.export_briefing_after_capture(error, 'Use letmein or 1234 to log in.', 2)
                self.assertIn('captured_credential', result.stderr)
                self.assertFalse((self.cwd / 'briefing.md').exists())

    def test_briefing_refuses_token_after_authorization_bearer(self):
        result = self.export_briefing_after_capture('Authorization: Bearer Wq7rT2pZx9', 'Login with Wq7rT2pZx9 failed.', 2)
        self.assertIn('captured_credential', result.stderr)
        self.assertFalse((self.cwd / 'briefing.md').exists())

    def test_briefing_refuses_bearer_as_the_value_of_another_keyword(self):
        for error in ('password=Bearer abc123', '--token Bearer abc123'):
            with self.subTest(error=error):
                self.setUp()
                result = self.export_briefing_after_capture(error, 'Send Bearer as the header.', 2)
                self.assertIn('captured_credential', result.stderr)
                self.assertFalse((self.cwd / 'briefing.md').exists())

    def test_briefing_ignores_prose_words_after_credential_keywords(self):
        for error in ('the bearer and the client', 'authorization: may be required',
                      'Authorization: Bearer and the client', 'the env-bearer and routine-id fix',
                      '{"note": "the bearer of news\\", then"}'):
            with self.subTest(error=error):
                self.setUp()
                self.export_briefing_after_capture(error, 'Check the client and the server; it may be 0 by default.', 0)
                self.assertIn('may be 0 by default', (self.cwd / 'briefing.md').read_text())

    def test_briefing_refuses_short_captured_value_standing_alone(self):
        for failure in ('abc', 'Use abc for login.', 'https://example.com/cb?state=abc&page=2'):
            with self.subTest(failure=failure):
                self.setUp()
                result = self.export_briefing_after_capture('password=abc', failure, 2)
                self.assertIn('captured_credential', result.stderr)
                self.assertFalse((self.cwd / 'briefing.md').exists())

    def test_briefing_ignores_short_captured_values_inside_ordinary_words(self):
        for error in ('max_tokens=150 reached', 'password=abc', '**Tokens:** 1500 in'):
            with self.subTest(error=error):
                self.setUp()
                self.export_briefing_after_capture(error, 'Reset fabcde after 1500 rows.', 0)
                self.assertIn('fabcde', (self.cwd / 'briefing.md').read_text())

    def test_incomplete_summaries_and_legacy_lessons_stay_undeliverable(self):
        repo, before, after = self.history()
        for field in ('failure', 'fix'):
            with self.subTest(field=field):
                revision = self.author_lesson()
                revision = json.loads(self.run_cli('lesson', '--revise', revision,
                    '--' + field, '').stdout)['id']
                proven = json.loads(self.run_replay(revision, repo, before, after, '--status', 'active').stdout)['id']
                output = self.cwd / (field + '.md')
                result = self.run_cli('export', '--lesson', proven, '--output', str(output), code=2)
                self.assertIn('retained pending', result.stderr)
                self.assertFalse(output.exists())
                self.assertEqual(self.lesson_record(proven)['verification'], 'proven')
        revision = self.author_lesson()
        path = self.store / 'lessons' / (revision + '.json')
        legacy = json.loads(path.read_text())
        legacy.update(version=2)
        del legacy['failure']
        del legacy['fix']
        path.write_text(json.dumps(legacy))
        proven = json.loads(self.run_replay(revision, repo, before, after, '--status', 'active').stdout)['id']
        self.run_cli('export', '--lesson', proven, '--output', 'legacy.md', code=2)
        self.assertFalse((self.cwd / 'legacy.md').exists())
        upgraded = json.loads(self.run_cli('lesson', '--revise', proven,
            '--failure', 'The count was not validated.', '--fix', 'Validate the expected count.').stdout)['id']
        upgraded = json.loads(self.run_replay(upgraded, repo, before, after, '--status', 'active').stdout)['id']
        self.assertEqual(self.lesson_record(upgraded)['version'], 3)
        self.run_cli('export', '--lesson', upgraded, '--output', 'upgraded.md')
        self.assertIn('## Lesson record', (self.cwd / 'upgraded.md').read_text())

    def test_tampered_proof_binding_or_check_content_blocks_exports(self):
        revision = self.author_lesson()
        repo, before, after = self.history()
        proven = json.loads(self.run_replay(revision, repo, before, after, "--status", "active").stdout)["id"]
        path = self.store / "lessons" / (proven + ".json")
        original = path.read_bytes()
        record = json.loads(original)
        for changes in (dict(rule="Changed"), dict(check_script="print('changed')"), dict(status="enforced"),
                        dict(receipt=None), dict(scope="repo:example-one"), dict(previous="0" * 32)):
            with self.subTest(changes=changes):
                path.write_text(json.dumps(dict(record, **changes)))
                self.run_cli("export", "--lesson", proven, "--reviewed", "--output", "tampered.md", code=2)
                self.assertFalse((self.cwd / "tampered.md").exists())
        path.write_bytes(original)
        receipt_path = self.store / "receipts" / (record["receipt"] + ".json")
        original_receipt = receipt_path.read_bytes()
        receipt = json.loads(original_receipt)
        for changes in (dict(check_sha256="0" * 64), dict(lesson_revision=revision),
                        dict(before=dict(receipt["before"], stdout="not a check failure")),
                        dict(after=dict(receipt["after"], exit=2)), dict(after=dict(receipt["after"], tree="0" * 40)), dict(after=dict(receipt["after"], status="INCOMPLETE"))):
            with self.subTest(changes=changes):
                receipt_path.write_text(json.dumps(dict(receipt, **changes)))
                self.run_cli("export", "--lesson", proven, "--reviewed", "--output", "tampered.md", code=2)
        receipt_path.write_bytes(original_receipt)
        self.run_cli("export", "--lesson", proven, "--reviewed", "--output", "untampered.md")

    def test_authoring_does_not_execute_and_replay_rejects_changed_check_between_trees(self):
        script = ("import json, sys\nfrom pathlib import Path\n"
                  "Path(__file__).write_text(\"print('changed')\")\n"
                  "print(json.dumps({'status':'FAIL','failure':'count-mismatch'}))\nsys.exit(1)\n")
        revision = self.author_lesson(script)
        self.assertNotIn(".replay-", " ".join(str(path) for path in self.store.iterdir()))
        repo, before, after = self.history()
        result = json.loads(self.run_replay(revision, repo, before, after, code=2).stdout)
        self.assertEqual((result["status"], result["verification"]), ("INCOMPLETE", "unproven"))
        self.run_cli("lesson", "--revise", result["id"], "--status", "active", code=2)
        check = self.cwd / "authored-check.py"
        check.write_text("print('new check')")
        updated = json.loads(self.run_cli("lesson", "--revise", result["id"], "--check-file", str(check)).stdout)["id"]
        self.assertEqual(self.lesson_record(updated)["verification"], "unproven")

    def test_revisions_refresh_evidence_scope_and_concurrent_revisions_have_one_successor(self):
        revision = self.author_lesson(repos=("Example", "EXAMPLE"))
        self.run_cli("ingest", stdin=json.dumps(self.event(repo="other", error="count mismatch private-evidence-canary")))
        def revise(title):
            return subprocess.run([sys.executable, str(CLI), "lesson", "--revise", revision, "--title", title],
                                  cwd=self.cwd, capture_output=True, text=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(revise, ("First title", "Second title")))
        self.assertEqual(sorted(result.returncode for result in results), [0, 2])
        winner, = [json.loads(result.stdout)["id"] for result in results if result.returncode == 0]
        record = self.lesson_record(winner)
        self.assertEqual(record["scope"], "repo:example")
        self.assertEqual(len(record["evidence"]), 3)
        latest, = json.loads(self.run_cli("lesson").stdout)["lessons"]
        self.assertEqual(latest["id"], winner)
        self.assertEqual(len(list((self.store / "lessons").glob("*.json"))), 2)

    def test_shallow_or_changed_expected_failure_proof_never_counts_toward_global_scope(self):
        script = ("import json, sys\nfrom pathlib import Path\n"
                  "name = Path('failure-name').read_text() if Path('failure-name').exists() else 'count-mismatch'\n"
                  "ok = Path('count.txt').read_text() == '2'\n"
                  "print(json.dumps({'status': 'PASS'} if ok else {'status': 'FAIL', 'failure': name}))\n"
                  "sys.exit(0 if ok else 1)\n")
        revision = self.author_lesson(script)
        first, before, after = self.history("first")
        one = json.loads(self.run_replay(revision, first, before, after, "--status", "active").stdout)
        self.assertEqual(one["verification"], "proven")
        shallow = self.cwd / "shallow"
        subprocess.run(["git", "clone", "--quiet", "--depth", "1", "file://" + str(first), str(shallow)], check=True)
        subprocess.run(["git", "-C", str(shallow), "fetch", "--quiet", "--depth", "1", "origin", before], check=True)
        shallow_proof = json.loads(self.run_replay(one["id"], shallow, before, after).stdout)
        self.assertEqual(shallow_proof["verification"], "proven")
        self.assertNotEqual(self.lesson_record(shallow_proof["id"])["scope"], "global")
        changed = json.loads(self.run_cli("lesson", "--revise", shallow_proof["id"], "--expected-failure", "other").stdout)["id"]
        second, _, _ = self.history("second")
        def git(*args):
            return subprocess.run(["git", "-C", str(second), *args], capture_output=True, text=True, check=True).stdout.strip()
        (second / "count.txt").write_text("1")
        (second / "failure-name").write_text("other")
        git("add", ".")
        git("commit", "--quiet", "-m", "Other failure")
        other_before = git("rev-parse", "HEAD")
        (second / "count.txt").write_text("2")
        git("commit", "--quiet", "-am", "Other fix")
        reproven = json.loads(self.run_replay(changed, second, other_before, git("rev-parse", "HEAD")).stdout)
        self.assertEqual(reproven["verification"], "proven")
        self.assertNotEqual(self.lesson_record(reproven["id"])["scope"], "global")

    def test_proof_against_an_unrelated_failing_history_has_no_repository_identity(self):
        revision = self.author_lesson()
        first, _, first_after = self.history("first")
        other, other_before, _ = self.history("other")
        subprocess.run(["git", "-C", str(first), "fetch", "--quiet", str(other), other_before], check=True)
        proven = json.loads(self.run_replay(revision, first, other_before, first_after).stdout)
        self.assertEqual(proven["verification"], "proven")
        receipt = json.loads((self.store / "receipts" / (proven["receipt_id"] + ".json")).read_text())
        self.assertNotIn("repo_root", receipt)
        second, before, after = self.history("second")
        again = json.loads(self.run_replay(proven["id"], second, before, after).stdout)
        self.assertNotEqual(self.lesson_record(again["id"])["scope"], "global")

    def test_store_with_legacy_global_scope_still_opens_and_revises(self):
        revision = self.author_lesson()
        path = self.store / "lessons" / (revision + ".json")
        record = json.loads(path.read_text())
        path.write_text(json.dumps(dict(record, scope="global")))
        self.run_cli("lesson")
        repo, before, after = self.history()
        proven = json.loads(self.run_replay(revision, repo, before, after).stdout)
        self.assertEqual(proven["verification"], "proven")
        self.assertTrue(self.lesson_record(proven["id"])["scope"].startswith("repo:"))
        self.assertNotEqual(self.lesson_record(proven["id"])["scope"], "global")

    def test_replay_same_tree_cannot_prove_and_timeout_kills_check_process(self):
        script = ("import json, sys\nfrom pathlib import Path\n"
                  "flag = Path(__file__).with_suffix('.flag')\nok = flag.exists()\nflag.touch()\n"
                  "print(json.dumps({'status':'PASS'} if ok else {'status':'FAIL','failure':'count-mismatch'}))\n"
                  "sys.exit(0 if ok else 1)\n")
        revision = self.author_lesson(script)
        repo, before, after = self.history()
        result = json.loads(self.run_replay(revision, repo, before, before, code=1).stdout)
        self.assertEqual((result["status"], result["verification"]), ("FAIL", "fail_before"))
        hanging = self.author_lesson("import time\ntime.sleep(60)\n")
        timed = json.loads(self.run_replay(hanging, repo, before, after, "--timeout", "0.05", code=2).stdout)
        self.assertEqual((timed["status"], timed["verification"]), ("INCOMPLETE", "unproven"))
        receipt = json.loads((self.store / "receipts" / (timed["receipt_id"] + ".json")).read_text())
        self.assertIn("Check timed out", receipt["before"]["stderr"])
        self.assertIn("Check timed out", receipt["after"]["stderr"])
        for timeout in ("nan", "inf", "0", "-1"):
            self.run_replay(timed["id"], repo, before, after, "--timeout", timeout, code=2)

    def test_interrupted_lesson_publication_leaves_old_tip_and_safe_orphan_receipt(self):
        revision = self.author_lesson()
        repo, before, after = self.history()
        spec = importlib.util.spec_from_file_location("coldfix", CLI)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        args = module.parser().parse_args(["--store", str(self.store), "replay", "--lesson", revision,
                                           "--repo", str(repo), "--before", before, "--after", after, "--status", "active"])
        root, store = module.load_store(self.store)
        save = module.save
        def interrupted(root, collection, record):
            if collection == "lessons":
                raise OSError("publication interrupted")
            return save(root, collection, record)
        with patch.object(module, "save", side_effect=interrupted):
            with self.assertRaises(OSError):
                module.replay(args, root, store)
        self.assertEqual(len(list((self.store / "receipts").glob("*.json"))), 1)
        latest, = json.loads(self.run_cli("lesson").stdout)["lessons"]
        self.assertEqual((latest["id"], latest["verification"]), (revision, "unproven"))
        proven = json.loads(self.run_replay(revision, repo, before, after, "--status", "active").stdout)
        self.assertEqual(proven["verification"], "proven")


if __name__ == "__main__":
    unittest.main()
