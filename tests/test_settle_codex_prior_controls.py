"""Settle Codex finding lesson importer proofs. Synthetic repository, logs and GitHub responses; no network."""

import ast
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

import coldfix as c
import settle_codex_lessons as scl

BOT = "chatgpt-codex-connector[bot]"
BADGE = "**<sub><sub>![P2 Badge](https://img.shields.io/badge/P2-yellow?style=flat)</sub></sub>  {}**"
TEST = "import unittest\nimport {mod}\n\n\nclass T(unittest.TestCase):\n    def test_total(self):\n        {check}\n"


class SettleCodexLessonTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        self.projects = self.work / "projects"
        self.settle = self.work / "settle"
        self.scratch = self.work / "scratch"
        self.settle.mkdir()
        self.repo = self.projects / "proj"
        self.repo.mkdir(parents=True)
        self.store = self.work / ".coldfix"
        self.comments = {}
        self.replies = {}
        self.pulls = {}
        self.git("init", "-q", "-b", "main")
        self.git("remote", "add", "origin", "https://github.com/o/proj.git")
        self.base = self.commit("base", {"calc.py": "def total():\n    return 0\n"})

    def git(self, *args):
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
        return subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, text=True, check=True, env=env).stdout.strip()

    def commit(self, message, files, branch=None, executable=()):
        if branch:
            self.git("checkout", "-q", "-B", branch, self.base)
        for name, content in files.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            if name in executable:
                path.chmod(0o755)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def api(self, path):
        return {**self.pulls, **self.comments}.get(path, []) + self.replies.get(path, [])

    def matched(self, name):
        settle = scl.parse(self.settle / f"settle-{name}.out")
        self.assertEqual(scl.resolve(settle, self.projects, self.api), "")
        findings = scl.finding_text(settle, self.api)
        self.assertIsNotNone(findings)
        return findings

    def comment(self, number, head, title, text="Full finding text.", path="calc.py", ident=0, line=2,
                created_at="2026-10-31T09:59:00Z"):
        slug_path = f"repos/o/proj/pulls/{number}/comments?per_page=100"
        self.comments.setdefault(slug_path, []).append(dict(
            id=1000 + number + ident, user=dict(login=BOT), path=path, line=line,
            original_line=line, original_commit_id=head,
            body=f"<!-- codex-code-review-finding:v1 -->\n{BADGE.format(title)}\n\n{text}",
            html_url=f"https://github.com/o/proj/pull/{number}#discussion_r{1000 + number + ident}", created_at=created_at))
        names = set()
        for candidate in self.repo.glob("tests/**/test_*.py"):
            try:
                names.update(node.name for node in ast.walk(ast.parse(candidate.read_text()))
                             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"))
            except (SyntaxError, UnicodeError):
                continue
        if names:
            self.replies.setdefault(slug_path, []).append(dict(id=100000 + number * 100 + ident,
                in_reply_to_id=1000 + number + ident, user=dict(login="t"),
                body="Fixed and guarded by " + ", ".join(sorted(names))))

    def associate(self, number, *heads):
        for head in heads:
            self.pulls[f"repos/o/proj/commits/{head}/pulls?per_page=100"] = [dict(number=number)]

    def settle_out(self, name, head, minute, *, titles=(), slug=None, passed=True, merged=False, clean=False):
        start = f"start 2026-10-31T10:{minute:02d}:00+00:00" + (f" {name} {slug} {head}" if slug else "")
        lines = [start, f"pr: OPEN {head}"]
        if passed:
            lines.append(f"PASS: tests=3 head={head[:12]} pr_head={head[:12]} base=main@{self.base[:12]}")
        lines.append(f"log: {self.repo}/.git/verify-loop/{head[:12]}-{self.base[:12]}-aaaaaaaa.log")
        lines.append("codex: ok=summary-clean bad=" if clean else "codex: ok= bad=summary-findings")
        lines += [f"{title[0] if isinstance(title, tuple) else 'calc.py'}: {BADGE.format(title[1] if isinstance(title, tuple) else title)}\n\nTruncated body" for title in titles]
        if not clean:
            lines.append("no clean codex receipt at head")
        if merged:
            lines.append(f"MERGED {head}")
        finished = datetime(2026, 10, 31, 10, minute, tzinfo=timezone.utc).timestamp() + 60
        lines.append(f"end {datetime.fromtimestamp(finished, timezone.utc).isoformat()}")
        path = self.settle / f"settle-{name}.out"
        path.write_text("\n".join(lines) + "\n")
        os.utime(path, (finished, finished))

    def build(self):
        calc = lambda value: {"calc.py": f"def total():\n    return {value}\n"}
        fixed = lambda value, mod, check, extra=None: {**calc(value), f"tests/test_{mod}.py": TEST.format(mod="calc", check=check), **(extra or {})}
        heads = {}

        heads["bad1"] = self.commit("pr1 bad", calc(3), "pr1")
        heads["fix1"] = self.commit("pr1 fix", fixed(4, "calc1", "self.assertEqual(calc.total(), 4)"))
        self.comment(1, heads["bad1"], "Return the corrected total", "The total is wrong because it stops at three.")
        self.associate(1, heads["bad1"], heads["fix1"])
        self.settle_out("p1a", heads["bad1"], 0, titles=["Return the corrected total"])
        self.settle_out("p1b", heads["fix1"], 20, clean=True, merged=True)

        heads["bad2"] = self.commit("pr2 bad", calc(5), "pr2")
        heads["fix2"] = self.commit("pr2 fix", fixed(5, "calc2", "self.assertTrue(calc.total())"))
        self.comment(2, heads["bad2"], "Cover the total")
        self.settle_out("p2a", heads["bad2"], 1, titles=["Cover the total"], slug="o/proj#2")
        self.settle_out("p2b", heads["fix2"], 21, clean=True, merged=True, slug="o/proj#2")

        heads["bad3"] = self.commit("pr3 bad", calc(6), "pr3")
        self.comment(3, heads["bad3"], "Nobody fixed this")
        self.settle_out("p3a", heads["bad3"], 2, titles=["Nobody fixed this"], slug="o/proj#3")

        heads["bad4"] = self.commit("pr4 bad", calc(7), "pr4")
        heads["fix4"] = self.commit("pr4 fix", fixed(8, "calc4", "with self.subTest(case=1):\n            self.assertEqual(calc.total(), 8)"))
        self.comment(4, heads["bad4"], "Report eight", "Eight is the expected total.")
        self.settle_out("p4a", heads["bad4"], 3, titles=["Report eight"], slug="o/proj#4")
        self.settle_out("p4b", heads["fix4"], 23, passed=True, merged=False, titles=["Another finding"], slug="o/proj#4")

        heads["bad5"] = self.commit("pr5 bad", calc(9), "pr5")
        heads["fix5"] = self.commit("pr5 fix", fixed(10, "calc5", "self.assertEqual(calc.total(), 10)"))
        self.comment(5, heads["bad5"], "Return ten")
        self.settle_out("p5a", heads["bad5"], 4, titles=["Return ten"], slug="o/proj#5")
        self.settle_out("p5b", heads["fix5"], 24, passed=False, slug="o/proj#5")

        heads["bad6"] = self.commit("pr6 bad", calc(11), "pr6")
        self.settle_out("p6a", heads["bad6"], 5, slug="o/proj#6")

        heads["bad7"] = self.commit("pr7 bad", calc(12), "pr7")
        heads["fix7"] = self.commit("pr7 fix", fixed(13, "calc7", "self.assertEqual(calc.total(), 13)"))
        self.settle_out("p7a", heads["bad7"], 6, titles=["Finding GitHub no longer has"], slug="o/proj#7")
        self.settle_out("p7b", heads["fix7"], 26, clean=True, merged=True, slug="o/proj#7")

        heads["bad8"] = self.commit("pr8 bad", calc(14), "pr8")
        heads["fix8"] = self.commit("pr8 fix", {"newmod.py": "VALUE = 1\n", "tests/test_new.py": TEST.format(mod="newmod", check="self.assertEqual(newmod.VALUE, 1)")})
        self.comment(8, heads["bad8"], "Add the module")
        self.settle_out("p8a", heads["bad8"], 7, titles=["Add the module"], slug="o/proj#8")
        self.settle_out("p8b", heads["fix8"], 27, clean=True, merged=True, slug="o/proj#8")

        return heads

    def run_import(self, **kwargs):
        return scl.run(self.store, self.projects, self.settle, self.scratch, api=self.api, **kwargs)

    def test_proven_pairs_become_lessons_and_every_skip_records_its_reason(self):
        heads = self.build()
        result = self.run_import()
        self.assertEqual((result["candidates"], result["pairs"], result["proven"], result["ingested"]), (8, 5, 2, 2))
        self.assertEqual({row["source"]: row["reason"] for row in result["skipped"]}, {
            "settle-p2a.out": "no-failing-replay",
            "settle-p3a.out": "no-later-settle",
            "settle-p4b.out": "no-later-settle",
            "settle-p5a.out": "later-not-passed",
            "settle-p7a.out": "finding-text-missing",
            "settle-p8a.out": "no-failing-replay"})
        self.assertEqual(sorted(result["sources"]), [
            f"settle-codex:o/proj:1:{heads['bad1'][:12]}:{heads['fix1'][:12]}",
            f"settle-codex:o/proj:4:{heads['bad4'][:12]}:{heads['fix4'][:12]}"])
        rows = {row["source_ref"]: row for row in c.load_store(self.store)[1]["failures"].values()}
        row = rows[f"settle-codex:o/proj:1:{heads['bad1'][:12]}:{heads['fix1'][:12]}"]
        self.assertEqual((row["kind"], row["repo"], row["thread"]), ("review", "o/proj", "https://github.com/o/proj/pull/1"))
        lesson = json.loads(row["error"])
        self.assertEqual(lesson["bad_head"], heads["bad1"])
        self.assertEqual(lesson["fixed_head"], heads["fix1"])
        self.assertEqual(lesson["findings"][0]["severity"], "P2")
        self.assertEqual(lesson["findings"][0]["title"], "Return the corrected total")
        self.assertIn("stops at three", lesson["findings"][0]["text"])
        self.assertEqual(lesson["findings"][0]["url"], "https://github.com/o/proj/pull/1#discussion_r1001")
        self.assertIn("-    return 3", lesson["diff"]["patch"])
        self.assertIn("+    return 4", lesson["diff"]["patch"])
        self.assertEqual(lesson["fixing_settle"]["outcome"], "merged")
        self.assertEqual(lesson["replay"]["files"], ["tests/test_calc1.py"])
        self.assertEqual(lesson["replay"]["tests"], [dict(id="tests.test_calc1.T.test_total", bad="fail", fixed="ok")])
        self.assertIn("AssertionError", lesson["replay"]["bad_output"])
        passed = json.loads(rows[f"settle-codex:o/proj:4:{heads['bad4'][:12]}:{heads['fix4'][:12]}"]["error"])
        self.assertEqual(passed["fixing_settle"]["outcome"], "passed")

    def test_same_title_on_different_files_keeps_each_findings_own_comment(self):
        module = lambda value: f"def run():\n    return {value}\n"
        bad = self.commit("pr9 bad", {"calc.py": module(15), "parser.py": module(1), "writer.py": module(2)}, "pr9")
        fix = self.commit("pr9 fix", {"calc.py": module(16), "parser.py": module(3), "writer.py": module(4),
                                      "tests/test_calc9.py": TEST.format(mod="calc", check="self.assertEqual(calc.run(), 16)")})
        self.comment(9, bad, "Handle the empty case", "Problem in the parser.", path="parser.py")
        self.comment(9, bad, "Handle the empty case", "Problem in the writer.", path="writer.py", ident=1)
        self.associate(9, bad, fix)
        self.settle_out("p9a", bad, 8, titles=[("parser.py", "Handle the empty case"), ("writer.py", "Handle the empty case")], slug="o/proj#9")
        self.settle_out("p9b", fix, 28, clean=True, merged=True, slug="o/proj#9")
        findings = self.matched("p9a")
        self.assertEqual([f["path"] for f in findings], ["parser.py", "writer.py"])
        self.assertTrue(findings[0]["text"].endswith("Problem in the parser."))
        self.assertTrue(findings[1]["text"].endswith("Problem in the writer."))
        self.assertEqual([f["url"][-4:] for f in findings], ["1009", "1010"])
        self.assertEqual(self.run_import()["ingested"], 0)

    def test_two_settles_of_one_pair_coalesce_and_a_later_repeat_is_ignored(self):
        bad = self.commit("pr10 bad", {"calc.py": "def total():\n    return 17\n"}, "pr10")
        fix = self.commit("pr10 fix", {"calc.py": "def total():\n    return 18\n",
                                       "tests/test_calc10.py": TEST.format(mod="calc", check="self.assertEqual(calc.total(), 18)")})
        self.comment(10, bad, "Return eighteen")
        self.associate(10, bad, fix)
        self.settle_out("p10a", bad, 9, titles=["Return eighteen"], slug="o/proj#10")
        self.settle_out("p10b", bad, 10, titles=["Return eighteen"], slug="o/proj#10")
        self.settle_out("p10c", fix, 30, clean=True, merged=True, slug="o/proj#10")
        first = self.run_import()
        self.assertEqual((first["proven"], first["ingested"]), (1, 1))
        self.settle_out("p10d", bad, 11, titles=["Return eighteen"], slug="o/proj#10")
        again = self.run_import()
        self.assertEqual((again["proven"], again["ingested"], again["already_present"]), (1, 0, 1))
        self.assertEqual(len(c.load_store(self.store)[1]["failures"]), 1)

    def test_nested_test_modules_are_replayed_by_package_path(self):
        bad = self.commit("pr11 bad", {"calc.py": "def total():\n    return 19\n"}, "pr11")
        fix = self.commit("pr11 fix", {"calc.py": "def total():\n    return 20\n",
                                       "tests/unit/test_calc11.py": TEST.format(mod="calc", check="self.assertEqual(calc.total(), 20)")})
        self.comment(11, bad, "Return twenty")
        self.associate(11, bad, fix)
        self.settle_out("p11a", bad, 12, titles=["Return twenty"], slug="o/proj#11")
        self.settle_out("p11b", fix, 32, clean=True, merged=True, slug="o/proj#11")
        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"]), (1, 1))
        row = next(iter(c.load_store(self.store)[1]["failures"].values()))
        self.assertEqual(json.loads(row["error"])["replay"]["tests"],
                         [dict(id="tests.unit.test_calc11.T.test_total", bad="fail", fixed="ok")])

    def test_a_later_passing_settle_is_tried_when_the_nearest_one_has_no_proof(self):
        bad = self.commit("pr12 bad", {"calc.py": "def total():\n    return 21\n"}, "pr12")
        docs = self.commit("pr12 docs", {"README.md": "notes\n"})
        fix = self.commit("pr12 fix", {"calc.py": "def total():\n    return 22\n",
                                       "tests/test_calc12.py": TEST.format(mod="calc", check="self.assertEqual(calc.total(), 22)")})
        self.comment(12, bad, "Return twenty-two")
        self.associate(12, bad, docs, fix)
        self.settle_out("p12a", bad, 13, titles=["Return twenty-two"], slug="o/proj#12")
        self.settle_out("p12b", docs, 33, clean=True, slug="o/proj#12")
        self.settle_out("p12c", fix, 34, clean=True, merged=True, slug="o/proj#12")
        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"]), (1, 1))
        row = next(iter(c.load_store(self.store)[1]["failures"].values()))
        self.assertEqual(json.loads(row["error"])["fixed_head"], fix)

    def test_only_findings_the_fix_touched_are_labeled_fixed(self):
        source = lambda a, b: f"def first():\n    return {a}\n\n\ndef second():\n    return {b}\n"
        bad = self.commit("pr13 bad", {"calc.py": source(1, 2)}, "pr13")
        fix = self.commit("pr13 fix", {"calc.py": source(3, 2),
                                       "tests/test_calc13.py": TEST.format(mod="calc", check="self.assertEqual(calc.first(), 3)")})
        self.comment(13, bad, "Fix the first function", "The first is wrong.", line=2)
        self.comment(13, bad, "Fix the second function", "The second is wrong.", line=6, ident=1)
        self.associate(13, bad, fix)
        self.settle_out("p13a", bad, 14, titles=["Fix the first function", "Fix the second function"], slug="o/proj#13")
        self.settle_out("p13b", fix, 36, clean=True, merged=True, slug="o/proj#13")
        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"]), (1, 1))
        row = next(iter(c.load_store(self.store)[1]["failures"].values()))
        lesson = json.loads(row["error"])
        self.assertEqual([f["title"] for f in lesson["findings"]], ["Fix the first function"])
        self.assertEqual([f["title"] for f in lesson["unverified_findings"]], ["Fix the second function"])

    def test_a_fix_that_touches_no_reported_code_is_not_a_lesson(self):
        bad = self.commit("pr14 bad", {"calc.py": "def total():\n    return 23\n", "other.py": "VALUE = 1\n"}, "pr14")
        fix = self.commit("pr14 fix", {"other.py": "VALUE = 2\n",
                                       "tests/test_other14.py": TEST.format(mod="other", check="self.assertEqual(other.VALUE, 2)")})
        self.comment(14, bad, "Return twenty-four")
        self.associate(14, bad, fix)
        self.settle_out("p14a", bad, 15, titles=["Return twenty-four"], slug="o/proj#14")
        self.settle_out("p14b", fix, 38, clean=True, merged=True, slug="o/proj#14")
        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"]), (0, 0))
        self.assertEqual([row["reason"] for row in result["skipped"]], ["finding-not-covered"])

    def test_a_fixture_added_by_the_fix_is_staged_before_the_failing_run(self):
        bad = self.commit("pr15 bad", {"calc.py": "def total():\n    return 2\n"}, "pr15")
        fix = self.commit("pr15 fix", {"calc.py": "def total():\n    return 1 + 1\n",
                                       "tests/expected.txt": "2\n",
                                       "tests/test_calc15.py": TEST.format(mod="calc", check="self.assertEqual(calc.total(), int(open('tests/expected.txt').read()))")})
        self.comment(15, bad, "Compute the total")
        self.associate(15, bad, fix)
        self.settle_out("p15a", bad, 16, titles=["Compute the total"], slug="o/proj#15")
        self.settle_out("p15b", fix, 39, clean=True, merged=True, slug="o/proj#15")
        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"]), (0, 0))
        self.assertEqual([row["reason"] for row in result["skipped"]], ["no-failing-replay"])

    def test_a_fixture_replacing_a_file_with_a_directory_is_staged(self):
        bad = self.commit("pr26 bad", {"calc.py": "def total():\n    return 1\n",
                                        "tests/data": "old fixture\n"}, "pr26")
        (self.repo / "tests/data").unlink()
        fix = self.commit("pr26 fix", {"calc.py": "def total():\n    return 2\n",
                                       "tests/data/expected.txt": "2\n",
                                       "tests/test_calc26.py": TEST.format(mod="calc", check="self.assertEqual(calc.total(), int(open('tests/data/expected.txt').read()))")})
        self.comment(26, bad, "Compute the total")
        self.associate(26, bad, fix)
        self.settle_out("p26a", bad, 16, titles=["Compute the total"], slug="o/proj#26")
        self.settle_out("p26b", fix, 39, clean=True, merged=True, slug="o/proj#26")
        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"]), (1, 1))

    def test_a_fixture_replacing_a_directory_with_a_file_is_staged(self):
        bad = self.commit("pr27 bad", {"calc.py": "def total():\n    return 1\n",
                                        "tests/data/old.txt": "old fixture\n"}, "pr27")
        (self.repo / "tests/data/old.txt").unlink()
        (self.repo / "tests/data").rmdir()
        fix = self.commit("pr27 fix", {"calc.py": "def total():\n    return 2\n",
                                       "tests/data": "2\n",
                                       "tests/test_calc27.py": TEST.format(mod="calc", check="self.assertEqual(calc.total(), int(open('tests/data').read()))")})
        self.comment(27, bad, "Compute the total")
        self.associate(27, bad, fix)
        self.settle_out("p27a", bad, 16, titles=["Compute the total"], slug="o/proj#27")
        self.settle_out("p27b", fix, 39, clean=True, merged=True, slug="o/proj#27")
        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"]), (1, 1))

    def test_a_symlinked_scratch_root_is_preserved_during_replay(self):
        bad = self.commit("pr28 bad", {"calc.py": "def total():\n    return 1\n"}, "pr28")
        fix = self.commit("pr28 fix", {"calc.py": "def total():\n    return 2\n",
                                       "tests/test_calc28.py": TEST.format(mod="calc", check="self.assertEqual(calc.total(), 2)")})
        self.comment(28, bad, "Compute the total")
        self.associate(28, bad, fix)
        self.settle_out("p28a", bad, 16, titles=["Compute the total"], slug="o/proj#28")
        self.settle_out("p28b", fix, 39, clean=True, merged=True, slug="o/proj#28")
        self.scratch.mkdir()
        real_scratch = self.work / "scratch-target"
        self.scratch.rename(real_scratch)
        self.scratch.symlink_to(real_scratch, target_is_directory=True)
        result = self.run_import()
        self.assertTrue(self.scratch.is_symlink())
        self.assertEqual((result["proven"], result["ingested"]), (1, 1))

    def test_a_fixture_outside_the_test_directories_is_not_proof_when_missing_at_the_bad_head(self):
        bad = self.commit("pr22 bad", {"calc.py": "def total():\n    return 2\n"}, "pr22")
        fix = self.commit("pr22 fix", {"calc.py": "def total():\n    return 1 + 1\n",
                                       "expected.json": "2\n",
                                       "tests/test_calc22.py": TEST.format(mod="calc", check="self.assertEqual(calc.total(), int(open('expected.json').read()))")})
        self.comment(22, bad, "Compute the total")
        self.associate(22, bad, fix)
        self.settle_out("p22a", bad, 16, titles=["Compute the total"], slug="o/proj#22")
        self.settle_out("p22b", fix, 39, clean=True, merged=True, slug="o/proj#22")
        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"]), (0, 0))
        self.assertEqual([row["reason"] for row in result["skipped"]], ["no-failing-replay"])

    def test_a_missing_fixture_is_not_proof_when_scratch_is_a_symlink(self):
        bad = self.commit("pr42 bad", {"calc.py": "def total():\n    return 2\n"}, "pr42")
        fix = self.commit("pr42 fix", {"calc.py": "def total():\n    return 1 + 1\n",
                                       "expected.json": "2\n",
                                       "tests/test_calc42.py": TEST.format(mod="calc", check="self.assertEqual(calc.total(), int(open('expected.json').read()))")})
        self.comment(42, bad, "Compute the total")
        self.associate(42, bad, fix)
        self.settle_out("p42a", bad, 16, titles=["Compute the total"], slug="o/proj#42")
        self.settle_out("p42b", fix, 39, clean=True, merged=True, slug="o/proj#42")
        real_scratch = self.work / "scratch-target"
        real_scratch.mkdir()
        self.scratch.symlink_to(real_scratch, target_is_directory=True)

        result = self.run_import()
        self.assertTrue(self.scratch.is_symlink())
        self.assertEqual((result["proven"], result["ingested"]), (0, 0))
        self.assertEqual([row["reason"] for row in result["skipped"]], ["no-failing-replay"])

    def test_a_missing_application_file_error_remains_unverified(self):
        bad = self.commit("pr29 bad", {"calc.py": "from pathlib import Path\ndef total():\n    return Path('data/value.txt').read_text().strip()\n"}, "pr29")
        fix = self.commit("pr29 fix", {"calc.py": "from pathlib import Path\ndef total():\n    return Path('data/answer.txt').read_text().strip()\n",
                                       "data/answer.txt": "42\n",
                                       "tests/test_calc29.py": TEST.format(mod="calc", check="self.assertEqual(calc.total(), '42')")})
        self.comment(29, bad, "Restore the missing application data", path="calc.py", line=3)
        self.associate(29, bad, fix)
        self.settle_out("p29a", bad, 16, titles=[("calc.py", "Restore the missing application data")], slug="o/proj#29")
        self.settle_out("p29b", fix, 39, clean=True, merged=True, slug="o/proj#29")
        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"]), (0, 0))
        self.assertEqual(result["skipped"][0]["reason"], "no-failing-replay")

    def test_a_fixture_whose_name_has_a_space_is_staged_without_aborting_the_batch(self):
        bad = self.commit("pr23 bad", {"calc.py": "def total():\n    return 2\n"}, "pr23")
        fix = self.commit("pr23 fix", {"calc.py": "def total():\n    return 1 + 2\n",
                                       "tests/expected value.txt": "3\n",
                                       "tests/test_calc23.py": TEST.format(mod="calc", check="self.assertEqual(calc.total(), 3)\n        self.assertEqual(open('tests/expected value.txt').read(), '3\\n')")})
        self.comment(23, bad, "Compute the total")
        self.associate(23, bad, fix)
        self.settle_out("p23a", bad, 16, titles=["Compute the total"], slug="o/proj#23")
        self.settle_out("p23b", fix, 39, clean=True, merged=True, slug="o/proj#23")
        result = self.run_import()
        self.assertEqual(result["skipped"], [])

    def test_an_executable_helper_added_by_the_fix_keeps_its_permissions(self):
        bad = self.commit("pr16 bad", {"calc.py": "def total():\n    return 2\n"}, "pr16")
        fix = self.commit("pr16 fix", {"calc.py": "def total():\n    return 1 + 1\n",
                                       "tests/helper.sh": "#!/bin/sh\nexit 0\n",
                                       "tests/test_calc16.py": TEST.format(mod="calc", check="__import__('subprocess').run(['./tests/helper.sh'], check=True)")},
                          executable=("tests/helper.sh",))
        self.comment(16, bad, "Compute the sum")
        self.associate(16, bad, fix)
        self.settle_out("p16a", bad, 17, titles=["Compute the sum"], slug="o/proj#16")
        self.settle_out("p16b", fix, 40, clean=True, merged=True, slug="o/proj#16")
        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"]), (0, 0))
        self.assertEqual([row["reason"] for row in result["skipped"]], ["no-failing-replay"])

    def test_a_pr_url_names_the_pr_when_the_start_line_has_no_pr_metadata(self):
        bad = self.commit("pr17 bad", {"calc.py": "def total():\n    return 3\n"}, "pr17")
        fix = self.commit("pr17 fix", {"calc.py": "def total():\n    return 4\n",
                                       "tests/test_calc17.py": TEST.format(mod="calc", check="self.assertEqual(calc.total(), 4)")})
        self.comment(17, bad, "Return four")
        self.associate(17, fix)
        self.pulls[f"repos/o/proj/commits/{bad}/pulls?per_page=100"] = [dict(number=17), dict(number=18)]
        self.settle_out("p17a", bad, 18, titles=["Return four"])
        with (self.settle / "settle-p17a.out").open("a") as log:
            log.write("https://github.com/o/proj/pull/17\n")
        self.settle_out("p17b", fix, 41, clean=True, merged=True, slug="o/proj#17")
        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"]), (1, 1))

    def test_a_fixture_deleted_by_the_fix_is_removed_before_the_failing_run(self):
        bad = self.commit("pr18 bad", {"calc.py": "def total():\n    return 2\n", "tests/stale.txt": "old\n"}, "pr18")
        (self.repo / "tests/stale.txt").unlink()
        fix = self.commit("pr18 fix", {"calc.py": "def total():\n    return 1 + 1\n",
                                       "tests/test_calc18.py": TEST.format(mod="calc", check="self.assertFalse(__import__('os').path.exists('tests/stale.txt'))")})
        self.comment(18, bad, "Remove the stale fixture", line=2)
        self.associate(18, bad, fix)
        self.settle_out("p18a", bad, 19, titles=["Remove the stale fixture"], slug="o/proj#18")
        self.settle_out("p18b", fix, 42, clean=True, merged=True, slug="o/proj#18")
        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"]), (0, 0))
        self.assertEqual([row["reason"] for row in result["skipped"]], ["no-failing-replay"])

    def pair_of_two_functions(self, number, minute, second_created_at="2026-10-31T09:59:00Z"):
        source = lambda a, b: f"def first():\n    return {a}\n\n\n\ndef second():\n    return {b}\n"
        bad = self.commit(f"pr{number} bad", {"calc.py": source(1, 2)}, f"pr{number}")
        fix = self.commit(f"pr{number} fix", {"calc.py": source(3, 4),
                                              f"tests/test_calc{number}.py": TEST.format(mod="calc", check="self.assertEqual(calc.first(), 3)")})
        self.comment(number, bad, "Fix the first function", line=2)
        self.comment(number, bad, "Fix the second function", line=7, ident=1, created_at=second_created_at)
        self.associate(number, bad, fix)
        slug = f"o/proj#{number}"
        self.settle_out(f"p{number}a", bad, minute, titles=["Fix the first function"], slug=slug)
        return bad, fix, slug

    def test_findings_from_repeated_settles_of_one_pair_are_merged(self):
        bad, fix, slug = self.pair_of_two_functions(19, 20, second_created_at="2026-10-31T10:20:30Z")
        self.settle_out("p19b", bad, 21, titles=["Fix the second function"], slug=slug)
        self.settle_out("p19c", fix, 43, clean=True, merged=True, slug=slug)
        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"]), (1, 1))
        lesson = json.loads(next(iter(c.load_store(self.store)[1]["failures"].values()))["error"])
        self.assertEqual([f["title"] for f in lesson["findings"]], ["Fix the first function"])
        self.assertEqual([f["title"] for f in lesson["unverified_findings"]], ["Fix the second function"])

    def test_comment_posted_during_settle_is_eligible_for_that_report(self):
        bad, fix, slug = self.pair_of_two_functions(21, 0, second_created_at="2026-10-31T10:00:30Z")
        self.settle_out("p21b", bad, 0, titles=["Fix the second function"], slug=slug)
        self.settle_out("p21c", fix, 43, clean=True, merged=True, slug=slug)
        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"]), (1, 1))

    def test_a_finding_first_seen_after_the_pair_was_stored_is_reported(self):
        bad, fix, slug = self.pair_of_two_functions(20, 22)
        self.settle_out("p20c", fix, 44, clean=True, merged=True, slug=slug)
        self.assertEqual(self.run_import()["ingested"], 1)
        self.settle_out("p20b", bad, 23, titles=["Fix the second function"], slug=slug)
        again = self.run_import()
        self.assertEqual((again["ingested"], again["already_present"]), (0, 1))
        self.assertEqual([row["reason"] for row in again["skipped"]], ["findings-not-in-stored-lesson"])

    def test_a_replay_that_never_finishes_is_skipped_and_the_batch_continues(self):
        hang = self.commit("pr21 bad", {"calc.py": "def total():\n    __import__('time').sleep(20)\n    return 2\n"}, "pr21")
        hang_fix = self.commit("pr21 fix", {"calc.py": "def total():\n    return 1 + 1\n",
                                            "tests/test_calc21.py": TEST.format(mod="calc", check="self.assertEqual(calc.total(), 2)")})
        self.comment(21, hang, "Stop retrying forever")
        self.associate(21, hang, hang_fix)
        self.settle_out("p21a", hang, 24, titles=["Stop retrying forever"], slug="o/proj#21")
        self.settle_out("p21b", hang_fix, 45, clean=True, merged=True, slug="o/proj#21")
        bad, fix, slug = self.pair_of_two_functions(22, 25)
        self.settle_out("p22c", fix, 46, clean=True, merged=True, slug=slug)
        with mock.patch.object(scl, "REPLAY_SECONDS", 2):
            result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"]), (1, 1))
        self.assertEqual([row["reason"] for row in result["skipped"]], ["replay-timeout"])

    def carried_pair(self, number, minute, fresh):
        calc = lambda value: {"calc.py": f"def total():\n    return {value}\n"}
        first = self.commit(f"pr{number} first", calc(25), f"pr{number}")
        bad = self.commit(f"pr{number} bad", {**calc(25), "notes.md": "unrelated\n"})
        fix = self.commit(f"pr{number} fix", {**calc(26), "notes.md": "unrelated\n",
                                            f"tests/test_calc{number}.py": TEST.format(mod="calc", check="self.assertEqual(calc.total(), 26)")})
        self.comment(number, first, "Return twenty-six", "Carried finding text.")
        titles = ["Return twenty-six"]
        if fresh:
            self.comment(number, bad, "Mention the notes", "Fresh finding text.", ident=1)
            titles.append("Mention the notes")
        self.associate(number, first, bad, fix)
        slug = f"o/proj#{number}"
        self.settle_out(f"p{number}a", bad, minute, titles=titles, slug=slug)
        self.settle_out(f"p{number}b", fix, minute + 20, clean=True, merged=True, slug=slug)
        return bad

    def test_a_finding_the_settle_only_carried_over_is_not_credited_to_the_fix(self):
        self.carried_pair(23, 26, fresh=False)
        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"]), (0, 0))
        self.assertEqual([row["reason"] for row in result["skipped"]], ["finding-not-covered"])

    def test_a_reposted_carried_finding_is_not_reclassified_as_fresh(self):
        bad = self.carried_pair(25, 28, fresh=False)
        self.comment(25, bad, "Return twenty-six", "Reposted after the stopped settle.", ident=1,
                     created_at="2026-10-31T10:29:01Z")
        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"]), (0, 0))
        self.assertEqual([row["reason"] for row in result["skipped"]], ["finding-not-covered"])

    def test_a_same_text_repost_before_completion_keeps_the_earlier_attribution(self):
        bad = self.carried_pair(30, 30, fresh=False)
        self.comment(30, bad, "Return twenty-six", "Carried finding text.", ident=1,
                     created_at="2026-10-31T10:30:30Z")
        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"]), (0, 0))
        self.assertEqual([row["reason"] for row in result["skipped"]], ["finding-not-covered"])

    def test_a_carried_finding_stays_unverified_when_its_line_shifts(self):
        source = lambda value: {"calc.py": f"def total():\n    return {value}\n"}
        prior = self.commit("pr35 prior", source(1), "pr35")
        bad = self.commit("pr35 bad", {"calc.py": "# moved down\n" + next(iter(source(1).values()))})
        fix = self.commit("pr35 fix", {"calc.py": "# moved down\ndef total():\n    return 2\n",
                                        "tests/test_calc35.py": TEST.format(mod="calc", check="self.assertEqual(calc.total(), 2)")})
        self.comment(35, prior, "Return two", "The returned total has the wrong value.", line=2,
                     created_at="2026-10-31T10:34:00Z")
        self.comment(35, bad, "Return two", "The total value returned here is incorrect.", ident=1, line=3,
                     created_at="2026-10-31T10:35:00Z")
        self.associate(35, prior, bad, fix)
        self.settle_out("p35a", bad, 35, titles=["Return two"], slug="o/proj#35")
        self.settle_out("p35b", fix, 55, clean=True, merged=True, slug="o/proj#35")

        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"]), (0, 0))
        self.assertEqual([row["reason"] for row in result["skipped"]], ["finding-not-covered"])

    def test_an_identical_short_description_survives_a_line_shift(self):
        prior = self.commit("pr40 prior", {"calc.py": "def total():\n    return 1\n"}, "pr40")
        bad = self.commit("pr40 bad", {"calc.py": "# shifted\ndef total():\n    return 1\n"})
        fix = self.commit("pr40 fix", {"calc.py": "# shifted\ndef total():\n    return 2\n",
                                        "tests/test_calc40.py": TEST.format(mod="calc", check="self.assertEqual(calc.total(), 2)")})
        text = "This is incorrect."
        self.comment(40, prior, "Return two", text, line=2, created_at="2026-10-31T10:44:00Z")
        self.comment(40, bad, "Return two", text, ident=1, line=3, created_at="2026-10-31T10:45:00Z")
        self.associate(40, prior, bad, fix)
        self.settle_out("p40a", bad, 45, titles=["Return two"], slug="o/proj#40")
        self.settle_out("p40b", fix, 55, clean=True, merged=True, slug="o/proj#40")

        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"]), (0, 0))
        self.assertEqual([row["reason"] for row in result["skipped"]], ["finding-not-covered"])

    def test_a_carried_module_level_finding_stays_unverified_when_its_line_shifts(self):
        prior = self.commit("pr36 prior", {"calc.py": "VALUE = 1\n"}, "pr36")
        bad = self.commit("pr36 bad", {"calc.py": "# moved down\nVALUE = 1\n"})
        fix = self.commit("pr36 fix", {"calc.py": "# moved down\nVALUE = 2\n",
                                        "tests/test_calc36.py": TEST.format(mod="calc", check="self.assertEqual(calc.VALUE, 2)")})
        self.comment(36, prior, "Set the correct value", "The module value is wrong.", line=1,
                     created_at="2026-10-31T10:36:00Z")
        self.comment(36, bad, "Set the correct value", "The module value is wrong.", ident=1, line=2,
                     created_at="2026-10-31T10:37:00Z")
        self.associate(36, prior, bad, fix)
        self.settle_out("p36a", bad, 37, titles=["Set the correct value"], slug="o/proj#36")
        self.settle_out("p36b", fix, 57, clean=True, merged=True, slug="o/proj#36")

        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"]), (0, 0))
        self.assertEqual([row["reason"] for row in result["skipped"]], ["finding-not-covered"])

    def test_identical_line_numbers_in_different_functions_are_distinct_findings(self):
        prior = self.commit("pr37 prior", {"calc.py": "def first():\n    return 1\n"}, "pr37")
        bad = self.commit("pr37 bad", {"calc.py": "def second():\n    return 1\n"})
        fix = self.commit("pr37 fix", {"calc.py": "def second():\n    return 2\n",
                                        "tests/test_calc37.py": TEST.format(mod="calc", check="self.assertEqual(calc.second(), 2)")})
        title, body = "Return the correct value", "This function returns the wrong value."
        self.comment(37, prior, title, body, line=2, created_at="2026-10-31T10:38:00Z")
        self.comment(37, bad, title, body, ident=1, line=2, created_at="2026-10-31T10:39:00Z")
        self.associate(37, prior, bad, fix)
        self.settle_out("p37a", bad, 39, titles=[title], slug="o/proj#37")
        self.settle_out("p37b", fix, 59, clean=True, merged=True, slug="o/proj#37")

        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"], result["skipped"]), (1, 1, []))
        lesson = json.loads(next(iter(c.load_store(self.store)[1]["failures"].values()))["error"])
        self.assertEqual([(finding["title"], finding["reviewed_head"]) for finding in lesson["findings"]], [(title, bad)])

    def test_functions_inside_compound_statements_keep_distinct_scopes(self):
        prior = self.commit("pr38 prior", {"calc.py": "if True:\n    def first():\n        return 1\n"}, "pr38")
        bad = self.commit("pr38 bad", {"calc.py": "if True:\n    def second():\n        return 1\n"})
        fix = self.commit("pr38 fix", {"calc.py": "if True:\n    def second():\n        return 2\n",
                                        "tests/test_calc38.py": TEST.format(mod="calc", check="self.assertEqual(calc.second(), 2)")})
        title, body = "Return the correct value", "This function returns the wrong value."
        self.comment(38, prior, title, body, line=3, created_at="2026-10-31T10:40:00Z")
        self.comment(38, bad, title, body, ident=1, line=3, created_at="2026-10-31T10:41:00Z")
        self.associate(38, prior, bad, fix)
        self.settle_out("p38a", bad, 41, titles=[title], slug="o/proj#38")
        self.settle_out("p38b", fix, 51, clean=True, merged=True, slug="o/proj#38")

        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"], result["skipped"]), (1, 1, []))
        lesson = json.loads(next(iter(c.load_store(self.store)[1]["failures"].values()))["error"])
        self.assertEqual([(finding["title"], finding["reviewed_head"]) for finding in lesson["findings"]], [(title, bad)])

    def test_decorator_findings_use_the_decorated_function_scope(self):
        prior = self.commit("pr39 prior", {"calc.py": "from functools import lru_cache\n@lru_cache()\ndef first():\n    return object()\n"}, "pr39")
        bad = self.commit("pr39 bad", {"calc.py": "from functools import lru_cache\n@lru_cache()\ndef second():\n    return object()\n"})
        fix = self.commit("pr39 fix", {"calc.py": "def second():\n    return object()\n",
                                        "tests/test_calc39.py": "import unittest\nimport calc\n\nclass T(unittest.TestCase):\n    def test_second_is_not_cached(self):\n        self.assertIsNot(calc.second(), calc.second())\n"})
        title, body = "Remove the unintended cache", "This decorator incorrectly caches each returned object."
        self.comment(39, prior, title, body, line=2, created_at="2026-10-31T10:42:00Z")
        self.comment(39, bad, title, body, ident=1, line=2, created_at="2026-10-31T10:43:00Z")
        self.associate(39, prior, bad, fix)
        self.settle_out("p39a", bad, 43, titles=[title], slug="o/proj#39")
        self.settle_out("p39b", fix, 53, clean=True, merged=True, slug="o/proj#39")

        findings = self.matched("p39a")
        self.assertEqual([finding["reviewed_head"] for finding in findings], [bad])
        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"]), (0, 0))
        self.assertEqual(result["skipped"][0]["reason"], "finding-not-covered")

    def test_a_same_title_report_at_the_stopped_head_uses_its_own_comment(self):
        bad = self.carried_pair(27, 28, fresh=False)
        self.comment(27, bad, "Return twenty-six", "Separate issue text.", ident=1,
                     created_at="2026-10-31T10:20:00Z")
        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"], result["skipped"]), (1, 1, []))
        lesson = json.loads(next(iter(c.load_store(self.store)[1]["failures"].values()))["error"])
        self.assertEqual([f["text"].endswith("Separate issue text.") for f in lesson["findings"]], [True])

    def test_a_finding_on_an_unrelated_commit_is_not_carried_to_the_stopped_head(self):
        source = {"calc.py": "def total():\n    return 25\n"}
        unrelated = self.commit("unrelated matching finding", source, "unrelated")
        bad = self.commit("pr41 bad", source, "pr41")
        fix = self.commit("pr41 fix", {"calc.py": "def total():\n    return 26\n",
                                        "tests/test_calc41.py": TEST.format(mod="calc", check="self.assertEqual(calc.total(), 26)")})
        title, text = "Return twenty-six", "The total is wrong because it stops at twenty-five."
        self.comment(41, unrelated, title, text, created_at="2026-10-31T10:49:00Z")
        self.comment(41, bad, title, text, ident=1, created_at="2026-10-31T10:50:00Z")
        self.associate(41, bad, fix)
        self.settle_out("p41a", bad, 50, titles=[title], slug="o/proj#41")
        self.settle_out("p41b", fix, 55, clean=True, merged=True, slug="o/proj#41")

        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"], result["skipped"]), (1, 1, []))
        lesson = json.loads(next(iter(c.load_store(self.store)[1]["failures"].values()))["error"])
        self.assertEqual([(finding["reviewed_head"], finding["text"].endswith(text))
                          for finding in lesson["findings"]], [(bad, True)])

    def test_a_finding_at_the_stopped_head_with_changed_source_line_is_fresh(self):
        prior = self.commit("pr43 prior", {"calc.py": "def total():\n    return 1\n"}, "pr43")
        bad = self.commit("pr43 bad", {"calc.py": "def total():\n    return 2\n"})
        fix = self.commit("pr43 fix", {"calc.py": "def total():\n    return 3\n",
                                        "tests/test_calc43.py": TEST.format(mod="calc", check="self.assertEqual(calc.total(), 3)")})
        title, text = "Return the correct total", "The returned total is incorrect."
        self.comment(43, prior, title, text, created_at="2026-10-31T10:49:00Z")
        self.comment(43, bad, title, text, ident=1, created_at="2026-10-31T10:50:00Z")
        self.associate(43, bad, fix)
        self.settle_out("p43a", bad, 50, titles=[title], slug="o/proj#43")
        self.settle_out("p43b", fix, 55, clean=True, merged=True, slug="o/proj#43")

        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"], result["skipped"]), (1, 1, []))
        lesson = json.loads(next(iter(c.load_store(self.store)[1]["failures"].values()))["error"])
        self.assertEqual([finding["reviewed_head"] for finding in lesson["findings"]], [bad])

    def test_source_line_comparison_preserves_whitespace_inside_string_literals(self):
        prior = self.commit("pr46 prior", {"calc.py": 'def total():\n    return "a b"\n'}, "pr46")
        bad = self.commit("pr46 bad", {"calc.py": 'def total():\n    return "a  b"\n'})
        fix = self.commit("pr46 fix", {"calc.py": 'def total():\n    return "a   b"\n',
                                        "tests/test_calc46.py": TEST.format(mod="calc", check="self.assertEqual(calc.total(), 'a   b')")})
        title, text = "Return the exact text", "The function returns an incorrect string."
        self.comment(46, prior, title, text, created_at="2026-10-31T10:49:00Z")
        self.comment(46, bad, title, text, ident=1, created_at="2026-10-31T10:50:00Z")
        self.associate(46, bad, fix)
        self.settle_out("p46a", bad, 50, titles=[title], slug="o/proj#46")
        self.settle_out("p46b", fix, 55, clean=True, merged=True, slug="o/proj#46")

        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"], result["skipped"]), (1, 1, []))
        lesson = json.loads(next(iter(c.load_store(self.store)[1]["failures"].values()))["error"])
        self.assertEqual([finding["reviewed_head"] for finding in lesson["findings"]], [bad])

    def test_carried_matching_compares_the_complete_multiline_statement(self):
        prior = self.commit("pr48 prior", {"calc.py": "def total():\n    return (\n        1\n    )\n"}, "pr48")
        bad = self.commit("pr48 bad", {"calc.py": "def total():\n    return (\n        2\n    )\n"})
        fix = self.commit("pr48 fix", {"calc.py": "def total():\n    return (\n        3\n    )\n",
                                        "tests/test_calc48.py": TEST.format(mod="calc", check="self.assertEqual(calc.total(), 3)")})
        title, text = "Return the correct total", "The returned total is incorrect."
        self.comment(48, prior, title, text, line=2, created_at="2026-10-31T10:49:00Z")
        self.comment(48, bad, title, text, ident=1, line=2, created_at="2026-10-31T10:50:00Z")
        self.associate(48, bad, fix)
        self.settle_out("p48a", bad, 50, titles=[title], slug="o/proj#48")
        self.settle_out("p48b", fix, 55, clean=True, merged=True, slug="o/proj#48")

        findings = self.matched("p48a")
        self.assertEqual([finding["reviewed_head"] for finding in findings], [bad])
        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"]), (0, 0))
        self.assertEqual(result["skipped"][0]["reason"], "finding-not-covered")

    def test_carried_function_header_ignores_unrelated_body_changes(self):
        prior = self.commit("pr49 prior", {"calc.py": "def total(values=[]):\n    values.append(1)\n    return list(values)\n"}, "pr49")
        bad = self.commit("pr49 bad", {"calc.py": "def total(values=[]):\n    values.append(2)\n    return list(values)\n"})
        fix = self.commit("pr49 fix", {"calc.py": "def total(values=None):\n    if values is None:\n        values = []\n    values.append(2)\n    return list(values)\n",
                                        "tests/test_calc49.py": "import unittest\nimport calc\n\nclass T(unittest.TestCase):\n    def test_default_is_fresh(self):\n        self.assertEqual(calc.total(), [2])\n        self.assertEqual(calc.total(), [2])\n"})
        title, text = "Avoid a shared mutable default", "The default list is shared across calls."
        self.comment(49, prior, title, text, line=1, created_at="2026-10-31T10:49:00Z")
        self.comment(49, bad, title, text, ident=1, line=1, created_at="2026-10-31T10:50:00Z")
        self.associate(49, bad, fix)
        self.settle_out("p49a", bad, 50, titles=[title], slug="o/proj#49")
        self.settle_out("p49b", fix, 55, clean=True, merged=True, slug="o/proj#49")

        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"]), (0, 0))
        self.assertEqual([row["reason"] for row in result["skipped"]], ["finding-not-covered"])

    def test_carried_matching_compares_multiline_decorator_arguments(self):
        module = lambda offset: ("def offset(amount):\n    def decorate(fn):\n        def wrapped():\n"
                                 "            return fn() + amount\n        return wrapped\n    return decorate\n\n"
                                 f"@offset(\n    {offset}\n)\ndef total():\n    return 1\n")
        prior = self.commit("pr50 prior", {"calc.py": module(1)}, "pr50")
        bad = self.commit("pr50 bad", {"calc.py": module(2)})
        fix = self.commit("pr50 fix", {"calc.py": module(3),
                                        "tests/test_calc50.py": TEST.format(mod="calc", check="self.assertEqual(calc.total(), 4)")})
        title, text = "Use the correct offset", "The decorator applies the wrong offset."
        self.comment(50, prior, title, text, line=8, created_at="2026-10-31T10:49:00Z")
        self.comment(50, bad, title, text, ident=1, line=8, created_at="2026-10-31T10:50:00Z")
        self.associate(50, bad, fix)
        self.settle_out("p50a", bad, 50, titles=[title], slug="o/proj#50")
        self.settle_out("p50b", fix, 55, clean=True, merged=True, slug="o/proj#50")

        findings = self.matched("p50a")
        self.assertEqual([finding["reviewed_head"] for finding in findings], [bad])
        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"]), (0, 0))
        self.assertEqual(result["skipped"][0]["reason"], "finding-not-covered")

    def test_carried_if_header_ignores_unrelated_suite_edits(self):
        prior = self.commit("pr51 prior", {"calc.py": "def total(value):\n    if value:\n        return 1\n    return 0\n"}, "pr51")
        bad = self.commit("pr51 bad", {"calc.py": "def total(value):\n    if value:\n        return 2\n    return 0\n"})
        fix = self.commit("pr51 fix", {"calc.py": "def total(value):\n    if value is not None:\n        return 2\n    return 0\n",
                                        "tests/test_calc51.py": TEST.format(mod="calc", check="self.assertEqual(calc.total(0), 2)")})
        title, text = "Handle zero values", "The conditional treats zero as absent."
        self.comment(51, prior, title, text, line=2, created_at="2026-10-31T10:49:00Z")
        self.comment(51, bad, title, text, ident=1, line=2, created_at="2026-10-31T10:50:00Z")
        self.associate(51, bad, fix)
        self.settle_out("p51a", bad, 50, titles=[title], slug="o/proj#51")
        self.settle_out("p51b", fix, 55, clean=True, merged=True, slug="o/proj#51")

        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"]), (0, 0))
        self.assertEqual([row["reason"] for row in result["skipped"]], ["finding-not-covered"])

    def test_non_python_findings_compare_source_content_before_carrying(self):
        prior = self.commit("pr52 prior", {"settings.json": '{"value": 1}\n'}, "pr52")
        bad = self.commit("pr52 bad", {"settings.json": '{"value": 2}\n'})
        fix = self.commit("pr52 fix", {"settings.json": '{"value": 3}\n',
                                        "tests/test_settings52.py": "import json\nimport unittest\n\nclass T(unittest.TestCase):\n    def test_value(self):\n        with open('settings.json') as f:\n            self.assertEqual(json.load(f)['value'], 3)\n"})
        title, text = "Set the expected value", "The configured value is incorrect."
        self.comment(52, prior, title, text, path="settings.json", line=1, created_at="2026-10-31T10:49:00Z")
        self.comment(52, bad, title, text, path="settings.json", ident=1, line=1,
                     created_at="2026-10-31T10:50:00Z")
        self.associate(52, bad, fix)
        self.settle_out("p52a", bad, 50, titles=[("settings.json", title)], slug="o/proj#52")
        self.settle_out("p52b", fix, 55, clean=True, merged=True, slug="o/proj#52")

        findings = self.matched("p52a")
        self.assertEqual([finding["reviewed_head"] for finding in findings], [bad])
        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"]), (0, 0))
        self.assertEqual(result["skipped"][0]["reason"], "finding-not-covered")

    def test_carried_matching_compares_match_case_patterns_and_guards(self):
        source = lambda value: ("def total(value):\n    match value:\n"
                                f"        case {value}:\n            return 'target'\n"
                                "        case _:\n            return 'other'\n")
        prior = self.commit("pr53 prior", {"calc.py": source(1)}, "pr53")
        bad = self.commit("pr53 bad", {"calc.py": source(2)})
        fix = self.commit("pr53 fix", {"calc.py": source(3),
                                        "tests/test_calc53.py": TEST.format(mod="calc", check="self.assertEqual(calc.total(3), 'target')")})
        title, text = "Match the target case", "This pattern selects the wrong value."
        self.comment(53, prior, title, text, line=3, created_at="2026-10-31T10:49:00Z")
        self.comment(53, bad, title, text, ident=1, line=3, created_at="2026-10-31T10:50:00Z")
        self.associate(53, bad, fix)
        self.settle_out("p53a", bad, 50, titles=[title], slug="o/proj#53")
        self.settle_out("p53b", fix, 55, clean=True, merged=True, slug="o/proj#53")

        findings = self.matched("p53a")
        self.assertEqual([finding["reviewed_head"] for finding in findings], [bad])
        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"]), (1, 1))
        lesson = json.loads(next(iter(c.load_store(self.store)[1]["failures"].values()))["error"])
        self.assertEqual([finding["reviewed_head"] for finding in lesson["findings"]], [bad])

    def test_carried_matching_compares_except_headers_without_handler_bodies(self):
        prior = self.commit("pr54 prior", {"calc.py": "def total():\n    try:\n        raise TypeError('x')\n    except ValueError:\n        return 1\n"}, "pr54")
        bad = self.commit("pr54 bad", {"calc.py": "def total():\n    try:\n        raise TypeError('x')\n    except ValueError:\n        return 2\n"})
        fix = self.commit("pr54 fix", {"calc.py": "def total():\n    try:\n        raise TypeError('x')\n    except TypeError:\n        return 2\n",
                                        "tests/test_calc54.py": TEST.format(mod="calc", check="self.assertEqual(calc.total(), 2)")})
        title, text = "Catch the raised exception", "The handler catches the wrong exception type."
        self.comment(54, prior, title, text, line=4, created_at="2026-10-31T10:49:00Z")
        self.comment(54, bad, title, text, ident=1, line=4, created_at="2026-10-31T10:50:00Z")
        self.associate(54, bad, fix)
        self.settle_out("p54a", bad, 50, titles=[title], slug="o/proj#54")
        self.settle_out("p54b", fix, 55, clean=True, merged=True, slug="o/proj#54")

        self.assertEqual([finding["reviewed_head"] for finding in self.matched("p54a")], [prior])
        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"]), (0, 0))
        self.assertEqual(result["skipped"][0]["reason"], "no-failing-replay")

    def test_non_python_matching_compares_complete_multiline_json_values(self):
        source = lambda value: '{\n  "value":\n    ' + str(value) + '\n}\n'
        prior = self.commit("pr55 prior", {"settings.json": source(1)}, "pr55")
        bad = self.commit("pr55 bad", {"settings.json": source(2)})
        fix = self.commit("pr55 fix", {"settings.json": source(3),
                                        "tests/test_settings55.py": "import json\nimport unittest\n\nclass T(unittest.TestCase):\n    def test_value(self):\n        with open('settings.json') as f:\n            self.assertEqual(json.load(f)['value'], 3)\n"})
        title, text = "Set the correct value", "The multiline configuration value is wrong."
        self.comment(55, prior, title, text, path="settings.json", line=2,
                     created_at="2026-10-31T10:49:00Z")
        self.comment(55, bad, title, text, path="settings.json", ident=1, line=2,
                     created_at="2026-10-31T10:50:00Z")
        self.associate(55, bad, fix)
        self.settle_out("p55a", bad, 50, titles=[("settings.json", title)], slug="o/proj#55")
        self.settle_out("p55b", fix, 55, clean=True, merged=True, slug="o/proj#55")

        findings = self.matched("p55a")
        self.assertEqual([finding["reviewed_head"] for finding in findings], [bad])
        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"]), (0, 0))
        self.assertEqual(result["skipped"][0]["reason"], "finding-not-covered")

    def test_json_carried_matching_includes_indented_property_key(self):
        source = lambda value, other: '{\n  "value": ' + str(value) + ',\n  "other": ' + str(other) + '\n}\n'
        prior = self.commit("pr56 prior", {"settings.json": source(1, 1)}, "pr56")
        bad = self.commit("pr56 bad", {"settings.json": source(1, 2)})
        fix = self.commit("pr56 fix", {"settings.json": source(3, 2),
                                        "tests/test_settings56.py": "import json\nimport unittest\n\nclass T(unittest.TestCase):\n    def test_value(self):\n        with open('settings.json') as f:\n            self.assertEqual(json.load(f)['value'], 3)\n"})
        title, text = "Set the correct value", "The value is wrong."
        self.comment(56, prior, title, text, path="settings.json", line=2,
                     created_at="2026-10-31T10:49:00Z")
        self.comment(56, bad, title, text, path="settings.json", ident=1, line=2,
                     created_at="2026-10-31T10:50:00Z")
        self.associate(56, bad, fix)
        self.settle_out("p56a", bad, 50, titles=[("settings.json", title)], slug="o/proj#56")
        self.settle_out("p56b", fix, 55, clean=True, merged=True, slug="o/proj#56")

        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"]), (0, 0))
        self.assertEqual([row["reason"] for row in result["skipped"]], ["finding-not-covered"])

    def test_json_finding_on_line_with_multiple_members_uses_changed_value(self):
        source = lambda count: '{\n  "value": {"count": ' + str(count) + ', "x": 0}\n}\n'
        prior = self.commit("pr57 prior", {"settings.json": source(1)}, "pr57")
        bad = self.commit("pr57 bad", {"settings.json": source(2)})
        fix = self.commit("pr57 fix", {"settings.json": source(3),
            "tests/test_settings57.py": "import json\nimport unittest\n\nclass T(unittest.TestCase):\n    def test_value(self):\n        with open('settings.json') as f:\n            self.assertEqual(json.load(f)['value']['count'], 3)\n"})
        title, text = "Set the correct count", "The count is wrong."
        self.comment(57, prior, title, text, path="settings.json", line=2,
                     created_at="2026-10-31T10:49:00Z")
        self.comment(57, bad, title, text, path="settings.json", ident=1, line=2,
                     created_at="2026-10-31T10:50:00Z")
        self.associate(57, bad, fix)
        self.settle_out("p57a", bad, 50, titles=[("settings.json", title)], slug="o/proj#57")
        self.settle_out("p57b", fix, 55, clean=True, merged=True, slug="o/proj#57")

        findings = self.matched("p57a")
        self.assertEqual([finding["reviewed_head"] for finding in findings], [bad])
        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"]), (0, 0))
        self.assertEqual(result["skipped"][0]["reason"], "finding-not-covered")

    def test_an_unrelated_commit_cannot_supply_a_stopped_head_finding_fallback(self):
        source = {"calc.py": "def total():\n    return 25\n"}
        unrelated = self.commit("unrelated matching finding", source, "unrelated")
        bad = self.commit("pr44 bad", source, "pr44")
        fix = self.commit("pr44 fix", {"calc.py": "def total():\n    return 26\n",
                                        "tests/test_calc44.py": TEST.format(mod="calc", check="self.assertEqual(calc.total(), 26)")})
        self.comment(44, unrelated, "Return twenty-six", "The total is wrong.")
        self.associate(44, bad, fix)
        self.settle_out("p44a", bad, 50, titles=["Return twenty-six"], slug="o/proj#44")
        self.settle_out("p44b", fix, 55, clean=True, merged=True, slug="o/proj#44")

        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"]), (0, 0))
        self.assertEqual([row["reason"] for row in result["skipped"]], ["finding-text-missing"])

    def test_an_ambiguous_ancestor_fallback_is_not_attributed_arbitrarily(self):
        source = {"calc.py": "def total():\n    return 25\n"}
        prior = self.commit("pr45 prior", {**source, "notes.md": "first\n"}, "pr45")
        middle = self.commit("pr45 middle", {**source, "notes.md": "middle\n"})
        bad = self.commit("pr45 bad", {**source, "notes.md": "bad\n"})
        fix = self.commit("pr45 fix", {"calc.py": "def total():\n    return 26\n",
                                        "tests/test_calc45.py": TEST.format(mod="calc", check="self.assertEqual(calc.total(), 26)")})
        title, text = "Return twenty-six", "The total is wrong."
        self.comment(45, prior, title, text, created_at="2026-10-31T10:48:00Z")
        self.comment(45, middle, title, text, ident=1, created_at="2026-10-31T10:49:00Z")
        self.associate(45, bad, fix)
        self.settle_out("p45a", bad, 50, titles=[title], slug="o/proj#45")
        self.settle_out("p45b", fix, 55, clean=True, merged=True, slug="o/proj#45")

        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"]), (0, 0))
        self.assertEqual([row["reason"] for row in result["skipped"]], ["finding-text-missing"])

    def test_carried_fallback_uses_updated_line_and_preserves_fresh_proof(self):
        prior = self.commit("pr47 prior", {"calc.py": "def total():\n    return 25\n\n\ndef extra():\n    return 0\n"}, "pr47")
        bad = self.commit("pr47 bad", {"calc.py": "# header\ndef total():\n    return 25\n\n\ndef extra():\n    return 1\n"})
        fix = self.commit("pr47 fix", {"calc.py": "# header\ndef total():\n    return 26\n\n\ndef extra():\n    return 2\n",
                                        "tests/test_calc47.py": "import unittest\nimport calc\n\nclass T(unittest.TestCase):\n    def test_values(self):\n        self.assertEqual(calc.total(), 26)\n        self.assertEqual(calc.extra(), 2)\n"})
        title, text = "Return the correct total", "The total is incorrect."
        self.comment(47, prior, title, text, line=2, created_at="2026-10-31T10:49:00Z")
        self.comments[f"repos/o/proj/pulls/47/comments?per_page=100"][-1]["line"] = 3
        self.comment(47, bad, "Return the correct extra value", "The extra value is incorrect.", ident=1,
                     line=7, created_at="2026-10-31T10:50:00Z")
        self.associate(47, bad, fix)
        self.settle_out("p47a", bad, 50, titles=[title, "Return the correct extra value"], slug="o/proj#47")
        self.settle_out("p47b", fix, 55, clean=True, merged=True, slug="o/proj#47")

        findings = self.matched("p47a")
        self.assertEqual([(finding["title"], finding["reviewed_head"]) for finding in findings],
                         [(title, prior), ("Return the correct extra value", bad)])
        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"]), (0, 0))
        self.assertEqual(result["skipped"][0]["reason"], "finding-not-covered")

    def test_a_carried_finding_stays_unverified_beside_one_first_reported_at_the_bad_head(self):
        bad = self.carried_pair(24, 27, fresh=True)
        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"], result["skipped"]), (1, 1, []))
        lesson = json.loads(next(iter(c.load_store(self.store)[1]["failures"].values()))["error"])
        self.assertEqual(lesson["bad_head"], bad)
        self.assertEqual([f["title"] for f in lesson["findings"]], ["Mention the notes"])
        self.assertEqual([f["reviewed_head"] for f in lesson["findings"]], [bad])
        self.assertEqual([f["title"] for f in lesson["unverified_findings"]], ["Return twenty-six"])
        self.assertTrue(lesson["unverified_findings"][0]["text"].endswith("Carried finding text."))

    def test_a_stored_lesson_reports_corrected_finding_attribution(self):
        bad = self.carried_pair(31, 27, fresh=True)
        original = scl.finding_text

        def formerly_misattributed(settle, api):
            findings = original(settle, api)
            if settle.head == bad:
                for finding in findings:
                    finding["reviewed_head"] = bad
            return findings

        with mock.patch.object(scl, "finding_text", side_effect=formerly_misattributed):
            first = self.run_import()
        self.assertEqual((first["proven"], first["ingested"]), (1, 1))

        store = c.load_store(self.store)[1]
        existing = next(iter(store["failures"].values()))
        legacy_lesson = json.loads(existing["error"])
        for finding in legacy_lesson["findings"] + legacy_lesson.get("unverified_findings", []):
            finding.pop("reviewed_head", None)
        existing["error"] = c.encoded(legacy_lesson).strip()
        existing["group_id"] = c.group_id(existing)
        existing["signature"] = existing["group_id"]
        (self.store / "failures" / f"{existing['id']}.json").write_text(c.encoded(existing))

        self.comment(31, bad, "Fresh notes finding", "The notes have a separate defect.",
                     path="notes.md", ident=3, line=1, created_at="2026-10-31T10:27:40Z")
        self.settle_out("p31a", bad, 27, titles=["Return twenty-six", "Mention the notes",
                                                   ("notes.md", "Fresh notes finding")], slug="o/proj#31")
        again = self.run_import()
        self.assertEqual((again["proven"], again["ingested"], again["already_present"]), (1, 0, 1))
        self.assertEqual([row["reason"] for row in again["skipped"]],
                         ["attribution-changed-in-stored-lesson", "findings-not-in-stored-lesson"])
        change = again["skipped"][0]
        self.assertEqual([row["title"] for row in change["previous_attribution"] if row["status"] == "verified"],
                         ["Mention the notes", "Return twenty-six"])
        self.assertEqual([row["title"] for row in change["corrected_attribution"] if row["status"] == "verified"],
                         ["Mention the notes"])
        self.assertTrue(all(row["reviewed_head"] == bad for row in change["previous_attribution"]))

    def test_a_stored_lesson_reports_when_all_findings_are_now_unverified(self):
        bad = self.carried_pair(32, 27, fresh=False)
        self.git("rm", "tests/test_calc32.py")
        self.git("commit", "-q", "-m", "pr32 later candidate")
        later = self.git("rev-parse", "HEAD")
        self.settle_out("p32c", later, 58, clean=True, merged=True, slug="o/proj#32")
        self.comment(32, bad, "Return twenty-six", "Carried finding text.", ident=1,
                     created_at="2026-10-31T10:27:30Z")
        original = scl.finding_text

        def formerly_misattributed(settle, api):
            findings = original(settle, api)
            if settle.head == bad:
                for finding in findings:
                    finding["reviewed_head"] = bad
            return findings

        with mock.patch.object(scl, "finding_text", side_effect=formerly_misattributed):
            first = self.run_import()
        self.assertEqual((first["proven"], first["ingested"]), (1, 1))

        self.comment(32, bad, "Additional finding", "The notes need their own correction.",
                     path="notes.md", ident=2, line=1, created_at="2026-10-31T10:27:40Z")
        self.settle_out("p32a", bad, 27, titles=["Return twenty-six", ("notes.md", "Additional finding")], slug="o/proj#32")
        again = self.run_import()
        self.assertEqual((again["proven"], again["ingested"]), (0, 0))
        self.assertEqual([row["reason"] for row in again["skipped"]], ["attribution-changed-in-stored-lesson"])
        change = again["skipped"][0]
        self.assertEqual([row["status"] for row in change["previous_attribution"]], ["verified"])
        self.assertEqual([row["title"] for row in change["corrected_attribution"]], ["Return twenty-six", "Additional finding"])
        self.assertEqual([row["status"] for row in change["corrected_attribution"]], ["unverified", "unverified"])

    def test_a_later_identical_repost_does_not_override_the_stopped_head_comment(self):
        calc = lambda value: {"calc.py": f"def total():\n    return {value}\n"}
        bad = self.commit("pr33 bad", calc(3), "pr33")
        fix = self.commit("pr33 fix", {**calc(4), "tests/test_calc33.py": TEST.format(mod="calc", check="self.assertEqual(calc.total(), 4)")})
        text = "The stopped-head finding text."
        self.comment(33, bad, "Return four", text, ident=0, created_at="2026-10-31T10:20:00Z")
        self.comment(33, fix, "Return four", text, ident=1, created_at="2026-10-31T10:20:30Z")
        self.associate(33, bad, fix)
        self.settle_out("p33a", bad, 20, titles=["Return four"], slug="o/proj#33")
        self.settle_out("p33b", fix, 40, clean=True, merged=True, slug="o/proj#33")

        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"], result["skipped"]), (1, 1, []))
        lesson = json.loads(next(iter(c.load_store(self.store)[1]["failures"].values()))["error"])
        self.assertEqual([finding["reviewed_head"] for finding in lesson["findings"]], [bad])

    def test_same_text_findings_at_different_lines_bind_to_the_stopped_head(self):
        source = lambda first, second: f"def first():\n    return {first}\n\n\ndef second():\n    return {second}\n"
        prior = self.commit("pr34 prior", {"calc.py": source(1, 2)}, "pr34")
        bad = self.commit("pr34 bad", {"calc.py": source(3, 2)})
        fix = self.commit("pr34 fix", {"calc.py": source(3, 4),
                                        "tests/test_calc34.py": TEST.format(mod="calc", check="self.assertEqual(calc.second(), 4)")})
        text = "Return the right value."
        self.comment(34, prior, "Return the right value", text, ident=0, line=2,
                     created_at="2026-10-31T10:19:00Z")
        self.comment(34, bad, "Return the right value", text, ident=1, line=6,
                     created_at="2026-10-31T10:20:00Z")
        self.associate(34, prior, bad, fix)
        self.settle_out("p34a", bad, 20, titles=["Return the right value"], slug="o/proj#34")
        self.settle_out("p34b", fix, 40, clean=True, merged=True, slug="o/proj#34")

        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"], result["skipped"]), (1, 1, []))
        lesson = json.loads(next(iter(c.load_store(self.store)[1]["failures"].values()))["error"])
        self.assertEqual([(finding["line"], finding["reviewed_head"]) for finding in lesson["findings"]], [(6, bad)])

    def test_a_finding_binds_to_the_comment_with_the_exact_title_not_a_longer_one(self):
        calc = lambda value: {"calc.py": f"def total():\n    return {value}\n"}
        bad = self.commit("pr25 bad", calc(25), "pr25")
        fix = self.commit("pr25 fix", {**calc(26), "tests/test_calc25.py": TEST.format(mod="calc", check="self.assertEqual(calc.total(), 26)")})
        self.comment(25, bad, "Return twenty-six before parsing", "Longer title text.")
        self.comment(25, bad, "Return twenty-six", "Exact title text.", ident=1)
        self.associate(25, bad, fix)
        self.settle_out("p25a", bad, 28, titles=["Return twenty-six"], slug="o/proj#25")
        self.settle_out("p25b", fix, 48, clean=True, merged=True, slug="o/proj#25")
        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"], result["skipped"]), (1, 1, []))
        lesson = json.loads(next(iter(c.load_store(self.store)[1]["failures"].values()))["error"])
        self.assertEqual([f["text"].endswith("Exact title text.") for f in lesson["findings"]], [True])

    def test_imported_lessons_group_like_every_other_failure(self):
        self.build()
        self.run_import()
        rows = list(c.load_store(self.store)[1]["failures"].values())
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({c.signature(row) for row in rows}), 2)

    def test_a_store_holding_older_failures_without_a_source_ref_still_imports(self):
        legacy = dict(c.new_record(), agent="codex", command="older failure", error="boom", repo="o/proj", source="stdin", thread="")
        legacy["group_id"] = c.group_id(legacy)
        c.save(self.store, "failures", legacy)
        self.build()
        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"]), (2, 2))
        self.assertEqual(len(c.load_store(self.store)[1]["failures"]), 3)

    def test_rerun_is_idempotent_and_dry_run_writes_nothing(self):
        self.build()
        dry = self.run_import(dry_run=True)
        self.assertEqual((dry["proven"], dry["ingested"]), (2, 0))
        self.assertFalse(self.store.exists())
        self.run_import()
        again = self.run_import()
        self.assertEqual((again["proven"], again["ingested"], again["already_present"]), (2, 0, 2))
        self.assertEqual(len(c.load_store(self.store)[1]["failures"]), 2)


    def test_a_fixture_whose_name_contains_a_space_is_staged_before_the_failing_run(self):
        bad = self.commit("pr23 bad", {"calc.py": "def total():\n    return 2\n"}, "pr23")
        fix = self.commit("pr23 fix", {"calc.py": "def total():\n    return 1 + 1\n",
                                       "tests/expected output.txt": "2\n",
                                       "tests/test_calc23.py": TEST.format(mod="calc", check="self.assertEqual(calc.total(), int(open('tests/expected output.txt').read()))")})
        self.comment(23, bad, "Compute the total again")
        self.associate(23, bad, fix)
        self.settle_out("p23a", bad, 26, titles=["Compute the total again"], slug="o/proj#23")
        self.settle_out("p23b", fix, 47, clean=True, merged=True, slug="o/proj#23")
        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"]), (0, 0))
        self.assertEqual([row["reason"] for row in result["skipped"]], ["no-failing-replay"])

    def test_a_symlink_fixture_replaced_by_a_file_is_restaged_before_the_failing_run(self):
        self.git("checkout", "-q", "-B", "pr24", self.base)
        (self.repo / "tests").mkdir()
        os.symlink("other.txt", self.repo / "tests/expected.txt")
        bad = self.commit("pr24 bad", {"calc.py": "def total():\n    return 2\n", "tests/other.txt": "1\n"})
        (self.repo / "tests/expected.txt").unlink()
        fix = self.commit("pr24 fix", {"calc.py": "def total():\n    return 1 + 1\n",
                                       "tests/expected.txt": "2\n",
                                       "tests/test_calc24.py": TEST.format(mod="calc", check="self.assertEqual(open('tests/expected.txt').read().strip(), '2')")})
        self.comment(24, bad, "Compute the total once more")
        self.associate(24, bad, fix)
        self.settle_out("p24a", bad, 27, titles=["Compute the total once more"], slug="o/proj#24")
        self.settle_out("p24b", fix, 48, clean=True, merged=True, slug="o/proj#24")
        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"]), (0, 0))
        self.assertEqual([row["reason"] for row in result["skipped"]], ["no-failing-replay"])

    def test_a_fixture_file_replaced_by_a_directory_is_restaged_before_the_failing_run(self):
        bad = self.commit("pr25 bad", {"calc.py": "def total():\n    return 2\n", "tests/data": "old\n"}, "pr25")
        (self.repo / "tests/data").unlink()
        fix = self.commit("pr25 fix", {"calc.py": "def total():\n    return 1 + 1\n",
                                       "tests/data/expected.txt": "2\n",
                                       "tests/test_calc25.py": TEST.format(mod="calc", check="self.assertEqual(open('tests/data/expected.txt').read().strip(), '2')")})
        self.comment(25, bad, "Compute the total a fourth time")
        self.associate(25, bad, fix)
        self.settle_out("p25a", bad, 28, titles=["Compute the total a fourth time"], slug="o/proj#25")
        self.settle_out("p25b", fix, 49, clean=True, merged=True, slug="o/proj#25")
        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"]), (0, 0))
        self.assertEqual([row["reason"] for row in result["skipped"]], ["no-failing-replay"])

    def test_a_fixture_directory_replaced_by_a_file_is_restaged_before_the_failing_run(self):
        bad = self.commit("pr26 bad", {"calc.py": "def total():\n    return 2\n", "tests/data/expected.txt": "old\n"}, "pr26")
        (self.repo / "tests/data/expected.txt").unlink()
        (self.repo / "tests/data").rmdir()
        fix = self.commit("pr26 fix", {"calc.py": "def total():\n    return 1 + 1\n",
                                       "tests/data": "2\n",
                                       "tests/test_calc26.py": TEST.format(mod="calc", check="self.assertEqual(open('tests/data').read().strip(), '2')")})
        self.comment(26, bad, "Compute the total a fifth time")
        self.associate(26, bad, fix)
        self.settle_out("p26a", bad, 29, titles=["Compute the total a fifth time"], slug="o/proj#26")
        self.settle_out("p26b", fix, 50, clean=True, merged=True, slug="o/proj#26")
        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"]), (0, 0))
        self.assertEqual([row["reason"] for row in result["skipped"]], ["no-failing-replay"])

if __name__ == "__main__":
    unittest.main()
