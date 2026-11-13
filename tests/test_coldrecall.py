import json
import hashlib
import os
import re
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

import coldfix as c
import coldrecall as recall

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "coldrecall.py"
LEGACY_CLI = ROOT / "coldimage.py"
LESSON = "a" * 32
MISSING = object()
CHECK = '''import json, sys
from pathlib import Path
path = Path("guarded.py")
if not path.exists():
    sys.exit(3)
ok = path.read_text().strip() == "good"
print(json.dumps({"status": "PASS"} if ok else {"status": "FAIL", "failure": "guarded-value"}))
sys.exit(0 if ok else 1)
'''
COMMAND = "python3 check.py"


def run(*command, cwd=None, stdin=None):
    return subprocess.run(command, cwd=cwd, input=stdin, text=True, capture_output=True)


def rule_file(before, after, revision="b" * 32, command=COMMAND, status="active", rule="Guarded value must stay good.", recorded=COMMAND, lesson=None):
    proof = dict(
        schema_version=1, lesson=f"coldfix:lesson:{LESSON}:{revision}", source_evidence=["coldfix:failure:" + "c" * 32],
        what_went_wrong="The value went bad.", fix="Write the good value.", rule=rule,
        replay=dict(receipt="coldfix:replay:" + "d" * 32, expected_failure="guarded-value",
                    check_sha256=c.digest(recorded + "\n"),
                    before=dict(commit=before, tree="1" * 40, exit=1, status="FAIL"),
                    after=dict(commit=after, tree="2" * 40, exit=0, status="PASS")),
        gates=dict(privacy="PASS", release_check="PASS"))
    if lesson is not None:
        proof["lesson"] = lesson
    if lesson is MISSING:
        del proof["lesson"]
    return (f"# Guard\n\n{rule}\n\n## Lesson record\n\n```json\n{json.dumps(proof, indent=2)}\n```\n\n"
            f"## Verify\n\nKind: test\n\nLevel: test\n\nStatus: {status}\n\nVerification: proven\n\n"
            f"Scope: repository-specific\n\n```sh\n{command}\n```\n\n## Evidence\n\n"
            f"Lesson: coldfix:lesson:{LESSON}:{revision}\n\nReplay: coldfix:replay:{'d' * 32}\n\n"
            f"- coldfix:failure:{'c' * 32}\n")


class ImageCase(unittest.TestCase):
    DIRECTORY_NAME = ""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name) / self.DIRECTORY_NAME
        self.repo.mkdir(exist_ok=True)
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.repo / "check.py").write_text(CHECK)
        self.write("guarded.py", "bad")
        self.before = self.commit("bad")
        self.write("guarded.py", "good")
        self.after = self.commit("good")
        self.install(rule_file(self.before, self.after))

    def git(self, *args):
        result = run("git", "-C", str(self.repo), *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def write(self, name, content):
        (self.repo / name).write_text(content)

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")

    def install(self, body, name="lesson-" + LESSON, pending=False):
        (self.repo / ".coldlocker/rules").mkdir(parents=True, exist_ok=True)
        path = self.repo / ".coldlocker/rules" / (name + ".md")
        path.write_text(body)
        digest = hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        registry = {name: dict(hashes=[digest], lesson=True, pending=pending)}
        (self.repo / ".coldlocker/rules.json").write_text(json.dumps(registry))

    def compile(self):
        result = run(sys.executable, str(CLI), "compile", "--repo", str(self.repo), cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def lookup(self, *args):
        result = run(sys.executable, str(CLI), "lookup", "--repo", str(self.repo), *args, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.splitlines()

    def image(self, query):
        with sqlite3.connect(self.repo / ".coldrecall/project.cold") as database:
            return database.execute(query).fetchall()


class CompileAndLookupTests(ImageCase):
    def test_compile_from_subdirectories_uses_the_root_image_and_witness(self):
        nested = self.repo / "checks"
        nested.mkdir()
        ref = f"coldfix:lesson:{LESSON}:{'b' * 32}"
        line = f"[coldrecall] Guarded value must stay good. (witness passed at {self.after[:12]})"
        for cwd, options in ((nested, ()), (self.repo, ("--repo", "checks"))):
            with self.subTest(cwd=cwd, options=options):
                result = run(sys.executable, str(CLI), "compile", *options, cwd=cwd)
                self.assertEqual(result.returncode, 0, result.stderr)
                summary = json.loads(result.stdout)
                self.assertEqual((summary["active"], summary["regressions"]), ([ref], []))
                self.assertTrue((self.repo / ".coldrecall/project.cold").is_file())
                self.assertEqual(self.image("SELECT ref FROM facts"), [(ref,)])
                table = json.loads((self.repo / ".coldrecall/reflex.json").read_text())
                self.assertEqual(table["anchors"]["path:guarded.py"], [ref])
                self.assertEqual(table["atoms"][ref]["witness"]["result"], "pass")
                looked = run(sys.executable, str(CLI), "lookup", *options, "--path", "../guarded.py", cwd=cwd)
                self.assertEqual(looked.returncode, 0, looked.stderr)
                self.assertEqual(looked.stdout.splitlines(), [line])
                event = dict(cwd=str(nested), tool_input=dict(file_path="../guarded.py"))
                hook = run(sys.executable, str(CLI), "hook", cwd=nested, stdin=json.dumps(event))
                self.assertEqual(hook.returncode, 0, hook.stderr)
                self.assertEqual(json.loads(hook.stdout)["hookSpecificOutput"]["additionalContext"], line)
                self.assertFalse((nested / ".coldrecall").exists())

    def test_atomic_facts_share_one_witness_and_retire_together(self):
        text = "Keep guarded good. Preserve Unicode strings."
        body = rule_file(self.before, self.after, rule=text)
        proof, _, _ = self.read_proof(body)
        proof["facts"] = [{"id": "guard", "text": "Keep guarded good."},
                          {"id": "unicode", "text": "Preserve Unicode strings."}]
        original = recall.FENCE.search(recall.sections(body)["Lesson record"]).group(2)
        self.install(body.replace(original, json.dumps(proof)))
        summary = self.compile()
        self.assertEqual(len(summary["active"]), 2)
        self.assertEqual(len(self.image("SELECT ref FROM facts")), 1)
        self.assertEqual(len(self.image("SELECT event FROM events WHERE event='added'")), 1)
        refs, lines = recall.lookup(self.repo, paths=["guarded.py"], text="Unicode strings", ranking="overlap", limit=1)
        self.assertTrue(refs[0].endswith("#unicode"))
        self.assertIn("Preserve Unicode strings.", lines[0])
        self.assertNotIn("Keep guarded good.", lines[0])
        self.assertEqual(self.compile()["epoch"], summary["epoch"])
        self.install(body.replace("Status: active", "Status: retired"))
        self.assertEqual(self.compile()["active"], [])
        self.assertEqual(self.lookup("--path", "guarded.py"), [])

    def read_proof(self, body):
        path = self.repo / "candidate.md"
        path.write_text(body)
        return recall.read_rule(path)

    def test_atomic_fact_validation_rejects_bad_ids_and_unproven_text(self):
        body = rule_file(self.before, self.after)
        proof, _, _ = self.read_proof(body)
        original = recall.FENCE.search(recall.sections(body)["Lesson record"]).group(2)
        valid = {"id": "one", "text": "Guarded value must stay good."}
        for facts in ([], None, [valid, valid], [{"id": "x#y", "text": valid["text"]}],
                      [{"id": "one", "text": "New unsupported instruction."}],
                      [{"id": "one", "text": ""}], [{"id": "one"}]):
            with self.subTest(facts=facts):
                with self.assertRaises(c.ColdfixError):
                    self.read_proof(body.replace(original, json.dumps(dict(proof, facts=facts))))

    def test_path_and_command_actions_get_matching_facts_and_others_get_nothing(self):
        summary = self.compile()
        self.assertEqual(len(summary["active"]), 1)
        self.assertEqual(summary["retired"], {})
        for args in (["--path", "guarded.py"], ["--path", str(self.repo / "guarded.py")],
                     ["--command", "sed -i s/a/b/ ./guarded.py"]):
            with self.subTest(args=args):
                lines = self.lookup(*args)
                self.assertEqual(len(lines), 1)
                self.assertIn("Guarded value must stay good.", lines[0])
                self.assertIn("witness passed", lines[0])
        self.assertEqual(self.lookup("--path", "check.py"), [])
        self.assertEqual(self.lookup("--command", "ls -la"), [])

    def test_image_is_versioned_and_recompiling_unchanged_state_keeps_the_epoch(self):
        first = self.compile()
        second = self.compile()
        self.assertEqual((first["epoch"], second["epoch"]), (1, 1))
        self.assertEqual(self.image("SELECT value FROM meta WHERE key='format'"), [("1",)])
        self.assertEqual([row[0] for row in self.image("SELECT event FROM events")], ["added"])
        self.commit("new head")
        self.assertEqual(self.compile()["epoch"], 2)

    def test_pass_from_an_older_head_is_not_presented_as_current(self):
        self.compile()
        self.commit("later")
        self.assertIn("not re-run on this head", self.lookup("--path", "guarded.py")[0])

    def test_pending_or_unproven_records_are_not_compiled(self):
        self.install(rule_file(self.before, self.after), pending=True)
        self.assertEqual(self.compile()["active"], [])
        self.install(rule_file(self.before, self.after).replace("Verification: proven", "Verification: unproven"))
        summary = self.compile()
        self.assertEqual((summary["active"], len(summary["skipped"])), ([], 1))

    def test_missing_table_and_bad_hook_input_fail_open(self):
        self.assertEqual(self.lookup("--path", "guarded.py"), [])
        for stdin in ("not json", "{}", json.dumps(dict(tool_input=None))):
            with self.subTest(stdin=stdin):
                self.assertEqual(run(sys.executable, str(CLI), "hook", cwd=self.repo, stdin=stdin).returncode, 0)

    def test_hook_adds_facts_for_the_tool_call_and_exits_zero(self):
        self.compile()
        for flags, tool_input in ((), dict(file_path=str(self.repo / "guarded.py"))), (("-I",), dict(file_path=str(self.repo / "guarded.py"))), ((), dict(command="cat guarded.py")):
            with self.subTest(flags=flags, tool_input=tool_input):
                result = run(sys.executable, *flags, str(CLI), "hook", stdin=json.dumps(dict(cwd=str(self.repo), tool_input=tool_input)))
                self.assertEqual(result.returncode, 0, result.stderr)
                output = json.loads(result.stdout)["hookSpecificOutput"]
                self.assertEqual(output["hookEventName"], "PreToolUse")
                self.assertIn("Guarded value must stay good.", output["additionalContext"])
        quiet = run(sys.executable, str(CLI), "hook", stdin=json.dumps(dict(cwd=str(self.repo), tool_input=dict(command="ls"))))
        self.assertEqual((quiet.returncode, quiet.stdout), (0, ""))


class HookContractTests(ImageCase):
    def hook(self, stdin):
        result = run(sys.executable, "-I", str(CLI), "hook", stdin=stdin)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def event(self, cwd):
        return json.dumps(dict(cwd=str(cwd), tool_input=dict(file_path="guarded.py")))

    def test_bad_input_and_a_missing_store_exit_zero_with_no_output(self):
        with tempfile.TemporaryDirectory() as plain:
            for stdin in ("", "not json", "null", "[]", self.event(self.repo), self.event(plain)):
                with self.subTest(stdin=stdin):
                    self.assertEqual(self.hook(stdin), "")

    def test_an_event_on_stdin_writes_its_lesson_to_stdout(self):
        self.compile()
        output = json.loads(self.hook(self.event(self.repo)))["hookSpecificOutput"]
        self.assertIn("Guarded value must stay good.", output["additionalContext"])


class ProjectRootTests(ImageCase):
    def hook(self, cwd, tool_input):
        result = run(sys.executable, str(CLI), "hook", stdin=json.dumps(dict(cwd=str(cwd), tool_input=tool_input)))
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_hook_in_a_subdirectory_reads_the_project_root_image(self):
        self.compile()
        sub = self.repo / "pkg" / "deep"
        sub.mkdir(parents=True)
        for tool_input in (dict(file_path=str(self.repo / "guarded.py")), dict(file_path="../../guarded.py"),
                           dict(command="cat ../../guarded.py")):
            with self.subTest(tool_input=tool_input):
                self.assertIn("Guarded value must stay good.", self.hook(sub, tool_input))

    def test_relative_paths_resolve_from_the_working_directory_not_the_root(self):
        self.compile()
        sub = self.repo / "pkg"
        sub.mkdir()
        self.assertEqual(self.hook(sub, dict(file_path="guarded.py")), "")
        self.assertEqual(self.hook(sub, dict(command="cat guarded.py")), "")

    def test_lookup_cli_from_a_subdirectory_finds_the_root_table(self):
        self.compile()
        sub = self.repo / "pkg"
        sub.mkdir()
        result = run(sys.executable, str(CLI), "lookup", "--repo", str(sub), "--path", "../guarded.py", cwd=ROOT)
        self.assertIn("Guarded value must stay good.", result.stdout)


class LinkedWorktreeTests(ImageCase):
    def worktree(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        linked = Path(temporary.name) / "linked"
        self.git("worktree", "add", "-q", "--detach", str(linked))
        return linked

    def session_start(self, cwd, *options, **payload):
        result = run(sys.executable, "-I", str(CLI), "hook", "--budget", "5", *options,
                     stdin=json.dumps(dict(hook_event_name="SessionStart", cwd=str(cwd), **payload)))
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_a_linked_worktree_without_its_own_table_reads_the_main_checkout_table(self):
        self.compile()
        linked = self.worktree()
        self.assertIn("Guarded value must stay good.", self.session_start(linked))
        self.assertFalse((linked / ".coldrecall").exists())

    def test_once_delivery_in_a_fresh_linked_worktree_reads_the_main_checkout_table(self):
        self.compile()
        linked = self.worktree()
        self.assertIn("Guarded value must stay good.", self.session_start(linked, "--once", session_id="s1"))
        self.assertEqual(self.session_start(linked, "--once", session_id="s1"), "")


class TrailingWhitespacePathTests(ImageCase):
    DIRECTORY_NAME = "project "

    test_compile_from_subdirectories_uses_the_root_image_and_witness = (
        CompileAndLookupTests.test_compile_from_subdirectories_uses_the_root_image_and_witness)


class ReservedCharacterPathTests(ImageCase):
    DIRECTORY_NAME = "project#one?x%20y"

    def test_repeated_compiles_and_lookup_work_when_the_checkout_path_has_uri_reserved_characters(self):
        self.assertEqual(self.compile()["epoch"], 1)
        self.commit("later")
        self.assertEqual(self.compile()["epoch"], 2)
        (self.repo / "guarded.py").unlink()
        self.assertEqual(len(self.compile()["retired"]), 1)
        self.assertEqual(self.image("SELECT event FROM events ORDER BY seq")[-1], ("retired",))


class WitnessContentIsNotMetadataTests(ImageCase):
    HEREDOC = "python3 check.py <<'EOF'\nStatus: retired\nVerification: unproven\nKind: other\nEOF"

    def test_metadata_lines_inside_the_witness_do_not_change_the_lesson_lifecycle(self):
        self.compile()
        self.install(rule_file(self.before, self.after, command=self.HEREDOC))
        summary = self.compile()
        self.assertEqual((len(summary["active"]), summary["retired"], summary["skipped"]), (1, {}, []))
        self.install(rule_file(self.before, self.after))
        self.assertEqual((len(self.compile()["active"]), self.lookup("--path", "guarded.py") != []), (1, True))

    def test_real_status_outside_the_witness_still_retires_the_lesson(self):
        self.compile()
        self.install(rule_file(self.before, self.after, command=self.HEREDOC.replace("retired", "active"), status="retired"))
        self.assertEqual(list(self.compile()["retired"].values()), ["lesson retired"])


class FencedWitnessSectionTests(ImageCase):
    FENCED = "python3 check.py <<'EOF'\n## Fixture\nEOF"

    def test_a_heading_line_inside_the_fenced_witness_does_not_truncate_it(self):
        self.install(rule_file(self.before, self.after, command=self.FENCED, recorded=self.FENCED))
        summary = self.compile()
        self.assertEqual((len(summary["active"]), summary["skipped"], summary["regressions"]), (1, [], []))
        lines = self.lookup("--path", "guarded.py")
        self.assertEqual(len(lines), 1)
        self.assertIn("witness passed", lines[0])

    def test_headings_after_the_fence_still_start_new_sections(self):
        body = rule_file(self.before, self.after, command=self.FENCED, recorded=self.FENCED) + "\n## Notes\n\nStatus: retired\n"
        self.install(body)
        self.assertEqual(len(self.compile()["active"]), 1)


class MalformedRecordIdentityTests(ImageCase):
    BAD = {"missing": MISSING, "short": "coldfix:lesson", "no revision": f"coldfix:lesson:{LESSON}", "empty id": "coldfix:lesson::" + "b" * 32,
           "wrong prefix": f"other:lesson:{LESSON}:{'b' * 32}", "not a string": 5}

    def test_a_malformed_lesson_identifier_is_skipped_and_never_aborts_compile(self):
        for label, lesson in self.BAD.items():
            with self.subTest(label=label):
                self.install(rule_file(self.before, self.after, lesson=lesson))
                summary = self.compile()
                self.assertEqual((summary["active"], summary["skipped"]), ([], ["lesson-" + LESSON]))

    def test_a_malformed_lesson_identifier_keeps_the_previous_fact_until_readable(self):
        self.compile()
        for label, lesson in self.BAD.items():
            with self.subTest(label=label):
                self.install(rule_file(self.before, self.after, lesson=lesson))
                summary = self.compile()
                self.assertEqual((len(summary["active"]), summary["retired"], len(summary["held"])), (1, {}, 1))
                self.assertEqual(len(self.lookup("--path", "guarded.py")), 1)

    def test_a_registry_entry_that_is_not_an_object_is_skipped_and_holds_its_fact(self):
        self.compile()
        (self.repo / ".coldlocker/rules.json").write_text(json.dumps({"lesson-" + LESSON: "oops"}))
        summary = self.compile()
        self.assertEqual((len(summary["active"]), summary["retired"], summary["skipped"]), (1, {}, ["lesson-" + LESSON]))


class UndeliveredAndMalformedRecordTests(ImageCase):
    NAME = "lesson-" + LESSON

    def register(self, **entry):
        path = self.repo / ".coldlocker/rules" / (self.NAME + ".md")
        digest = hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        registration = dict(hashes=[digest], lesson=True)
        registration.update(entry)
        (self.repo / ".coldlocker/rules.json").write_text(json.dumps({self.NAME: registration}))

    def proof_body(self):
        path = self.repo / ".coldlocker/rules" / (self.NAME + ".md")
        body = path.read_text()
        record = recall.FENCE.search(recall.sections(body)["Lesson record"]).group(2)
        return path, body, json.loads(record)

    def replace_proof(self, path, body, proof):
        original = recall.FENCE.search(recall.sections(body)["Lesson record"]).group(2)
        path.write_text(body.replace(original, json.dumps(proof, indent=2)))

    def test_invalid_gate_values_are_skipped_while_valid_hash_and_gates_compile(self):
        self.install(rule_file(self.before, self.after))
        self.register(pending=False)
        self.assertEqual(len(self.compile()["active"]), 1)

        ref = f"coldfix:lesson:{LESSON}:{'b' * 32}"
        for gates in (dict(privacy="FAIL", release_check="PASS"),
                      dict(privacy="PASS", release_check="INCOMPLETE")):
            with self.subTest(gates=gates):
                path, body, proof = self.proof_body()
                proof["gates"] = gates
                self.replace_proof(path, body, proof)
                self.register(pending=False)
                summary = self.compile()
                self.assertEqual((summary["active"], summary["skipped"], summary["held"]),
                                 ([ref], [self.NAME], [ref]))
                proof["gates"] = dict(privacy="PASS", release_check="PASS")
                body = path.read_text()
                self.replace_proof(path, body, proof)
                self.register(pending=False)

    def test_unowned_source_hash_is_skipped_without_compiling_its_fact(self):
        self.install(rule_file(self.before, self.after))
        self.register(pending=False, hashes=["0" * 64])
        summary = self.compile()
        self.assertEqual((summary["active"], summary["skipped"]), ([], [self.NAME]))

    def test_registered_rule_symlink_and_fifo_are_rejected_without_blocking(self):
        ref = f"coldfix:lesson:{LESSON}:{'b' * 32}"
        for kind in ("symlink", "fifo"):
            with self.subTest(kind=kind):
                path = self.repo / ".coldlocker/rules" / (self.NAME + ".md")
                if path.exists() or path.is_symlink():
                    path.unlink()
                self.install(rule_file(self.before, self.after))
                self.register(pending=False)
                self.assertEqual(len(self.compile()["active"]), 1)
                path.unlink()
                if kind == "symlink":
                    target = self.repo / ".coldlocker/outside.md"
                    target.write_text(rule_file(self.before, self.after))
                    path.symlink_to(target)
                else:
                    os.mkfifo(path)
                result = subprocess.run([sys.executable, str(CLI), "compile", "--repo", str(self.repo)],
                                        cwd=ROOT, text=True, capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                summary = json.loads(result.stdout)
                self.assertEqual((summary["active"], summary["skipped"], summary["held"]),
                                 ([ref], [self.NAME], [ref]))

    def test_malformed_v2_admission_is_skipped_but_valid_v2_compiles(self):
        path, body, proof = self.proof_body()
        proof["schema_version"] = 2
        proof["admission"] = dict(kind="verified_incident", diagnostic="FAIL: synthetic", test_sha256="5" * 64,
                                   source_ref_sha256="6" * 64, test_paths_sha256=["7" * 64])
        proof["replay"]["expected_failure"] = "regression:" + "5" * 16
        proof["replay"]["test_sha256"] = "5" * 64
        proof["replay"]["reversed"] = dict(commit=self.after, tree="3" * 40, exit=1, status="FAIL")
        self.replace_proof(path, body, proof)
        self.register(pending=False)
        self.assertEqual(len(self.compile()["active"]), 1)

        path, body, proof = self.proof_body()
        proof["admission"] = {}
        self.replace_proof(path, body, proof)
        self.register(pending=False)
        summary = self.compile()
        ref = f"coldfix:lesson:{LESSON}:{'b' * 32}"
        self.assertEqual((summary["active"], summary["skipped"], summary["held"]), ([ref], [self.NAME], [ref]))

    def test_unhashable_schema_array_values_fail_closed(self):
        self.install(rule_file(self.before, self.after))
        self.register(pending=False)
        self.assertEqual(len(self.compile()["active"]), 1)
        path, body, proof = self.proof_body()
        proof["source_evidence"] = [{}]
        self.replace_proof(path, body, proof)
        self.register(pending=False)
        summary = self.compile()
        ref = f"coldfix:lesson:{LESSON}:{'b' * 32}"
        self.assertEqual((summary["active"], summary["skipped"], summary["held"]), ([ref], [self.NAME], [ref]))

        self.install(rule_file(self.before, self.after))
        self.register(pending=False)
        self.assertEqual(len(self.compile()["active"]), 1)
        path, body, proof = self.proof_body()
        proof["schema_version"] = 2
        proof["admission"] = dict(kind="verified_incident", diagnostic="FAIL: synthetic", test_sha256="5" * 64,
                                   source_ref_sha256="6" * 64, test_paths_sha256=[{}])
        proof["replay"]["expected_failure"] = "regression:" + "5" * 16
        proof["replay"]["test_sha256"] = "5" * 64
        proof["replay"]["reversed"] = dict(commit=self.after, tree="3" * 40, exit=1, status="FAIL")
        self.replace_proof(path, body, proof)
        self.register(pending=False)
        summary = self.compile()
        self.assertEqual((summary["active"], summary["skipped"], summary["held"]), ([ref], [self.NAME], [ref]))

    def test_a_registration_without_explicit_pending_false_is_not_compiled(self):
        for label, entry in {"missing": {}, "null": dict(pending=None), "zero": dict(pending=0), "string": dict(pending="false")}.items():
            with self.subTest(label=label):
                self.install(rule_file(self.before, self.after))
                self.register(**entry)
                summary = self.compile()
                self.assertEqual((summary["active"], summary["skipped"]), ([], []))
                self.assertEqual(self.lookup("--path", "guarded.py"), [])

    def test_a_registry_that_is_not_an_object_is_reported_not_a_crash(self):
        (self.repo / ".coldlocker").mkdir(exist_ok=True)
        (self.repo / ".coldlocker/rules.json").write_text("[]")
        result = run(sys.executable, str(CLI), "compile", "--repo", str(self.repo), cwd=ROOT)
        self.assertEqual((result.returncode, "Traceback" in result.stderr), (2, False))

    def test_a_proof_that_is_not_a_json_object_is_skipped_and_holds_its_fact(self):
        self.install(rule_file(self.before, self.after))
        self.compile()
        good = (self.repo / ".coldlocker/rules" / (self.NAME + ".md")).read_text()
        for value in ("[]", "null", "5", '"text"'):
            with self.subTest(value=value):
                broken = re.sub(r"(```json\n).*?(\n```)", lambda m: m.group(1) + value + m.group(2), good, count=1, flags=re.S)
                self.install(broken)
                summary = self.compile()
                self.assertEqual((len(summary["active"]), summary["retired"], summary["skipped"]), (1, {}, [self.NAME]))


class CrlfRuleFileTests(ImageCase):
    def test_rule_file_with_crlf_line_endings_compiles_and_answers_lookup(self):
        body = rule_file(self.before, self.after)
        self.install(body.replace("\n", "\r\n"))
        self.assertIn(b"\r\n", (self.repo / ".coldlocker/rules" / ("lesson-" + LESSON + ".md")).read_bytes())
        summary = self.compile()
        self.assertEqual((len(summary["active"]), summary["skipped"]), (1, []))
        lines = self.lookup("--path", "guarded.py")
        self.assertEqual(len(lines), 1)
        self.assertIn("witness passed", lines[0])


class LiteralFilenameTests(ImageCase):
    NAMES = ("café.py", " leading space.py")

    def install_proof_touching_unusual_names(self):
        for name in self.NAMES:
            self.write(name, "bad")
        before = self.commit("unusual names bad")
        for name in self.NAMES:
            self.write(name, "good")
        after = self.commit("unusual names good")
        self.install(rule_file(before, after))

    def test_anchors_keep_the_literal_names_git_would_quote_or_trim(self):
        self.install_proof_touching_unusual_names()
        self.compile()
        for name in self.NAMES:
            with self.subTest(name=name):
                lines = self.lookup("--path", name)
                self.assertEqual(len(lines), 1)
                self.assertIn("Guarded value must stay good.", lines[0])

    def test_existing_unusual_file_keeps_the_fact_when_the_witness_gives_no_verdict(self):
        self.install_proof_touching_unusual_names()
        self.write("check.py", "import sys\nsys.exit(3)\n")
        summary = self.compile()
        self.assertEqual((len(summary["active"]), summary["retired"]), (1, {}))
        for name in self.NAMES:
            (self.repo / name).unlink()
        summary = self.compile()
        self.assertEqual(summary["active"], [])
        self.assertIn("obsolete", next(iter(summary["retired"].values())))


class RegressionIsNotRetirementTests(ImageCase):
    def test_a_failing_witness_from_a_regression_keeps_the_fact_and_only_warns(self):
        self.compile()
        self.write("guarded.py", "bad")
        summary = self.compile()
        ref = f"coldfix:lesson:{LESSON}:{'b' * 32}"
        self.assertEqual((summary["regressions"], summary["retired"], summary["active"]), ([ref], {}, [ref]))
        lines = self.lookup("--path", "guarded.py")
        self.assertEqual(len(lines), 1)
        self.assertIn("REGRESSION (guarded-value)", lines[0])
        self.assertIn("Guarded value must stay good.", lines[0])
        hook = run(sys.executable, str(CLI), "hook", stdin=json.dumps(dict(cwd=str(self.repo), tool_input=dict(file_path="guarded.py"))))
        self.assertEqual(hook.returncode, 0)
        self.assertIn("REGRESSION", hook.stdout)

    def test_protection_survives_repeated_regressions_and_ends_when_the_code_is_fixed(self):
        self.write("guarded.py", "bad")
        for _ in range(2):
            self.commit("regress")
            self.assertEqual(len(self.compile()["regressions"]), 1)
        self.write("guarded.py", "good")
        summary = self.compile()
        self.assertEqual((summary["regressions"], len(summary["active"]), summary["retired"]), ([], 1, {}))
        self.assertIn("witness passed", self.lookup("--path", "guarded.py")[0])

    def test_regression_is_recorded_in_the_image_history_not_as_retirement(self):
        self.compile()
        self.write("guarded.py", "bad")
        self.compile()
        self.assertEqual([row[0] for row in self.image("SELECT event FROM events")], ["added", "witness"])
        self.assertEqual(self.image("SELECT detail FROM events WHERE event='witness'"), [("regression",)])


class AutomaticRetirementTests(ImageCase):
    def test_no_verdict_with_every_guarded_path_gone_retires_the_fact(self):
        self.compile()
        (self.repo / "guarded.py").unlink()
        summary = self.compile()
        self.assertEqual(summary["active"], [])
        self.assertIn("obsolete", next(iter(summary["retired"].values())))
        self.assertEqual(self.lookup("--path", "guarded.py"), [])
        self.assertEqual(self.image("SELECT event FROM events WHERE event='retired'"), [("retired",)])

    def test_no_verdict_while_guarded_code_still_exists_retains_the_fact(self):
        self.compile()
        self.write("check.py", "import sys\nsys.exit(3)\n")
        summary = self.compile()
        self.assertEqual((len(summary["active"]), summary["retired"]), (1, {}))
        self.assertIn("witness not run", self.lookup("--path", "guarded.py")[0])

    def test_retired_fact_stays_retired_and_out_of_the_table(self):
        self.compile()
        (self.repo / "guarded.py").unlink()
        self.compile()
        self.write("guarded.py", "good")
        summary = self.compile()
        self.assertEqual((summary["active"], len(summary["retired"])), ([], 1))
        self.assertEqual(self.lookup("--path", "guarded.py"), [])

    def test_new_revision_supersedes_the_old_fact(self):
        self.compile()
        self.install(rule_file(self.before, self.after, revision="c" * 32, rule="Newer wording."))
        summary = self.compile()
        old, new = f"coldfix:lesson:{LESSON}:{'b' * 32}", f"coldfix:lesson:{LESSON}:{'c' * 32}"
        self.assertEqual(summary["active"], [new])
        self.assertEqual(summary["retired"], {old: "superseded by " + new})
        self.assertIn("Newer wording.", self.lookup("--path", "guarded.py")[0])

    def test_withdrawn_or_retired_source_retires_the_fact(self):
        self.compile()
        (self.repo / ".coldlocker/rules.json").write_text("{}")
        withdrawn = self.compile()
        self.assertEqual(list(withdrawn["retired"].values()), ["source withdrawn"])
        self.assertEqual(self.lookup("--path", "guarded.py"), [])
        self.write("unrelated.py", "after withdrawal")
        self.commit("after withdrawal")
        refreshed = self.compile()
        self.assertEqual(refreshed["active"], [])
        self.assertEqual(self.image("SELECT value FROM meta WHERE key='head'"), [(refreshed["head"],)])

    def test_unreadable_source_keeps_its_fact_with_a_warning_until_it_is_readable_again(self):
        self.compile()
        good = rule_file(self.before, self.after)
        for broken in ("## Lesson record\n\n```json\n{\n```\n", good.replace("Verification: proven", "Verification: unproven")):
            with self.subTest(broken=broken[:20]):
                self.install(broken)
                result = run(sys.executable, str(CLI), "compile", "--repo", str(self.repo), cwd=ROOT)
                self.assertEqual(result.returncode, 0, result.stderr)
                summary = json.loads(result.stdout)
                self.assertEqual((len(summary["active"]), summary["retired"], len(summary["held"])), (1, {}, 1))
                self.assertIn("source unreadable, fact kept", result.stderr)
                self.assertIn("Guarded value must stay good.", self.lookup("--path", "guarded.py")[0])
        self.install(good)
        summary = self.compile()
        self.assertEqual((len(summary["active"]), summary["retired"], summary["held"]), (1, {}, []))

    def test_fact_held_by_an_unreadable_source_still_retires_once_readable_and_obsolete(self):
        self.compile()
        self.install("not a rule")
        self.compile()
        (self.repo / "guarded.py").unlink()
        self.install(rule_file(self.before, self.after))
        summary = self.compile()
        self.assertEqual(summary["active"], [])
        self.assertIn("obsolete", next(iter(summary["retired"].values())))

    def test_lesson_marked_retired_in_its_rule_file_retires_the_fact(self):
        self.compile()
        self.install(rule_file(self.before, self.after, status="retired"))
        self.assertEqual(list(self.compile()["retired"].values()), ["lesson retired"])

    def test_conditional_rule_file_stays_active_and_superseded_retires(self):
        self.install(rule_file(self.before, self.after, status="conditional"))
        summary = self.compile()
        self.assertEqual((len(summary["active"]), summary["retired"]), (1, {}))
        self.install(rule_file(self.before, self.after, status="superseded"))
        self.assertEqual(list(self.compile()["retired"].values()), ["lesson superseded"])


class WitnessTrustTests(ImageCase):
    def test_check_that_differs_from_its_proof_record_is_not_executed(self):
        marker = self.repo / "ran"
        self.install(rule_file(self.before, self.after, command=f"touch {marker}"))
        summary = self.compile()
        self.assertFalse(marker.exists())
        self.assertEqual((len(summary["active"]), summary["retired"]), (1, {}))
        self.assertIn("does not match its proof record", self.lookup("--path", "guarded.py")[0])


class LegacyNameTests(ImageCase):
    def test_new_names_are_used_and_tagged(self):
        self.compile()
        self.assertTrue((self.repo / ".coldrecall/reflex.json").is_file())
        self.assertFalse((self.repo / ".coldimage").exists())
        self.assertTrue(self.lookup("--path", "guarded.py")[0].startswith("[coldrecall] "))

    def test_old_cli_still_compiles_looks_up_and_hooks(self):
        result = run(sys.executable, str(LEGACY_CLI), "compile", "--repo", str(self.repo), cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        looked = run(sys.executable, str(LEGACY_CLI), "lookup", "--repo", str(self.repo), "--path", "guarded.py", cwd=ROOT)
        self.assertIn("Guarded value must stay good.", looked.stdout)
        hook = run(sys.executable, str(LEGACY_CLI), "hook", stdin=json.dumps(dict(cwd=str(self.repo), tool_input=dict(file_path="guarded.py"))))
        self.assertEqual(hook.returncode, 0, hook.stderr)
        self.assertIn("Guarded value must stay good.", hook.stdout)

    def test_old_directory_is_still_read_for_lookup_and_migrated_on_compile(self):
        self.compile()
        self.commit("later")
        self.compile()
        (self.repo / ".coldrecall").rename(self.repo / ".coldimage")
        self.assertIn("Guarded value must stay good.", self.lookup("--path", "guarded.py")[0])
        summary = self.compile()
        self.assertEqual(summary["epoch"], 2)
        self.assertEqual([row[0] for row in self.image("SELECT event FROM events")], ["added"])
        self.assertTrue((self.repo / ".coldrecall/reflex.json").is_file())
        self.assertIn("Guarded value must stay good.", self.lookup("--path", "guarded.py")[0])


class HookOptionTests(ImageCase):
    def setUp(self):
        super().setUp()
        self.compile()
        table = self.repo / ".coldrecall/reflex.json"
        data = json.loads(table.read_text())
        for name, text in (("curated:alpha", "Alpha keeps strings intact."), ("curated:beta", "Beta guards the zebra parser.")):
            data["atoms"][name] = dict(text=text, witness=None, proven=None)
            data["anchors"].setdefault("path:guarded.py", []).append(name)
        table.write_text(json.dumps(data))
        self.log = self.repo / ".coldrecall/log.jsonl"

    def hook(self, *options, **payload):
        event = dict(cwd=str(self.repo), session_id="s1", tool_input=dict(file_path=str(self.repo / "guarded.py")))
        result = run(sys.executable, str(CLI), "hook", "--log", str(self.log), *options, stdin=json.dumps(dict(event, **payload)))
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)["hookSpecificOutput"] if result.stdout else None

    def logged(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.is_file() else []

    def test_budget_keeps_the_regression_first_and_logs_what_was_delivered(self):
        everything = self.hook()["additionalContext"].splitlines()
        self.assertEqual(len(everything), 3)
        one = self.hook("--budget", "1")["additionalContext"].splitlines()
        self.assertEqual(len(one), 1)
        self.assertEqual([len(row["refs"]) for row in self.logged()], [3, 1])
        self.assertEqual(self.logged()[1]["chars"], len(one[0]))

    def test_log_records_injected_bytes_with_a_session_digest_and_time(self):
        context = self.hook(session_id="s1", prompt="é")["additionalContext"]
        self.hook(session_id="", hook_event_name="CustomEvent")
        first, blank = self.logged()
        self.assertEqual((first["event"], blank["event"]), ("PreToolUse", "other"))
        self.assertEqual(first["session"], hashlib.sha256(b"s1").hexdigest())
        self.assertEqual(first["bytes"], len(context.encode("utf-8")))
        self.assertIsNotNone(c.time_key(first["at"]).utcoffset())
        self.assertIsNone(blank["session"])

    def test_a_deployed_runtime_logs_injections_beside_itself_without_a_flag(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        runtime = Path(temporary.name) / "hook-runtime"
        code = runtime / "code"
        code.mkdir(parents=True)
        for name in ("coldrecall.py", "coldfix.py"):
            (code / name).write_bytes((ROOT / name).read_bytes())
        event = json.dumps(dict(cwd=str(self.repo), session_id="s1", tool_input=dict(file_path="guarded.py")))
        run(sys.executable, str(code / "coldrecall.py"), "hook", stdin=event)
        self.assertFalse((runtime / "injections.jsonl").exists())
        (runtime / "code.revision").write_text("abc installed now backup=\n")
        result = run(sys.executable, str(code / "coldrecall.py"), "hook", stdin=event)
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = [json.loads(line) for line in (runtime / "injections.jsonl").read_text().split("\n") if line]
        self.assertEqual([len(row["refs"]) for row in rows], [3])

    def test_an_unwritable_log_still_delivers(self):
        result = run(sys.executable, str(CLI), "hook", "--log", str(self.repo / "missing/log.jsonl"),
                     stdin=json.dumps(dict(cwd=str(self.repo), session_id="s1", tool_input=dict(file_path="guarded.py"))))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"].splitlines()), 3)

    def test_overlap_ranking_puts_the_fact_sharing_words_with_the_tool_call_first(self):
        first = self.hook("--ranking", "overlap", "--budget", "1", tool_input=dict(command="grep zebra parser guarded.py"))["additionalContext"]
        self.assertIn("zebra parser", first)
        plain = self.hook("--budget", "2")["additionalContext"].splitlines()
        self.assertNotIn("zebra", plain[0])

    def test_default_ranking_uses_task_text_without_dropping_path_candidates(self):
        refs, lines = recall.lookup(self.repo, ["guarded.py"], text="zebra parser", limit=1)
        self.assertEqual(refs, ["curated:beta"])
        self.assertIn("zebra parser", lines[0])
        plain, _ = recall.lookup(self.repo, ["guarded.py"])
        paraphrased, _ = recall.lookup(self.repo, ["guarded.py"], text="Fix line splitting when reading logs")
        self.assertEqual(set(paraphrased), set(plain))

    def test_hook_ranks_using_the_prompt_and_tool_arguments(self):
        output = self.hook("--budget", "1", prompt="zebra parser",
                           tool_input=dict(file_path="guarded.py"))
        self.assertIn("zebra parser", output["additionalContext"])
        output = self.hook("--budget", "1", prompt="repair it",
                           tool_input=dict(command="grep zebra parser guarded.py"))
        self.assertIn("zebra parser", output["additionalContext"])

    def test_once_delivers_each_fact_one_time_per_session_and_then_stays_silent(self):
        self.assertEqual(len(self.hook("--once")["additionalContext"].splitlines()), 3)
        self.assertIsNone(self.hook("--once"))
        self.assertEqual(len(self.hook("--once", session_id="s2")["additionalContext"].splitlines()), 3)
        self.assertEqual(len(self.logged()), 2)

    def test_once_keeps_distinct_session_ids_independent(self):
        for session in ("host/session", "host_session", "x" * 300):
            with self.subTest(session=session):
                output = self.hook("--once", session_id=session)
                self.assertIsNotNone(output)
                self.assertEqual(len(output["additionalContext"].splitlines()), 3)
                self.assertIsNone(self.hook("--once", session_id=session))

    def test_once_without_a_session_delivers_without_persistent_suppression(self):
        for session in (None, "", 42):
            with self.subTest(session=session):
                for _ in range(2):
                    output = self.hook("--once", session_id=session)
                    self.assertIsNotNone(output)
                    self.assertEqual(len(output["additionalContext"].splitlines()), 3)
        self.assertEqual(list((self.repo / ".coldrecall").glob("delivered-*")), [])

    def test_once_state_is_shared_between_the_repository_root_and_a_subdirectory(self):
        (self.repo / "sub").mkdir()
        self.assertEqual(len(self.hook("--once")["additionalContext"].splitlines()), 3)
        self.assertIsNone(self.hook("--once", cwd=str(self.repo / "sub"), tool_input=dict(file_path="../guarded.py")))

    def test_parallel_hook_calls_of_one_session_deliver_each_fact_once(self):
        event = json.dumps(dict(cwd=str(self.repo), session_id="s1", tool_input=dict(file_path=str(self.repo / "guarded.py"))))
        jobs = [subprocess.Popen([sys.executable, str(CLI), "hook", "--once", "--log", str(self.log)], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True) for _ in range(8)]
        for job in jobs:
            job.stdin.write(event)
            job.stdin.close()
        self.assertEqual([job.wait() for job in jobs], [0] * 8)
        self.assertEqual(sorted(ref for row in self.logged() for ref in row["refs"]), sorted(set(ref for row in self.logged() for ref in row["refs"])))
        self.assertEqual(sum(len(row["refs"]) for row in self.logged()), 3)

    def test_once_in_a_repository_without_facts_leaves_it_untouched(self):
        with tempfile.TemporaryDirectory() as plain:
            run("git", "init", "-q", plain)
            self.assertIsNone(self.hook("--once", cwd=plain, tool_input=dict(file_path="x.py")))
            self.assertEqual(os.listdir(plain), [".git"])

    def test_apply_patch_input_delivers_the_facts_of_every_file_it_touches(self):
        patch = "*** Begin Patch\n*** Update File: guarded.py\n@@\n-old\n+it's \"new\n*** End Patch"
        self.assertEqual(len(self.hook(tool_name="apply_patch", tool_input=dict(command=patch))["additionalContext"].splitlines()), 3)
        moved = "*** Begin Patch\n*** Update File: other.py\n*** Move to: guarded.py\n*** End Patch"
        self.assertEqual(len(self.hook(tool_input=dict(command=moved))["additionalContext"].splitlines()), 3)
        other = "*** Begin Patch\n*** Add File: other.py\n+guarded.py\n*** End Patch"
        self.assertIsNone(self.hook(tool_input=dict(command=other)))

    def test_session_start_and_prompt_events_deliver_every_fact_ranked_by_the_prompt(self):
        for event in ("SessionStart", "UserPromptSubmit"):
            with self.subTest(event):
                out = self.hook("--ranking", "overlap", "--budget", "2", hook_event_name=event, prompt="the zebra parser", tool_input=None)
                lines = out["additionalContext"].splitlines()
                self.assertEqual((out["hookEventName"], len(lines)), (event, 2))
                self.assertTrue(any("zebra" in line for line in lines))
        self.assertEqual([row["event"] for row in self.logged()], ["SessionStart", "UserPromptSubmit"])

    def test_no_log_is_written_when_nothing_matches(self):
        self.assertIsNone(self.hook(tool_input=dict(command="ls")))
        self.assertEqual(self.logged(), [])


class RealProofRecordTests(unittest.TestCase):
    def test_coldfix_own_unicode_lesson_compiles_and_answers_an_edit_to_ingest_code(self):
        probe = run("git", "-C", str(ROOT), "cat-file", "-e", "56ba960fc28706d19b2cb138270e96d379a760cc")
        if probe.returncode:
            self.skipTest("replay commits are not present in this checkout")
        result = run(sys.executable, str(CLI), "compile", "--repo", str(ROOT), cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual((len(summary["active"]), summary["regressions"], summary["unanchored"]), (6, [], []))
        lines = run(sys.executable, str(CLI), "lookup", "--repo", str(ROOT), "--path", "coldfix.py", "--text", "JSONL Unicode separators", cwd=ROOT).stdout.splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("Parse JSONL at newline record boundaries", lines[0])
        self.assertIn("witness passed", lines[0])

        paraphrased = run(sys.executable, str(CLI), "lookup", "--repo", str(ROOT),
                          "--path", "coldfix.py", "--text", "Fix line splitting when reading logs", cwd=ROOT)
        self.assertEqual(paraphrased.returncode, 0, paraphrased.stderr)
        self.assertIn("Parse JSONL at newline record boundaries", paraphrased.stdout)
