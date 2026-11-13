import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def shell_blocks(section):
    readme = (ROOT / "README.md").read_text()
    match = re.search(r"^## " + re.escape(section) + r"\n(.*?)(?=^## |\Z)", readme, re.M | re.S)
    if match is None:
        raise AssertionError(f"README has no '## {section}' section")
    blocks = re.findall(r"^```sh\n(.*?)^```$", match.group(1), re.M | re.S)
    if not blocks:
        raise AssertionError(f"README section '{section}' has no sh block")
    return blocks


class ReadmeBlockTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        bin_dir = self.base / "bin"
        bin_dir.mkdir()
        (bin_dir / "python3").symlink_to(sys.executable)
        launcher = bin_dir / "coldfix"
        launcher.write_text(f"#!{sys.executable}\nimport sys\nsys.path.insert(0, {str(ROOT)!r})\n"
                            "from coldfix import main\nsys.exit(main())\n")
        launcher.chmod(0o755)
        (self.base / "home").mkdir()
        self.env = dict(HOME=str(self.base / "home"), PATH=f"{bin_dir}{os.pathsep}{os.defpath}", LANG="C.UTF-8")

    def run_section(self, section):
        work = self.base / re.sub(r"\W+", "-", section.lower())
        work.mkdir(exist_ok=True)
        for block in shell_blocks(section):
            result = subprocess.run(["sh", "-e", "-c", block], cwd=work, env=self.env, text=True,
                                    capture_output=True, timeout=300)
            self.assertEqual(result.returncode, 0, f"{section} block failed:\n{block}\n"
                             f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
            self.stdout = result.stdout
        return work

    def prepare_automatic_loop(self):
        bin_dir = self.base / "bin"
        gh = bin_dir / "gh"
        gh.write_text('#!/bin/sh\n[ -n "$GH_TOKEN" ] || { echo "gh: not logged in" >&2; exit 4; }\n'
                      '[ "$1 $2 $3" = "repo list example-owner" ] || exit 1\necho "[]"\n')
        model = bin_dir / "model"
        model.write_text('#!/bin/sh\ncat >/dev/null\necho \'{"score": 0.9}\'\n')
        for tool in (gh, model):
            tool.chmod(0o755)
        project = self.base / "home" / ".claude" / "projects" / "-home-user-app"
        project.mkdir(parents=True)
        rows = [dict(type="user", uuid="u-1", timestamp="2026-01-01T00:00:00Z", message=dict(role="user", content="Add the parser.")),
                dict(type="assistant", uuid="a-1", timestamp="2026-01-01T00:00:01Z",
                     message=dict(role="assistant", content=[dict(type="text", text="I wrote the parser.")])),
                dict(type="user", uuid="u-2", timestamp="2026-01-01T00:00:02Z",
                     message=dict(role="user", content="That is wrong: the test failed.")),
                dict(type="assistant", uuid="a-2", timestamp="2026-01-01T00:00:03Z",
                     message=dict(role="assistant", content=[dict(type="text", text="You're right, I fixed the import.")]))]
        (project / "session.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
        self.env.update(GITHUB_OWNER="example-owner", COLDFIX_MODEL_CMD=str(model))

    def test_check_your_repository_lists_the_uncollected_test(self):
        self.run_section("Check your repository")
        self.assertIn("FAIL: tests-must-test (A test must exercise the failure it names)\n  test_demo.py:8\n", self.stdout)
        self.assertIn("coldfix check exited 1\n", self.stdout)

    def test_first_run_exports_an_unproven_rule(self):
        work = self.run_section("First run")
        self.assertIn("Verification: unproven", (work / "exports" / "count-check.md").read_text())

    def test_prove_your_first_lesson_exports_a_proven_lesson(self):
        work = self.run_section("Prove your first lesson")
        exported = (work / "exports" / "first-lesson.md").read_text()
        self.assertIn("Verification: proven", exported)
        self.assertIn("Status: active", exported)

    def test_run_the_automatic_loop_finds_a_first_candidate(self):
        self.prepare_automatic_loop()
        self.env["GH_TOKEN"] = "example-token"
        self.run_section("Run the automatic loop")
        result = json.loads(self.stdout)
        self.assertEqual((result["backend"], result["candidates"], result["transcripts"]["jev_flagged"]), ("local", 1, 1))
        self.assertTrue(result["first_candidate"]["transcript"])
        self.assertNotIn("example-token", self.stdout)
        self.run_section("Run the automatic loop")
        self.assertEqual(json.loads(self.stdout)["transcripts"]["scanned"], 0)

    def test_run_the_automatic_loop_names_a_missing_github_token(self):
        self.prepare_automatic_loop()
        [block] = shell_blocks("Run the automatic loop")
        result = subprocess.run(["sh", "-e", "-c", block], cwd=self.base, env=self.env, text=True, capture_output=True, timeout=300)
        self.assertEqual(result.returncode, 2)
        self.assertIn("set GH_TOKEN", result.stderr)

    def test_run_the_automatic_loop_rejects_an_unrunnable_model_or_unreadable_runs(self):
        self.prepare_automatic_loop()
        [block] = shell_blocks("Run the automatic loop")
        cases = ((dict(GH_TOKEN="example-token", COLDFIX_MODEL_CMD=str(self.base / "missing-model")), "runnable model command"),
                 (dict(GH_TOKEN="example-token", GITHUB_OWNER="partial-owner"), "Actions read access"))
        repos = [dict(nameWithOwner="partial-owner/" + name, isPrivate=True) for name in ("app", "locked")]
        (self.base / "bin" / "gh").write_text(
            "#!/bin/sh\n"
            f"[ \"$1 $2\" != \"repo list\" ] || {{ echo '{json.dumps(repos)}'; exit 0; }}\n"
            "[ \"$1 $2 $4\" != \"run list github.com/partial-owner/locked\" ] || exit 1\n"
            "echo '[]'\n")
        for env, message in cases:
            with self.subTest(message):
                result = subprocess.run(["sh", "-e", "-c", block], cwd=self.base, env={**self.env, **env}, text=True,
                                        capture_output=True, timeout=300)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(message, result.stderr)


if __name__ == "__main__":
    unittest.main()
