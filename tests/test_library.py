import contextlib
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import time
import unittest
from unittest import mock

LIBRARY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "library")
sys.path.insert(0, LIBRARY)
import rerun


def lessons():
    return sorted(name for name in os.listdir(LIBRARY) if os.path.isfile(os.path.join(LIBRARY, name, "rule.md")))


def frontmatter(slug):
    with open(os.path.join(LIBRARY, slug, "rule.md")) as stream:
        head = stream.read().split("---\n")[1]
    return dict(line.split(": ", 1) for line in head.splitlines())


class LibraryTests(unittest.TestCase):
    def test_proof_level_matches_the_shipped_proof(self):
        for slug in lessons():
            with self.subTest(slug):
                front = frontmatter(slug)
                self.assertEqual(front["id"], slug)
                self.assertTrue(front["title"])
                shipped = rerun.proof_kind(os.path.join(LIBRARY, slug))
                self.assertEqual(front["proof"], "judged" if shipped is None else "rerunnable")

    def test_index_lists_every_lesson_with_its_proof_level(self):
        with open(os.path.join(LIBRARY, "README.md")) as stream:
            rows = re.findall(r"^\| \[.+\]\(([^/]+)/rule\.md\) \| (\w+) \|$", stream.read(), re.M)
        self.assertEqual(sorted(rows), [(slug, frontmatter(slug)["proof"]) for slug in lessons()])

    def test_index_lists_every_portable_scan(self):
        with open(os.path.join(LIBRARY, "README.md")) as stream:
            listed = re.findall(r"^- \[([^\]]+)\]\(([^/]+)/scan\.py\): ", stream.read(), re.M)
        self.assertEqual(sorted(listed), [(slug, slug) for slug in lessons()
                                          if os.path.isfile(os.path.join(LIBRARY, slug, "scan.py"))])

    def test_fixture_proofs_fail_on_bad_and_pass_on_good(self):
        kinds = (rerun.fixture_proof, rerun.generated_proof)
        fixtures = [slug for slug in lessons() if rerun.proof_kind(os.path.join(LIBRARY, slug)) in kinds]
        self.assertTrue(fixtures)
        for slug in fixtures:
            with self.subTest(slug):
                lesson = os.path.join(LIBRARY, slug)
                self.assertTrue(rerun.proof_kind(lesson)(lesson))

    def test_conflict_marker_and_jsonl_lessons_rerun_on_generated_fixtures(self):
        with contextlib.redirect_stdout(io.StringIO()) as output, \
                mock.patch.object(rerun, "repository_proof", lambda lesson: True):
            code = rerun.main(["conflict-markers", "jsonl-unicode-separators"])
        self.assertEqual((code, output.getvalue()), (0, "PASS conflict-markers\nPASS jsonl-unicode-separators\n"))

    def test_a_generated_proof_whose_check_passes_on_bad_is_not_a_proof(self):
        with tempfile.TemporaryDirectory() as lesson:
            self.check_in(lesson, "print('{\"status\": \"PASS\"}')\n")
            with open(os.path.join(lesson, "fixtures.py"), "w") as stream:
                stream.write("FIXTURES = {'bad': {'a/b.txt': 'bad'}, 'good': {'a/b.txt': 'good'}}\n")
            self.assertIs(rerun.proof_kind(lesson), rerun.generated_proof)
            self.assertFalse(rerun.generated_proof(lesson))

    def test_a_crashing_check_is_not_a_proof(self):
        with tempfile.TemporaryDirectory() as lesson:
            for side in ("bad", "good"):
                os.makedirs(os.path.join(lesson, "fixtures", side))
            with open(os.path.join(lesson, "check.py"), "w") as stream:
                stream.write("import os\n"
                             "if os.path.basename(os.getcwd()) == 'good':\n"
                             "    print('{\"status\": \"PASS\"}')\n"
                             "else:\n"
                             "    raise RuntimeError('broken')\n")
            self.assertFalse(rerun.fixture_proof(lesson))

    def test_a_check_that_does_not_finish_is_incomplete(self):
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, "check.py"), "w") as stream:
                stream.write("import time\ntime.sleep(60)\n")
            timeout = rerun.TIMEOUT
            rerun.TIMEOUT = 1
            try:
                with self.assertRaises(rerun.Incomplete):
                    rerun.check_holds(os.path.join(root, "check.py"), root, 1)
            finally:
                rerun.TIMEOUT = timeout

    def check_in(self, root, source):
        with open(os.path.join(root, "check.py"), "w") as stream:
            stream.write(source)
        return os.path.join(root, "check.py")

    def test_a_timeout_also_ends_the_checks_own_children(self):
        with tempfile.TemporaryDirectory() as root:
            late = os.path.join(root, "late")
            child = f"import time; time.sleep(3); open({late!r}, 'w').close()"
            check = self.check_in(root, "import subprocess, sys, time\n"
                                        f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
                                        "time.sleep(60)\n")
            timeout = rerun.TIMEOUT
            rerun.TIMEOUT = 1
            try:
                with self.assertRaises(rerun.Incomplete):
                    rerun.check_holds(check, root, 1)
            finally:
                rerun.TIMEOUT = timeout
            time.sleep(5)
            self.assertFalse(os.path.exists(late))

    def test_output_that_is_not_utf8_fails_the_check(self):
        with tempfile.TemporaryDirectory() as root:
            check = self.check_in(root, "import sys\nsys.stdout.buffer.write(b'\\xff\\n')\nsys.exit(1)\n")
            self.assertFalse(rerun.check_holds(check, root, 1))

    def test_an_unknown_lesson_fails_the_rerun(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(rerun.main(["no-such-lesson"]), 1)
        self.assertEqual(output.getvalue(), "UNKNOWN no-such-lesson\n")

    def test_an_unreachable_repository_is_incomplete_and_the_rerun_continues(self):
        with tempfile.TemporaryDirectory() as root:
            lesson = os.path.join(root, "unreachable")
            os.makedirs(lesson)
            open(os.path.join(lesson, "rule.md"), "w").close()
            with open(os.path.join(lesson, "check.py"), "w") as stream:
                stream.write("")
            with open(os.path.join(lesson, "proof.json"), "w") as stream:
                json.dump({"repo": os.path.join(root, "missing"), "check_sha256": hashlib.sha256(b"").hexdigest(),
                           "before": {"commit": "0" * 40, "exit": 1}, "after": {"commit": "0" * 40, "exit": 0}}, stream)
            library = rerun.LIBRARY
            rerun.LIBRARY = root
            try:
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    code = rerun.main(["unreachable", "also-missing"])
            finally:
                rerun.LIBRARY = library
        self.assertEqual(code, 1)
        self.assertEqual(output.getvalue(), "INCOMPLETE unreachable\nUNKNOWN also-missing\n")

    def rerun_fixture_lesson(self, check_source):
        with tempfile.TemporaryDirectory() as root:
            lesson = os.path.join(root, "lesson")
            for side in ("bad", "good"):
                os.makedirs(os.path.join(lesson, "fixtures", side))
            open(os.path.join(lesson, "rule.md"), "w").close()
            with open(os.path.join(lesson, "check.py"), "w") as stream:
                stream.write(check_source)
            library = rerun.LIBRARY
            rerun.LIBRARY = root
            try:
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    code = rerun.main(["lesson", "also-missing"])
            finally:
                rerun.LIBRARY = library
        return code, output.getvalue()

    def test_a_fixture_lesson_also_reruns_its_repository_proof(self):
        with tempfile.TemporaryDirectory() as root:
            lesson = os.path.join(root, "lesson")
            for side in ("bad", "good"):
                os.makedirs(os.path.join(lesson, "fixtures", side))
            open(os.path.join(lesson, "rule.md"), "w").close()
            source = ("import os, sys\nbad = os.path.basename(os.getcwd()) == 'bad'\n"
                      "print('{\"status\": \"%s\"}' % ('FAIL' if bad else 'PASS'))\nsys.exit(1 if bad else 0)\n")
            self.check_in(lesson, source)
            with open(os.path.join(lesson, "proof.json"), "w") as stream:
                json.dump({"repo": os.path.join(root, "missing"), "check_sha256": hashlib.sha256(source.encode()).hexdigest(),
                           "before": {"commit": "0" * 40, "exit": 1}, "after": {"commit": "0" * 40, "exit": 0}}, stream)
            library = rerun.LIBRARY
            rerun.LIBRARY = root
            try:
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    code = rerun.main(["lesson"])
            finally:
                rerun.LIBRARY = library
            self.assertTrue(rerun.fixture_proof(lesson))
        self.assertEqual((code, output.getvalue()), (1, "INCOMPLETE lesson\n"))

    def test_an_incomplete_check_is_reported_as_incomplete(self):
        code, output = self.rerun_fixture_lesson("import sys\nprint('{\"status\": \"INCOMPLETE\"}')\nsys.exit(2)\n")
        self.assertEqual((code, output), (1, "INCOMPLETE lesson\nUNKNOWN also-missing\n"))

    def test_a_non_object_status_line_fails_the_proof_and_the_rerun_continues(self):
        code, output = self.rerun_fixture_lesson("import sys\nprint('null')\nsys.exit(1)\n")
        self.assertEqual((code, output), (1, "FAIL lesson\nUNKNOWN also-missing\n"))

    def test_repository_proofs_pin_a_github_repository_and_their_check(self):
        for slug in lessons():
            path = os.path.join(LIBRARY, slug, "proof.json")
            if not os.path.isfile(path):
                continue
            with self.subTest(slug):
                with open(path) as stream:
                    proof = json.load(stream)
                with open(os.path.join(LIBRARY, slug, "check.py"), "rb") as stream:
                    self.assertEqual(hashlib.sha256(stream.read()).hexdigest(), proof["check_sha256"])
                self.assertRegex(proof["repo"], r"^https://github\.com/[\w.-]+/[\w.-]+$")
                self.assertEqual((proof["before"]["exit"], proof["after"]["exit"]), (1, 0))
                for side in ("before", "after"):
                    self.assertRegex(proof[side]["commit"], r"^[0-9a-f]{40}$")


if __name__ == "__main__":
    unittest.main()
