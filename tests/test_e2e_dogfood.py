"""Exercise the lesson pipeline using only a temporary store and Git repository."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from checks import dogfood_store
import coldrecall

import test_cli


RECALL = test_cli.CLI.parent / "coldrecall.py"
RULE = "Require the expected count."


class DogfoodTests(unittest.TestCase):
    setUp = test_cli.CliTests.setUp
    run_cli = test_cli.CliTests.run_cli
    event = test_cli.CliTests.event
    run_replay = test_cli.CliTests.run_replay

    def recall(self, *args, stdin=None, cwd=None):
        result = subprocess.run([sys.executable, str(RECALL), *args], cwd=cwd or self.cwd,
                                input=stdin, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def prepare_lesson(self, rule=RULE, facts=None):
        events = [self.event(error="count mismatch at line 10", source_ref="first"),
                  self.event(error="count mismatch at line 20", source_ref="second")]
        batch = "\n".join(json.dumps(event) for event in events)
        imported = json.loads(self.run_cli("ingest", stdin=batch).stdout)
        self.assertEqual(imported["imported_records"], 2)
        duplicate = json.loads(self.run_cli("ingest", stdin=batch).stdout)
        self.assertEqual((duplicate["imported_records"], duplicate["skipped_duplicates"]), (0, 2))
        self.assertEqual(json.loads(self.run_cli("group").stdout), {"groups": 1, "members": 2})
        group, = json.loads((self.store / "groups.json").read_text())["groups"]
        self.assertEqual(sorted(group["members"]), sorted(imported["failure_ids"]))

        repo = self.cwd / "repo"
        repo.mkdir()
        def git(*args):
            return subprocess.run(["git", "-C", str(repo), *args], text=True,
                                  capture_output=True, check=True).stdout.strip()
        git("init", "-q")
        git("config", "user.name", "Fixture")
        git("config", "user.email", "fixture@example.invalid")
        (repo / "count.txt").write_text("1")
        git("add", "count.txt")
        git("commit", "-qm", "before")
        before = git("rev-parse", "HEAD")
        (repo / "count.txt").write_text("2")
        git("commit", "-qam", "after")
        after = git("rev-parse", "HEAD")
        check = self.cwd / "check.py"
        check.write_text("import json, sys\nfrom pathlib import Path\n"
                         "ok = Path('count.txt').read_text() == '2'\n"
                         "print(json.dumps({'status': 'PASS'} if ok else "
                         "{'status': 'FAIL', 'failure': 'count-mismatch'}))\n"
                         "sys.exit(0 if ok else 1)\n")
        fact_args = []
        if facts is not None:
            fact_file = self.cwd / "facts.json"
            fact_file.write_text(json.dumps(facts))
            fact_args = ["--facts-file", str(fact_file)]
        authored = json.loads(self.run_cli("lesson", "--group", group["signature"],
            "--kind", "test", "--level", "test", "--title", "Verify the count",
            "--rule", rule, "--failure", "The count was wrong.", "--fix", "Set the expected count.",
            "--check-command", "python3 {check}", "--check-file", str(check),
            "--expected-failure", "count-mismatch", *fact_args).stdout)
        proven = json.loads(self.run_replay(authored["id"], repo, before, after, "--status", "active").stdout)
        lesson = json.loads((self.store / "lessons" / (proven["id"] + ".json")).read_text())
        self.assertEqual(lesson["verification"], "proven")
        self.assertEqual(set(lesson["evidence"]), set(imported["failure_ids"]))
        receipt = json.loads((self.store / "receipts" / (lesson["receipt"] + ".json")).read_text())
        self.assertEqual((receipt["before"]["status"], receipt["after"]["status"]), ("FAIL", "PASS"))

        rules = repo / ".coldlocker" / "rules"
        rules.mkdir(parents=True)
        name = "lesson-" + lesson["lesson_id"]
        self.run_cli("export", "--lesson", proven["id"], "--reviewed", "--output", str(rules / (name + ".md")))
        self.assertIn(RULE, (rules / (name + ".md")).read_text())
        path = rules / (name + ".md")
        body = path.read_text()
        record = coldrecall.FENCE.search(coldrecall.sections(body)["Lesson record"])
        proof = json.loads(record.group(2))
        proof["gates"]["release_check"] = "PASS"
        path.write_text(body.replace(record.group(2), json.dumps(proof, indent=2)))
        digest = hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        (rules.parent / "rules.json").write_text(json.dumps({name: {"hashes": [digest], "lesson": True, "pending": False}}))
        return repo

    def test_ingest_replay_export_and_recall(self):
        repo = self.prepare_lesson()
        compiled = json.loads(self.recall("compile", "--repo", str(repo)).stdout)
        self.assertEqual(len(compiled["active"]), 1)
        self.assertEqual(json.loads(self.recall("compile", "--repo", str(repo)).stdout)["epoch"], compiled["epoch"])
        self.assertEqual(self.recall("lookup", "--repo", str(repo), "--path", "count.txt").stdout.count(RULE), 1)
        payload = json.dumps({"cwd": str(repo), "tool_input": {"file_path": str(repo / "count.txt")}})
        hook = json.loads(self.recall("hook", stdin=payload).stdout)["hookSpecificOutput"]
        self.assertEqual(hook["hookEventName"], "PreToolUse")
        self.assertEqual(hook["additionalContext"].count(RULE), 1)
        once_payload = json.dumps({"cwd": str(repo), "session_id": "dogfood",
                                   "tool_input": {"file_path": str(repo / "count.txt")}})
        self.assertIn(RULE, self.recall("hook", "--once", stdin=once_payload).stdout)
        self.assertEqual(self.recall("hook", "--once", stdin=once_payload).stdout, "")
        self.assertEqual(self.recall("hook", stdin="not json", cwd=repo).stdout, "")

    def test_store_dogfood_rejects_dirty_explicit_source_checkout(self):
        source = self.cwd / "source"
        source.mkdir()

        def git(*args):
            return subprocess.run(["git", "-C", str(source), *args], text=True,
                                  capture_output=True, check=True).stdout.strip()

        git("init", "-q")
        git("config", "user.name", "Fixture")
        git("config", "user.email", "fixture@example.invalid")
        (source / "coldfix.py").write_text("# committed synthetic checker\n")
        (source / "coldrecall.py").write_text("# committed synthetic recall\n")
        git("add", "coldfix.py", "coldrecall.py")
        git("commit", "-qm", "synthetic clean source")
        (source / "coldfix.py").write_text("# dirty synthetic checker\n")
        dirty_before = git("status", "--porcelain=v1", "--untracked-files=all")

        self.store.mkdir()
        before = dogfood_store.snapshot(self.store)
        result = subprocess.run([sys.executable, "-B", str(Path(dogfood_store.__file__)),
                                 "--repo", str(source), "--store", str(self.store)],
                                cwd=self.cwd, text=True, capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.strip(),
                         "INCOMPLETE: source checkout has uncommitted changes")
        self.assertEqual(dogfood_store.snapshot(self.store), before)
        self.assertEqual(git("status", "--porcelain=v1", "--untracked-files=all"), dirty_before)

    def test_store_dogfood_accepts_atomic_facts(self):
        facts = [{"id": "count", "text": RULE}, {"id": "text", "text": "Preserve count text."}]
        repo = self.prepare_lesson(rule=RULE + " Preserve count text.", facts=facts)
        path, = (repo / ".coldlocker/rules").glob("*.md")
        proof, _, _ = coldrecall.read_rule(path)
        self.assertEqual(proof["facts"], facts)
        exported = self.cwd / "redelivered.md"
        self.run_cli("export", "--lesson", proof["lesson"].split(":")[-1], "--output", str(exported))
        redelivered = exported.read_text()
        exported_record = coldrecall.FENCE.search(coldrecall.sections(redelivered)["Lesson record"])
        redelivered_proof = json.loads(exported_record.group(2))
        redelivered_proof["gates"]["release_check"] = "PASS"
        self.assertEqual(redelivered.replace(exported_record.group(2), json.dumps(redelivered_proof, indent=2)),
                         path.read_text())
        retained = [json.loads(path.read_text()) for path in (self.store / "lessons").glob("*.json")]
        self.assertTrue(all(row["facts"] == facts for row in retained))
        for source in test_cli.CLI.parent.glob("*.py"):
            shutil.copy2(source, repo / source.name)
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "Atomic lesson"], check=True, capture_output=True)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(dogfood_store, "ROOT", repo), patch.object(
                sys, "argv", ["dogfood_store.py", "--store", str(self.store)]):
            dogfood_store.main()
        self.assertIn('"witnesses": ["pass", "pass"]', output.getvalue())
        revision = proof["lesson"].split(":")[-1]
        bad = self.cwd / "bad-facts.json"
        for malformed in ("{not-json", "[" * 2000):
            bad.write_text(malformed)
            rejected = self.run_cli("lesson", "--revise", revision, "--facts-file", str(bad), code=2)
            self.assertIn("invalid facts JSON", rejected.stderr)
            self.assertNotIn("Traceback", rejected.stderr)
        bad.write_text(json.dumps([{"id": "bad", "text": "Unsupported claim."}]))
        before = sorted((self.store / "lessons").glob("*.json"))
        self.run_cli("lesson", "--revise", revision, "--facts-file", str(bad), code=2)
        self.assertEqual(sorted((self.store / "lessons").glob("*.json")), before)
        revised = json.loads(self.run_cli("lesson", "--revise", revision).stdout)
        saved = json.loads((self.store / "lessons" / (revised["id"] + ".json")).read_text())
        self.assertEqual(saved["facts"], facts)
        self.assertIsNone(saved["receipt"])
        bad.write_text("[]")
        cleared = json.loads(self.run_cli("lesson", "--revise", revised["id"],
            "--facts-file", str(bad), "--rule", "Use the new count convention.").stdout)
        saved = json.loads((self.store / "lessons" / (cleared["id"] + ".json")).read_text())
        self.assertNotIn("facts", saved)
        self.assertEqual(saved["rule"], "Use the new count convention.")
        self.assertIsNone(saved["receipt"])
        self.assertIn('"live_store_unchanged": true', output.getvalue())

    def test_dogfood_explicit_source_matches_store_and_rejects_mismatch_unchanged(self):
        def source_checkout(fixture):
            repo = fixture.prepare_lesson()
            for source in test_cli.CLI.parent.glob("*.py"):
                shutil.copy2(source, repo / source.name)
            subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "Source checkout"],
                           check=True, capture_output=True)
            name, = (repo / ".coldlocker" / "rules").glob("*.md")
            proof, _, _ = coldrecall.read_rule(name)
            return repo, tuple(proof["lesson"].split(":")[2:])

        def run_check(repo, store):
            return subprocess.run(
                [sys.executable, dogfood_store.__file__, "--repo", str(repo), "--store", str(store)],
                text=True, capture_output=True)

        def git_status(repo):
            return subprocess.check_output(["git", "-C", str(repo), "status", "--porcelain"], text=True)

        matching_repo, matching_identity = source_checkout(self)
        matching_before = dogfood_store.snapshot(self.store)
        matched = run_check(matching_repo, self.store)
        self.assertEqual(matched.returncode, 0, matched.stderr)
        self.assertIn('"live_store_unchanged": true', matched.stdout)
        self.assertEqual(dogfood_store.snapshot(self.store), matching_before)

        other = DogfoodTests()
        other.setUp()
        try:
            mismatched_repo, mismatched_identity = source_checkout(other)
            self.assertNotEqual(matching_identity, mismatched_identity)
            mismatched_before = dogfood_store.snapshot(other.store)
            mismatched = run_check(matching_repo, other.store)
            self.assertEqual(mismatched.returncode, 2)
            self.assertEqual(mismatched.stderr.strip(),
                             "INCOMPLETE: registered lesson identity is absent from store")
            self.assertEqual(dogfood_store.snapshot(other.store), mismatched_before)
            self.assertEqual(git_status(matching_repo), "")
            self.assertEqual(git_status(mismatched_repo), "")
        finally:
            other.doCleanups()

    def test_store_dogfood_requires_passing_witnesses(self):
        repo = self.prepare_lesson()
        for source in test_cli.CLI.parent.glob("*.py"):
            shutil.copy2(source, repo / source.name)

        def commit():
            subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
            if subprocess.run(["git", "-C", str(repo), "diff", "--cached", "--quiet"]).returncode:
                subprocess.run(["git", "-C", str(repo), "commit", "-qm", "Witness state"],
                               check=True, capture_output=True)

        def verify():
            with patch.object(dogfood_store, "ROOT", repo), patch.object(
                    sys, "argv", ["dogfood_store.py", "--store", str(self.store)]):
                dogfood_store.main()

        commit()
        passing = io.StringIO()
        with contextlib.redirect_stdout(passing):
            verify()
        self.assertIn('"witnesses": ["pass"]', passing.getvalue())
        self.assertIn('"live_store_unchanged": true', passing.getvalue())
        for outcome in ("regression", "no-verdict"):
            with self.subTest(witness=outcome):
                if outcome == "regression":
                    (repo / "count.txt").write_text("1")
                else:
                    (repo / "count.txt").unlink()
                    (repo / "count.txt").mkdir()
                    (repo / "count.txt" / "placeholder").write_text("not a count file")
                commit()
                compiled = json.loads(self.recall("compile", "--repo", str(repo)).stdout)
                self.assertEqual(len(compiled["active"]), 1)
                table = json.loads((repo / ".coldrecall/reflex.json").read_text())
                atom, = table["atoms"].values()
                self.assertEqual(atom["witness"]["result"], outcome)
                commit()  # Compile's generated registry must be part of the selected source snapshot.
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    with self.assertRaisesRegex(RuntimeError, "dogfood requires passing lesson witnesses"):
                        verify()
                self.assertNotIn('"PASS"', output.getvalue())
