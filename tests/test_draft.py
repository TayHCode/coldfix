import concurrent.futures
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_cli


class DraftTests(unittest.TestCase):
    run_cli = test_cli.CliTests.run_cli
    event = test_cli.CliTests.event

    def setUp(self):
        test_cli.CliTests.setUp(self)
        self.repo = self.cwd / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.repo / "value").write_text("bad")
        self.git("add", ".")
        self.git("commit", "-qm", "before")
        self.before = self.git("rev-parse", "HEAD")
        (self.repo / "value").write_text("good")
        self.git("commit", "-qam", "after")
        self.after = self.git("rev-parse", "HEAD")
        events = [self.event(error="value mismatch", source_ref=ref) for ref in ("first", "second")]
        self.run_cli("ingest", stdin="\n".join(map(json.dumps, events)))
        self.group = json.loads(self.run_cli("propose", "--grouping", "normalized").stdout)["groups"][0]["group_id"]
        self.output = self.cwd / "fixes.json"
        self.reply = self.cwd / "reply.json"
        self.prompt = self.cwd / "prompt.json"
        self.calls = self.cwd / "calls.json"
        self.bin = self.cwd / "bin"
        self.bin.mkdir()
        for name, body in {
            "acme": 'assert sys.argv[1:] == ["model", "get", "light"]\nprint(json.dumps(dict(provider="claudeAgent", model="claude-haiku-5-5")))\n',
            "claude": 'Path(os.environ["DRAFT_PROMPT"]).write_text(json.dumps(dict(arguments=sys.argv[1:], prompt=sys.stdin.read())))\n'
                      'Path(os.environ["DRAFT_CALLS"]).write_text("called")\n'
                      'if os.environ.get("DRAFT_BARRIER"):\n'
                      '    import time\n'
                      '    barrier=Path(os.environ["DRAFT_BARRIER"])\n'
                      '    (barrier / (str(os.getpid()) + ".ready")).touch()\n'
                      '    deadline=time.monotonic() + 10\n'
                      '    while len(list(barrier.glob("*.ready"))) < 2:\n'
                      '        if time.monotonic() > deadline:\n'
                      '            sys.exit(3)\n'
                      '        time.sleep(0.01)\n'
                      'print(Path(os.environ["DRAFT_REPLY"]).read_text())\n',
        }.items():
            executable = self.bin / name
            executable.write_text("#!" + sys.executable + "\nimport json,os,sys\nfrom pathlib import Path\n" + body)
            executable.chmod(0o700)
        environment = patch.dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"], COLDFIX_FLEET_CLI="acme",
                                 DRAFT_PROMPT=str(self.prompt), DRAFT_CALLS=str(self.calls), DRAFT_REPLY=str(self.reply))
        environment.start()
        self.addCleanup(environment.stop)
        self.script = ('import json,sys\nfrom pathlib import Path\n'
                       'ok=Path("value").read_text()=="good"\n'
                       'print(json.dumps({"status":"PASS"} if ok else {"status":"FAIL","failure":"value"}))\n'
                       'sys.exit(0 if ok else 1)\n')
        self.response = dict(check_script=self.script, expected_failure="value", kind="test",
                             failure="The value was wrong.", fix="Correct the value.", rule="Check the value before accepting changes.")

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], text=True).strip()

    def draft(self, code=0, group=None, store=None):
        prefix = ("--store", str(store)) if store else ()
        return self.run_cli(*prefix, "draft", "--group", group or self.group, "--repo", str(self.repo),
                            "--before", self.before, "--after", self.after, "--output", str(self.output), code=code)

    def prove(self):
        self.run_cli("ingest-and-propose", "--fixes", str(self.output), stdin="")
        return json.loads((self.store / "pending.json").read_text())["pending"][0]["replay"]

    def test_good_draft_replays_to_a_proven_lesson(self):
        self.reply.write_text(json.dumps(self.response))
        self.draft()
        entry = json.loads(self.output.read_text())[self.group]
        self.assertEqual(set(entry), {"repo", "before", "after", "check_file", "expected_failure", "kind", "failure", "fix", "rule"})
        self.assertEqual((entry["repo"], entry["before"], entry["after"]), (str(self.repo), self.before, self.after))
        check = Path(entry["check_file"])
        self.assertTrue(check.is_relative_to(self.store))
        self.assertEqual(check.stat().st_mode & 0o777, 0o600)
        self.assertEqual(check.read_text(), self.script)
        self.assertFalse((self.store / "lessons").exists())
        self.assertFalse((self.store / "receipts").exists())
        sent = json.loads(self.prompt.read_text())
        arguments = sent["arguments"]
        self.assertEqual(arguments[arguments.index("--model") + 1], "claude-haiku-5-5")
        prompt = sent["prompt"]
        self.assertNotIn("value mismatch", " ".join(arguments))
        for evidence in ("value mismatch", "first", "second", "-bad", "+good"):
            self.assertIn(evidence, prompt)
        proof = self.prove()
        self.assertEqual((proof["status"], proof["verification"]), ("PASS", "proven"))

    def test_model_command_drafts_without_cold_or_claude(self):
        tools = self.cwd / "tools"
        tools.mkdir()
        (tools / "git").symlink_to(shutil.which("git"))
        fake = self.cwd / "fake-model"
        fake.write_text("#!" + sys.executable + "\nimport json, os, sys\nfrom pathlib import Path\n"
                        "Path(os.environ['DRAFT_PROMPT']).write_text(json.dumps(dict(arguments=sys.argv[1:], prompt=sys.stdin.read())))\n"
                        "print(Path(os.environ['DRAFT_REPLY']).read_text())\n")
        fake.chmod(0o700)
        self.reply.write_text(json.dumps(self.response))
        with patch.dict(os.environ, PATH=str(tools), COLDFIX_MODEL_CMD=str(fake)):
            self.draft()
        proof = self.prove()
        self.assertFalse(self.calls.exists())
        role, shape = json.loads(self.prompt.read_text())["arguments"]
        self.assertEqual((role, sorted(json.loads(shape)["required"])), ("drafter", sorted(self.response)))
        self.assertIn("value mismatch", json.loads(self.prompt.read_text())["prompt"])
        self.assertEqual(json.loads(self.output.read_text())[self.group]["rule"], self.response["rule"])
        self.assertEqual((proof["status"], proof["verification"]), ("PASS", "proven"))

    def test_wrong_check_is_drafted_and_rejected_by_replay(self):
        marker = self.cwd / "executed"
        self.response["check_script"] = 'from pathlib import Path\nPath(' + repr(str(marker)) + ').touch()\nprint(\'{"status":"PASS"}\')\n'
        self.reply.write_text(json.dumps(self.response))
        self.draft()
        self.assertFalse(marker.exists())
        proof = self.prove()
        self.assertTrue(marker.exists())
        self.assertEqual((proof["status"], proof["verification"]), ("FAIL", "unproven"))

    def test_malformed_replies_write_nothing(self):
        baseline = {path: path.read_bytes() for path in self.store.rglob("*") if path.is_file()}
        invalid = ["", "not json", "[]", "{}", "[" * 2000]
        invalid += [json.dumps(dict(self.response, **change)) for change in (
            dict(check_script=""), dict(expected_failure=""), dict(kind="invalid"),
            dict(rule=None), dict(check_script="\x00"), dict(expected_failure="two\nlines"), dict(extra="field"))]
        invalid.append(json.dumps({key: value for key, value in self.response.items() if key != "fix"}))
        for existing in (False, True):
            if existing:
                self.output.write_text('{"retained": {"value": 1}}\n')
            for reply in invalid:
                with self.subTest(existing=existing, reply=reply[:60]):
                    self.reply.write_text(reply)
                    result = self.draft(code=2)
                    self.assertIn("invalid draft reply", result.stderr)
                    self.assertTrue(self.calls.exists())
                    self.assertEqual({path: path.read_bytes() for path in self.store.rglob("*") if path.is_file()}, baseline)
                    if existing:
                        self.assertEqual(self.output.read_text(), '{"retained": {"value": 1}}\n')
                    else:
                        self.assertFalse(self.output.exists())

    def test_redraft_replaces_only_its_group(self):
        retained = {"a" * 64: dict(unrelated="retained")}
        self.output.write_text(json.dumps(retained))
        self.reply.write_text(json.dumps(self.response))
        self.draft()
        previous = json.loads(self.output.read_text())[self.group]
        self.response["rule"] = "Use the revised value check."
        self.reply.write_text(json.dumps(self.response))
        self.draft()
        rows = json.loads(self.output.read_text())
        self.assertEqual(set(rows), {*retained, self.group})
        self.assertEqual(rows["a" * 64], retained["a" * 64])
        self.assertEqual(rows[self.group]["rule"], "Use the revised value check.")
        self.assertEqual(Path(previous["check_file"]).read_text(), self.script)

    def test_prompt_stays_on_stdin_and_supports_large_diffs(self):
        (self.repo / "large.txt").write_text("x" * 200000)
        self.git("add", ".")
        self.git("commit", "-qm", "large fix")
        self.after = self.git("rev-parse", "HEAD")
        self.reply.write_text(json.dumps(self.response))
        self.draft()
        sent = json.loads(self.prompt.read_text())
        self.assertGreater(len(sent["prompt"]), 200000)
        self.assertIn("value mismatch", sent["prompt"])
        self.assertIn("x" * 200000, sent["prompt"])
        self.assertNotIn("value mismatch", " ".join(sent["arguments"]))
        self.assertNotIn("x" * 1000, " ".join(sent["arguments"]))
        self.assertIn(self.group, json.loads(self.output.read_text()))

    def test_non_utf8_diff_is_drafted(self):
        (self.repo / "legacy.txt").write_bytes(b"caf\xe9\n")
        self.git("add", ".")
        self.git("commit", "-qm", "legacy encoding")
        self.after = self.git("rev-parse", "HEAD")
        self.reply.write_text(json.dumps(self.response))
        self.draft()
        self.assertIn(r"caf\\xe9", json.loads(self.prompt.read_text())["prompt"])
        self.assertIn(self.group, json.loads(self.output.read_text()))

    def test_parallel_drafts_preserve_both_groups_across_stores(self):
        events = [self.event(error="another mismatch", source_ref=ref) for ref in ("third", "fourth")]
        self.run_cli("ingest", stdin="\n".join(map(json.dumps, events)))
        groups = json.loads(self.run_cli("propose", "--grouping", "normalized").stdout)["groups"]
        other_group = next(row["group_id"] for row in groups if row["group_id"] != self.group)
        other_store = self.cwd / "other-store"
        shutil.copytree(self.store, other_store)
        retained = {"a" * 64: dict(unrelated="retained")}
        self.output.write_text(json.dumps(retained))
        self.reply.write_text(json.dumps(self.response))
        barrier = self.cwd / "barrier"
        barrier.mkdir()
        with patch.dict(os.environ, DRAFT_BARRIER=str(barrier)):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as workers:
                jobs = [workers.submit(self.draft), workers.submit(self.draft, group=other_group, store=other_store)]
                for job in jobs:
                    job.result()
        rows = json.loads(self.output.read_text())
        self.assertEqual(set(rows), {*retained, self.group, other_group})
        self.assertEqual(rows["a" * 64], retained["a" * 64])
        for group in (self.group, other_group):
            self.assertEqual(Path(rows[group]["check_file"]).read_text(), self.script)

    def test_repo_path_with_controls_is_rejected(self):
        self.repo = self.repo.rename(self.cwd / "repo\nline")
        self.reply.write_text(json.dumps(self.response))
        self.draft(code=2)
        self.assertFalse(self.output.exists())
        self.assertFalse((self.store / "drafts").exists())

    def test_store_path_with_controls_is_rejected(self):
        store = self.cwd / "store\nline"
        shutil.copytree(self.store, store)
        self.reply.write_text(json.dumps(self.response))
        self.draft(store=store, code=2)
        self.assertFalse(self.output.exists())
        self.assertFalse((store / "drafts").exists())
