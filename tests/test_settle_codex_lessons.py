"""Settle Codex finding lesson importer proofs. Synthetic repository, logs and GitHub responses; no network."""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import tempfile
import unittest
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
        return {**self.pulls, **self.comments}.get(path, [])

    def comment(self, number, head, title, text="Full finding text.", path="calc.py", ident=0, line=2,
                test_names=("test_total",)):
        slug_path = f"repos/o/proj/pulls/{number}/comments?per_page=100"
        comment_id = 1000 + number + ident
        self.comments.setdefault(slug_path, []).append(dict(
            id=comment_id, user=dict(login=BOT), path=path, original_line=line, original_commit_id=head,
            body=f"<!-- codex-code-review-finding:v1 -->\n{BADGE.format(title)}\n\n{text}",
            html_url=f"https://github.com/o/proj/pull/{number}#discussion_r{comment_id}", created_at="2026-10-31T10:01:00Z"))
        if test_names:
            self.comments[slug_path].append(dict(
                id=100000 + comment_id, in_reply_to_id=comment_id, user=dict(login="t"),
                body="Fixed and guarded by " + ", ".join(test_names)))

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
        finished = datetime(2026, 10, 31, 10, minute, tzinfo=timezone.utc) + timedelta(minutes=1)
        lines.append(f"end {finished.isoformat()}")
        (self.settle / f"settle-{name}.out").write_text("\n".join(lines) + "\n")

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
                                      "tests/test_reviewed9.py": "import unittest\nimport parser\nimport writer\n\n"
                                      "class T(unittest.TestCase):\n"
                                      "    def test_parser(self):\n        self.assertEqual(parser.run(), 3)\n\n"
                                      "    def test_writer(self):\n        self.assertEqual(writer.run(), 4)\n"})
        self.comment(9, bad, "Handle the empty case", "Problem in the parser.", path="parser.py", test_names=("test_parser",))
        self.comment(9, bad, "Handle the empty case", "Problem in the writer.", path="writer.py", ident=1,
                     test_names=("test_writer",))
        self.associate(9, bad, fix)
        self.settle_out("p9a", bad, 8, titles=[("parser.py", "Handle the empty case"), ("writer.py", "Handle the empty case")], slug="o/proj#9")
        self.settle_out("p9b", fix, 28, clean=True, merged=True, slug="o/proj#9")
        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"]), (1, 1))
        row = next(iter(c.load_store(self.store)[1]["failures"].values()))
        findings = json.loads(row["error"])["findings"]
        self.assertEqual([f["path"] for f in findings], ["parser.py", "writer.py"])
        self.assertTrue(findings[0]["text"].endswith("Problem in the parser."))
        self.assertTrue(findings[1]["text"].endswith("Problem in the writer."))
        self.assertEqual([f["url"][-4:] for f in findings], ["1009", "1010"])

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
        source = lambda a, b: f"def total():\n    first = {a}\n    second = {b}\n    return first + second\n"
        bad = self.commit("pr13 bad", {"calc.py": source(1, 2)}, "pr13")
        fix = self.commit("pr13 fix", {"calc.py": source(3, 2),
                                       "tests/test_calc13.py": TEST.format(mod="calc", check="self.assertEqual(calc.total(), 5)")})
        self.comment(13, bad, "Fix the first value", "The first value is wrong.", line=2)
        self.comment(13, bad, "Fix the second value", "The second value is wrong.", line=3, ident=1)
        self.associate(13, bad, fix)
        self.settle_out("p13a", bad, 14, titles=["Fix the first value", "Fix the second value"], slug="o/proj#13")
        self.settle_out("p13b", fix, 36, clean=True, merged=True, slug="o/proj#13")
        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"]), (1, 1))
        row = next(iter(c.load_store(self.store)[1]["failures"].values()))
        lesson = json.loads(row["error"])
        self.assertEqual([f["title"] for f in lesson["findings"]], ["Fix the first value"])
        self.assertEqual([f["title"] for f in lesson["unverified_findings"]], ["Fix the second value"])

    def test_a_replay_only_proves_the_finding_named_by_its_review_reply(self):
        bad = self.commit("pr27 bad", {"calc.py": "def total():\n    return 10 + 0\n"}, "pr27")
        fix = self.commit("pr27 fix", {"calc.py": "def total():\n    return 10 + 2\n",
                                       "tests/test_calc27.py": TEST.format(mod="calc", check="self.assertEqual(calc.total(), 12)")})
        self.comment(27, bad, "Use the required offset", line=2, test_names=("test_total",))
        self.comment(27, bad, "Correct the base value", line=2, ident=1, test_names=())
        self.associate(27, bad, fix)
        self.settle_out("p27a", bad, 27, titles=[("calc.py", "Use the required offset"),
                                                  ("calc.py", "Correct the base value")], slug="o/proj#27")
        self.settle_out("p27b", fix, 47, clean=True, merged=True, slug="o/proj#27")
        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"]), (1, 1))
        lesson = json.loads(next(iter(c.load_store(self.store)[1]["failures"].values()))["error"])
        self.assertEqual([f["title"] for f in lesson["findings"]], ["Use the required offset"])
        self.assertEqual([f["title"] for f in lesson["unverified_findings"]], ["Correct the base value"])

    def test_a_finding_accepts_multiple_named_regression_tests(self):
        bad = self.commit("pr30 bad", {"calc.py": "def total():\n    return 10 + 0\n"}, "pr30")
        fix = self.commit("pr30 fix", {"calc.py": "def total():\n    return 10 + 2\n",
                                       "tests/test_calc30.py": "import unittest\nimport calc\n\n"
                                       "class T(unittest.TestCase):\n"
                                       "    def test_total_expected_value(self):\n        self.assertEqual(calc.total(), 12)\n\n"
                                       "    def test_total_twice_the_base(self):\n        self.assertEqual(calc.total(), 6 * 2)\n"})
        self.comment(30, bad, "Use the required offset", line=2,
                     test_names=("test_total_expected_value", "test_total_twice_the_base"))
        self.associate(30, bad, fix)
        self.settle_out("p30a", bad, 30, titles=["Use the required offset"], slug="o/proj#30")
        self.settle_out("p30b", fix, 50, clean=True, merged=True, slug="o/proj#30")
        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"]), (1, 1))

    def test_later_passing_settles_can_prove_findings_in_separate_fixes(self):
        source = lambda first, second: f"def first():\n    return {first}\n\ndef second():\n    return {second}\n"
        bad = self.commit("pr28 bad", {"calc.py": source(1, 2)}, "pr28")
        first_fix = self.commit("pr28 first fix", {"calc.py": source(3, 2),
                                                    "tests/test_first28.py": "import unittest\nimport calc\n\n"
                                                    "class T(unittest.TestCase):\n"
                                                    "    def test_first(self):\n        self.assertEqual(calc.first(), 3)\n"})
        second_fix = self.commit("pr28 second fix", {"calc.py": source(3, 4),
                                                     "tests/test_second28.py": "import unittest\nimport calc\n\n"
                                                     "class T(unittest.TestCase):\n"
                                                     "    def test_second(self):\n        self.assertEqual(calc.second(), 4)\n"})
        self.comment(28, bad, "Fix the first result", line=2, test_names=("test_first",))
        self.comment(28, bad, "Fix the second result", line=5, ident=1, test_names=("test_second",))
        self.associate(28, bad, first_fix, second_fix)
        self.settle_out("p28a", bad, 28, titles=[("calc.py", "Fix the first result"),
                                                  ("calc.py", "Fix the second result")], slug="o/proj#28")
        self.settle_out("p28b", first_fix, 38, clean=True, slug="o/proj#28")
        self.settle_out("p28c", second_fix, 48, clean=True, merged=True, slug="o/proj#28")

        result = self.run_import()
        rows = {row["source_ref"]: json.loads(row["error"])
                for row in c.load_store(self.store)[1]["failures"].values()}
        first = rows[f"settle-codex:o/proj:28:{bad[:12]}:{first_fix[:12]}"]
        last = rows[f"settle-codex:o/proj:28:{bad[:12]}:{second_fix[:12]}"]
        self.assertEqual(result["proven"], 2)
        self.assertEqual([f["title"] for f in first["findings"]], ["Fix the first result"])
        self.assertEqual([f["title"] for f in first["unverified_findings"]], ["Fix the second result"])
        self.assertEqual([f["title"] for f in last["findings"]], ["Fix the first result", "Fix the second result"])

    def test_newly_linked_proof_for_a_stored_finding_is_reported(self):
        source = lambda first, second: f"def first():\n    return {first}\n\ndef second():\n    return {second}\n"
        bad = self.commit("pr29 bad", {"calc.py": source(1, 2)}, "pr29")
        fix = self.commit("pr29 fix", {"calc.py": source(3, 4),
                                        "tests/test_both29.py": "import unittest\nimport calc\n\n"
                                        "class T(unittest.TestCase):\n"
                                        "    def test_first(self):\n        self.assertEqual(calc.first(), 3)\n\n"
                                        "    def test_second(self):\n        self.assertEqual(calc.second(), 4)\n"})
        self.comment(29, bad, "Fix the first result", line=2, test_names=("test_first",))
        self.comment(29, bad, "Fix the second result", line=5, ident=1, test_names=())
        self.associate(29, bad, fix)
        self.settle_out("p29a", bad, 29, titles=[("calc.py", "Fix the first result"),
                                                  ("calc.py", "Fix the second result")], slug="o/proj#29")
        self.settle_out("p29b", fix, 49, clean=True, merged=True, slug="o/proj#29")
        self.assertEqual(self.run_import()["ingested"], 1)

        comments = self.comments["repos/o/proj/pulls/29/comments?per_page=100"]
        comments.append(dict(id=100000 + 1030, in_reply_to_id=1030, user=dict(login="t"),
                             body="Fixed and guarded by test_second"))
        result = self.run_import()
        self.assertEqual((result["ingested"], result["already_present"]), (0, 1))
        self.assertEqual([row["reason"] for row in result["skipped"]],
                         ["newly-proven-findings-not-updated"])
        self.assertEqual(result["skipped"][0]["findings"], [{"path": "calc.py", "title": "Fix the second result"}])

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

    def test_an_unrelated_replay_does_not_prove_a_changed_finding(self):
        bad = self.commit("pr19 bad", {"calc.py": "def total():\n    return 23\n",
                                        "parser.py": "def parse():\n    return None\n"}, "pr19")
        fix = self.commit("pr19 fix", {"calc.py": "def total():\n    return 24\n",
                                        "parser.py": "def parse():\n    return 'value'\n",
                                        "tests/test_calc19.py": TEST.format(mod="calc", check="self.assertEqual(calc.total(), 24)")})
        self.comment(19, bad, "Return parsed values", path="parser.py")
        self.associate(19, bad, fix)
        self.settle_out("p19a", bad, 19, titles=[("parser.py", "Return parsed values")], slug="o/proj#19")
        self.settle_out("p19b", fix, 40, clean=True, merged=True, slug="o/proj#19")
        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"]), (0, 0))
        self.assertEqual([row["reason"] for row in result["skipped"]], ["finding-not-covered"])
        self.assertEqual(c.load_store(self.store)[1]["failures"], {})

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

    def pair_of_two_functions(self, number, minute):
        source = lambda a, b: f"def first():\n    return {a}\n\n\n\ndef second():\n    return {b}\n"
        bad = self.commit(f"pr{number} bad", {"calc.py": source(1, 2)}, f"pr{number}")
        fix = self.commit(f"pr{number} fix", {"calc.py": source(3, 4),
                                              f"tests/test_calc{number}.py": "import unittest\nimport calc\n\n"
                                              "class T(unittest.TestCase):\n"
                                              "    def test_first(self):\n        self.assertEqual(calc.first(), 3)\n\n"
                                              "    def test_second(self):\n        self.assertEqual(calc.second(), 4)\n"})
        self.comment(number, bad, "Fix the first function", line=2, test_names=("test_first",))
        self.comment(number, bad, "Fix the second function", line=7, ident=1, test_names=("test_second",))
        self.associate(number, bad, fix)
        slug = f"o/proj#{number}"
        self.settle_out(f"p{number}a", bad, minute, titles=["Fix the first function"], slug=slug)
        return bad, fix, slug

    def test_findings_from_repeated_settles_of_one_pair_are_merged(self):
        bad, fix, slug = self.pair_of_two_functions(19, 20)
        self.settle_out("p19b", bad, 21, titles=["Fix the second function"], slug=slug)
        self.settle_out("p19c", fix, 43, clean=True, merged=True, slug=slug)
        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"]), (1, 1))
        lesson = json.loads(next(iter(c.load_store(self.store)[1]["failures"].values()))["error"])
        self.assertEqual([f["title"] for f in lesson["findings"]], ["Fix the first function", "Fix the second function"])

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

    def test_a_replay_without_json_output_is_skipped_and_the_batch_continues(self):
        bad = self.commit("pr22 bad", {"calc.py": "def total():\n    __import__('os')._exit(7)\n"}, "pr22")
        fix = self.commit("pr22 fix", {"calc.py": "def total():\n    return 1\n",
                                       "tests/test_calc22.py": TEST.format(mod="calc", check="self.assertEqual(calc.total(), 1)")})
        self.comment(22, bad, "Return one")
        self.associate(22, bad, fix)
        self.settle_out("p22a", bad, 25, titles=["Return one"], slug="o/proj#22")
        self.settle_out("p22b", fix, 45, clean=True, merged=True, slug="o/proj#22")

        bad = self.commit("pr23 bad", {"calc.py": "def total():\n    return 2\n"}, "pr23")
        fix = self.commit("pr23 fix", {"calc.py": "def total():\n    return 3\n",
                                       "tests/test_calc23.py": TEST.format(mod="calc", check="self.assertEqual(calc.total(), 3)")})
        self.comment(23, bad, "Return three")
        self.associate(23, bad, fix)
        self.settle_out("p23a", bad, 26, titles=["Return three"], slug="o/proj#23")
        self.settle_out("p23b", fix, 46, clean=True, merged=True, slug="o/proj#23")

        result = self.run_import()
        self.assertEqual(result["proven"], 1)
        self.assertEqual(result["sources"], [f"settle-codex:o/proj:23:{bad[:12]}:{fix[:12]}"])
        self.assertEqual([row["reason"] for row in result["skipped"]], ["replay-invalid-output"])

    def test_pass_and_merge_receipts_must_match_the_pr_head(self):
        current, stale = "a" * 40, "b" * 40
        stale_log = self.settle / "settle-stale-receipt.out"
        stale_log.write_text(f"pr: OPEN {current}\nPASS: tests=3 head={stale}\nMERGED {stale}\n")
        parsed = scl.parse(stale_log)
        self.assertFalse(parsed.passed)
        self.assertFalse(parsed.merged)

        current_log = self.settle / "settle-current-receipt.out"
        current_log.write_text(f"pr: OPEN {current}\nPASS: tests=3 head={current[:12]}\nMERGED {current}\n")
        parsed = scl.parse(current_log)
        self.assertTrue(parsed.passed)
        self.assertTrue(parsed.merged)

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


    def test_carried_finding_stays_unverified_beside_fresh_finding(self):
        prior = self.commit("prior", {"calc.py": "def first():\n    return 1\n\ndef second():\n    return 1\n"}, "pr70")
        bad = self.commit("bad", {"calc.py": "def first():\n    return 1\n\ndef second():\n    return 2\n"})
        fixed = self.commit("fixed", {"calc.py": "def first():\n    return 3\n\ndef second():\n    return 4\n",
            "tests/test_calc70.py": "import unittest\nimport calc\nclass T(unittest.TestCase):\n    def test_first(self):\n        self.assertEqual(calc.first(), 3)\n    def test_second(self):\n        self.assertEqual(calc.second(), 4)\n"})
        self.comment(70, prior, "Fix first", line=2, test_names=("test_first",))
        self.comment(70, bad, "Fix first", line=2, ident=1, test_names=("test_first",))
        self.comment(70, bad, "Fix second", line=5, ident=2, test_names=("test_second",))
        self.associate(70, bad, fixed)
        self.settle_out("p70a", bad, 50, titles=["Fix first", "Fix second"], slug="o/proj#70")
        self.settle_out("p70b", fixed, 55, clean=True, merged=True, slug="o/proj#70")
        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"]), (1, 1))
        lesson = json.loads(next(iter(c.load_store(self.store)[1]["failures"].values()))["error"])
        self.assertEqual([f["title"] for f in lesson["findings"]], ["Fix second"])
        self.assertEqual([f["title"] for f in lesson["unverified_findings"]], ["Fix first"])

    def test_first_reported_finding_with_direct_assertion_is_proven(self):
        bad = self.commit("bad", {"calc.py": "def total():\n    return 1\n"}, "pr78")
        fixed = self.commit("fixed", {"calc.py": "def total():\n    return 2\n",
                                       "tests/test_calc78.py": TEST.format(mod="calc", check="self.assertEqual(calc.total(), 2)")})
        self.comment(78, bad, "Fix total")
        self.associate(78, bad, fixed)
        self.settle_out("p78a", bad, 50, titles=["Fix total"], slug="o/proj#78")
        self.settle_out("p78b", fixed, 55, clean=True, merged=True, slug="o/proj#78")
        result = self.run_import()
        self.assertEqual((result["proven"], result["ingested"]), (1, 1))
        lesson = json.loads(next(iter(c.load_store(self.store)[1]["failures"].values()))["error"])
        self.assertEqual([(f["title"], f["reviewed_head"]) for f in lesson["findings"]], [("Fix total", bad)])
        self.assertEqual(lesson["unverified_findings"], [])
        self.assertEqual(lesson["replay"]["counterfactuals"], [dict(
            path="calc.py", line=2, test="tests.test_calc78.T.test_total",
            assertion_path="tests/test_calc78.py", assertion_line=7,
            reverted="fail", restored="ok")])

    def test_multiline_edit_remains_unverified(self):
        bad = self.commit("bad", {"calc.py": "def total():\n    return (\n        1\n    )\n"}, "pr79")
        fixed = self.commit("fixed", {"calc.py": "def total():\n    return (\n        2 +\n        1\n    )\n",
                                       "tests/test_calc79.py": TEST.format(mod="calc", check="self.assertEqual(calc.total(), 3)")})
        self.comment(79, bad, "Fix total", line=3)
        self.associate(79, bad, fixed)
        self.settle_out("p79a", bad, 50, titles=["Fix total"], slug="o/proj#79")
        self.settle_out("p79b", fixed, 55, clean=True, merged=True, slug="o/proj#79")
        result = self.run_import()
        self.assertEqual(result["ingested"], 0)
        self.assertEqual(result["skipped"][0]["reason"], "finding-not-covered")

    def test_json_only_finding_is_reported_as_unverified(self):
        bad = self.commit("bad", {"settings.json": '{"count": 1}\n'}, "pr74")
        fixed = self.commit("fixed", {"settings.json": '{"count": 2}\n',
            "tests/test_config74.py": "import json\nimport unittest\nclass T(unittest.TestCase):\n    def test_total(self):\n        with open('settings.json') as f:\n            self.assertEqual(json.load(f)['count'], 2)\n"})
        self.comment(74, bad, "Fix count", path="settings.json", line=1)
        self.associate(74, bad, fixed)
        self.settle_out("p74a", bad, 50, titles=[("settings.json", "Fix count")], slug="o/proj#74")
        self.settle_out("p74b", fixed, 55, clean=True, merged=True, slug="o/proj#74")
        result = self.run_import()
        self.assertEqual(result["ingested"], 0)
        self.assertEqual(result["skipped"][0]["reason"], "finding-not-covered")
        self.assertEqual(result["skipped"][0]["unverified_findings"],
                         [{"path": "settings.json", "title": "Fix count", "reviewed_head": bad,
                           "url": "https://github.com/o/proj/pull/74#discussion_r1074"}])

    def test_json_finding_remains_unverified_beside_proven_python_finding(self):
        bad = self.commit("bad", {"calc.py": "def total():\n    return 1\n", "settings.json": '{"count": 1}\n'}, "pr73")
        fixed = self.commit("fixed", {"calc.py": "def total():\n    return 2\n", "settings.json": '{"count": 2}\n',
                                       "tests/test_calc73.py": TEST.format(mod="calc", check="self.assertEqual(calc.total(), 2)")})
        self.comment(73, bad, "Fix total")
        self.comment(73, bad, "Fix count", path="settings.json", line=1, ident=1)
        self.associate(73, bad, fixed)
        self.settle_out("p73a", bad, 50, titles=[("calc.py", "Fix total"), ("settings.json", "Fix count")], slug="o/proj#73")
        self.settle_out("p73b", fixed, 55, clean=True, merged=True, slug="o/proj#73")
        result = self.run_import()
        self.assertEqual(result["ingested"], 1)
        lesson = json.loads(next(iter(c.load_store(self.store)[1]["failures"].values()))["error"])
        self.assertEqual([f["title"] for f in lesson["findings"]], ["Fix total"])
        self.assertEqual([f["title"] for f in lesson["unverified_findings"]], ["Fix count"])

    def test_unrelated_assertion_does_not_prove_executed_finding(self):
        bad = self.commit("bad", {"calc.py": "def total():\n    return 1\n", "other.py": "VALUE = 1\n"}, "pr72")
        fixed = self.commit("fixed", {"calc.py": "def total():\n    return 2\n", "other.py": "VALUE = 2\n",
            "tests/test_calc72.py": "import unittest\nimport calc\nimport other\nclass T(unittest.TestCase):\n    def test_total(self):\n        calc.total()\n        self.assertEqual(other.VALUE, 2)\n"})
        self.comment(72, bad, "Fix total")
        self.associate(72, bad, fixed)
        self.settle_out("p72a", bad, 50, titles=["Fix total"], slug="o/proj#72")
        self.settle_out("p72b", fixed, 55, clean=True, merged=True, slug="o/proj#72")
        self.assertEqual(self.run_import()["ingested"], 0)

    def test_same_line_distinct_assertions_do_not_prove_finding(self):
        bad = self.commit("bad", {"calc.py": "def first():\n    return 1\n\ndef second():\n    return 2\n"}, "pr81")
        fixed = self.commit("fixed", {"calc.py": "def first():\n    return 3\n\ndef second():\n    return 4\n",
            "tests/test_calc81.py": "import unittest\nimport calc\nclass T(unittest.TestCase):\n    def test_total(self):\n        calc.second()\n        self.assertEqual(calc.first(), 3); self.assertEqual(calc.second(), 4)\n"})
        self.comment(81, bad, "Fix second", line=5)
        self.associate(81, bad, fixed)
        self.settle_out("p81a", bad, 50, titles=["Fix second"], slug="o/proj#81")
        self.settle_out("p81b", fixed, 55, clean=True, merged=True, slug="o/proj#81")
        result = self.run_import()
        self.assertEqual(result["ingested"], 0)
        self.assertEqual(result["skipped"][0]["reason"], "finding-not-covered")

    def test_same_expression_distinct_assertions_do_not_prove_finding(self):
        bad = self.commit("bad", {"calc.py": "def first():\n    return 1\n\ndef second():\n    return 2\n"}, "pr83")
        fixed = self.commit("fixed", {"calc.py": "def first():\n    return 3\n\ndef second():\n    return 4\n",
            "tests/test_calc83.py": "import unittest\nimport calc\nclass T(unittest.TestCase):\n    def test_total(self):\n        calc.second()\n        (self.assertEqual(calc.first(), 3), self.assertEqual(calc.second(), 4))\n"})
        self.comment(83, bad, "Fix second", line=5)
        self.associate(83, bad, fixed)
        self.settle_out("p83a", bad, 50, titles=["Fix second"], slug="o/proj#83")
        self.settle_out("p83b", fixed, 55, clean=True, merged=True, slug="o/proj#83")
        result = self.run_import()
        self.assertEqual(result["ingested"], 0)
        self.assertEqual(result["skipped"][0]["reason"], "finding-not-covered")

    def test_unrelated_assertion_in_same_file_does_not_prove_finding(self):
        bad = self.commit("bad", {"calc.py": "def first():\n    return 1\n\ndef second():\n    return 2\n"}, "pr75")
        fixed = self.commit("fixed", {"calc.py": "def first():\n    return 3\n\ndef second():\n    return 4\n",
            "tests/test_calc75.py": "import unittest\nimport calc\nclass T(unittest.TestCase):\n    def test_total(self):\n        calc.first()\n        self.assertEqual(calc.second(), 4)\n"})
        self.comment(75, bad, "Fix first")
        self.associate(75, bad, fixed)
        self.settle_out("p75a", bad, 50, titles=["Fix first"], slug="o/proj#75")
        self.settle_out("p75b", fixed, 55, clean=True, merged=True, slug="o/proj#75")
        result = self.run_import()
        self.assertEqual(result["ingested"], 0)
        self.assertEqual(result["skipped"][0]["reason"], "finding-not-covered")

    def test_simultaneous_promotion_and_demotion_are_reported(self):
        source = lambda first, second: f"def first():\n    return {first}\n\ndef second():\n    return {second}\n"
        prior = self.commit("prior", {"calc.py": source(1, 1)}, "pr82")
        bad = self.commit("bad", {"calc.py": source(1, 2)})
        fixed = self.commit("fixed", {"calc.py": source(3, 4),
            "tests/test_calc82.py": "import unittest\nimport calc\nclass T(unittest.TestCase):\n    def test_first(self):\n        self.assertEqual(calc.first(), 3)\n    def test_second(self):\n        self.assertEqual(calc.second(), 4)\n"})
        self.comment(82, bad, "Fix first", line=2, test_names=("test_first",))
        self.comment(82, bad, "Fix second", line=5, ident=1, test_names=())
        self.associate(82, prior, bad, fixed)
        self.settle_out("p82a", bad, 50, titles=["Fix first", "Fix second"], slug="o/proj#82")
        self.settle_out("p82b", fixed, 55, clean=True, merged=True, slug="o/proj#82")
        self.assertEqual(self.run_import()["ingested"], 1)
        original = next(iter(c.load_store(self.store)[1]["failures"].values()))
        self.comment(82, prior, "Fix first", line=2, ident=2, test_names=("test_first",))
        self.comments["repos/o/proj/pulls/82/comments?per_page=100"].append(dict(
            id=101083, in_reply_to_id=1083, user=dict(login="t"), body="Fixed and guarded by test_second"))
        result = self.run_import()
        self.assertEqual([row["reason"] for row in result["skipped"]],
                         ["newly-proven-findings-not-updated", "attribution-changed-in-stored-lesson"])
        self.assertEqual(next(iter(c.load_store(self.store)[1]["failures"].values())), original)

    def test_stored_finding_reports_demoted_attribution_without_rewriting(self):
        prior = self.commit("prior", {"calc.py": "def total():\n    return 1\n"}, "pr76")
        bad = self.commit("bad", {"notes.txt": "unchanged finding\n"})
        fixed = self.commit("fixed", {"calc.py": "def total():\n    return 2\n",
                                       "tests/test_calc76.py": TEST.format(mod="calc", check="self.assertEqual(calc.total(), 2)")})
        self.comment(76, bad, "Fix total", ident=1)
        self.associate(76, prior, bad, fixed)
        self.settle_out("p76a", bad, 50, titles=["Fix total"], slug="o/proj#76")
        self.settle_out("p76b", fixed, 55, clean=True, merged=True, slug="o/proj#76")
        self.assertEqual(self.run_import()["ingested"], 1)
        original = next(iter(c.load_store(self.store)[1]["failures"].values()))
        self.comment(76, prior, "Fix total")
        result = self.run_import()
        self.assertEqual(result["ingested"], 0)
        self.assertEqual(result["skipped"][0]["reason"], "attribution-changed-in-stored-lesson")
        self.assertEqual(result["skipped"][0]["corrected_attribution"][0]["status"], "unverified")
        self.assertEqual(next(iter(c.load_store(self.store)[1]["failures"].values())), original)

    def test_failed_replay_preserves_stored_finding_attribution(self):
        bad = self.commit("bad", {"calc.py": "def total():\n    return 1\n"}, "pr83")
        fixed = self.commit("fixed", {"calc.py": "def total():\n    return 2\n",
            "tests/test_calc83.py": TEST.format(mod="calc", check="self.assertEqual(calc.total(), 2)")})
        self.comment(83, bad, "Fix total")
        self.associate(83, bad, fixed)
        self.settle_out("p83a", bad, 50, titles=["Fix total"], slug="o/proj#83")
        self.settle_out("p83b", fixed, 55, clean=True, merged=True, slug="o/proj#83")
        self.assertEqual(self.run_import()["ingested"], 1)
        original = c.load_store(self.store)[1]["failures"]
        for reason in ("replay-timeout", "replay-invalid-output", "replay-failed"):
            with self.subTest(reason=reason), mock.patch.object(scl, "replay", return_value=(None, reason)):
                result = self.run_import()
                self.assertEqual([row["reason"] for row in result["skipped"]], [reason])
                self.assertNotIn("corrected_attribution", result["skipped"][0])
                self.assertEqual(c.load_store(self.store)[1]["failures"], original)

    def test_copying_a_log_does_not_admit_a_later_review_comment(self):
        bad = self.commit("bad", {"calc.py": "def total():\n    return 1\n"}, "pr84")
        fixed = self.commit("fixed", {"calc.py": "def total():\n    return 2\n",
            "tests/test_calc84.py": TEST.format(mod="calc", check="self.assertEqual(calc.total(), 2)")})
        self.comment(84, bad, "Fix total")
        self.comments["repos/o/proj/pulls/84/comments?per_page=100"][0]["created_at"] = "2026-10-31T10:51:01Z"
        self.associate(84, bad, fixed)
        self.settle_out("p84a", bad, 50, titles=["Fix total"], slug="o/proj#84")
        self.settle_out("p84b", fixed, 55, clean=True, merged=True, slug="o/proj#84")
        path = self.settle / "settle-p84a.out"
        completed = path.read_text()
        for recorded_end in (True, False):
            with self.subTest(recorded_end=recorded_end):
                path.write_text(completed if recorded_end else "\n".join(
                    line for line in completed.splitlines() if not line.startswith("end ")) + "\n")
                os.utime(path, (1800000000, 1800000000))
                result = self.run_import()
                self.assertEqual(result["proven"], 0)
                self.assertEqual([row["reason"] for row in result["skipped"]], ["finding-text-missing"])

    def test_helper_assertion_does_not_claim_direct_named_assertion(self):
        bad = self.commit("bad", {"calc.py": "def total():\n    return 1\n"}, "pr77")
        fixed = self.commit("fixed", {"calc.py": "def total():\n    return 2\n",
            "tests/test_calc77.py": "import unittest\nimport calc\nclass T(unittest.TestCase):\n    def test_total(self):\n        self.check_total()\n    def check_total(self):\n        self.assertEqual(calc.total(), 2)\n"})
        self.comment(77, bad, "Fix total")
        self.associate(77, bad, fixed)
        self.settle_out("p77a", bad, 50, titles=["Fix total"], slug="o/proj#77")
        self.settle_out("p77b", fixed, 55, clean=True, merged=True, slug="o/proj#77")
        result = self.run_import()
        self.assertEqual(result["ingested"], 0)
        self.assertEqual(result["skipped"][0]["reason"], "finding-not-covered")

    def test_product_assertion_error_is_not_a_test_assertion(self):
        bad = self.commit("bad", {"calc.py": "def total():\n    assert False, 'sandbox failure'\n"}, "pr80")
        fixed = self.commit("fixed", {"calc.py": "def total():\n    return 2\n",
                                       "tests/test_calc80.py": TEST.format(mod="calc", check="self.assertEqual(calc.total(), 2)")})
        self.comment(80, bad, "Fix total")
        self.associate(80, bad, fixed)
        self.settle_out("p80a", bad, 50, titles=["Fix total"], slug="o/proj#80")
        self.settle_out("p80b", fixed, 55, clean=True, merged=True, slug="o/proj#80")
        self.assertEqual(self.run_import()["ingested"], 0)

    def test_sandbox_error_does_not_prove_a_finding(self):
        bad = self.commit("bad", {"calc.py": "def total():\n    raise RuntimeError('sandbox unavailable')\n"}, "pr71")
        fixed = self.commit("fixed", {"calc.py": "def total():\n    return 3\n",
                                       "tests/test_calc71.py": TEST.format(mod="calc", check="self.assertEqual(calc.total(), 3)")})
        self.comment(71, bad, "Fix total")
        self.associate(71, bad, fixed)
        self.settle_out("p71a", bad, 50, titles=["Fix total"], slug="o/proj#71")
        self.settle_out("p71b", fixed, 55, clean=True, merged=True, slug="o/proj#71")
        self.assertEqual(self.run_import()["ingested"], 0)

if __name__ == "__main__":
    unittest.main()
