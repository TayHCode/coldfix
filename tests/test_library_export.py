import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
from unittest import mock

from test_cli import CliCase

LIBRARY = Path(__file__).resolve().parents[1] / "library"
TRANSCRIPT_LESSON = """---
id: L90
slug: name-the-service
title: Name the service before giving setup steps
scope: transcript
level: doc
status: {status}
verification: judged
---
# L90 Name the service before giving setup steps

Identify which service the user means before giving setup steps.

## Fix
Identify which service the user means before giving setup steps.

## Verify
Check that the steps name the service the user asked about.
"""


class LibraryExportTests(CliCase):
    def setUp(self):
        super().setUp()
        self.library = self.cwd / "library"
        self.library.mkdir()
        for name in ("README.md", "rerun.py"):
            shutil.copy(LIBRARY / name, self.library / name)
        shutil.copytree(LIBRARY / "use-the-configured-ssh-alias", self.library / "use-the-configured-ssh-alias")

    def proven_lesson(self, revision=None):
        revision = revision or self.author_lesson()
        if not (self.cwd / "history").exists():
            self.replayed = self.history()
        first = json.loads(self.run_replay(revision, *self.replayed).stdout)
        activated = subprocess.run(shlex.split(first["next"]), cwd=self.cwd, text=True, capture_output=True)
        self.assertEqual(activated.returncode, 0, activated.stderr)
        return json.loads(activated.stdout)["id"]

    def export_library(self, *extra, code=0):
        return self.run_cli("export", "--format", "library", "--output", str(self.library), *extra, code=code)

    def frontmatter(self, name):
        head = (self.library / name / "rule.md").read_text().split("---\n")[1]
        return dict(line.split(": ", 1) for line in head.splitlines())

    def index_rows(self):
        return re.findall(r"^\| \[.+\]\(([^/]+)/rule\.md\) \| ([^|]+?) \|$", (self.library / "README.md").read_text(), re.M)

    def test_a_lesson_proven_on_private_history_joins_as_replayed_once(self):
        self.proven_lesson()
        result = json.loads(self.export_library().stdout)
        self.assertEqual(result["added"], ["verify-the-count"])
        self.assertEqual(self.frontmatter("verify-the-count"), dict(id="verify-the-count", title="Verify the count", proof="replayed"))
        self.assertEqual(sorted(p.name for p in (self.library / "verify-the-count").iterdir()), ["rule.md"])
        rule = (self.library / "verify-the-count" / "rule.md").read_text()
        self.assertIn("Require the expected count.", rule)
        self.assertNotIn("private-evidence-canary", rule)
        self.assertNotIn("The count did not match", rule)
        self.assertNotIn("Set the count to its expected value", rule)
        self.assertNotIn("coldfix:", rule)
        self.assertIn(("verify-the-count", "replayed"), self.index_rows())
        again = json.loads(self.export_library().stdout)
        self.assertEqual((again["added"], again["already_in_library"]), ([], 1))
        self.assertEqual(self.index_rows().count(("verify-the-count", "replayed")), 1)

    def test_an_unproven_lesson_is_refused(self):
        self.author_lesson()
        self.assertEqual(json.loads(self.export_library().stdout)["added"], [])
        self.assertFalse((self.library / "verify-the-count").exists())

    def test_public_staging_is_refused(self):
        self.proven_lesson()
        self.assertIn("release-check", self.export_library("--public", code=2).stderr)
        self.assertFalse((self.library / "verify-the-count").exists())

    def test_a_rule_with_a_heading_is_not_added_twice(self):
        revision = self.proven_lesson()
        rule = "Require the expected count.\n\n## Scope\n\nOnly totals."
        self.proven_lesson(json.loads(self.run_cli("lesson", "--revise", revision, "--rule", rule).stdout)["id"])
        self.assertEqual(json.loads(self.export_library().stdout)["added"], ["verify-the-count"])
        self.assertIn(rule, (self.library / "verify-the-count" / "rule.md").read_text())
        again = json.loads(self.export_library().stdout)
        self.assertEqual((again["added"], again["already_in_library"], again["excluded"]), ([], 1, {}))

    def test_a_rule_containing_the_check_heading_is_not_added_twice(self):
        revision = self.proven_lesson()
        rule = "Require the expected count.\n\n## How to check\n\nCompare totals."
        self.proven_lesson(json.loads(self.run_cli("lesson", "--revise", revision, "--rule", rule).stdout)["id"])
        self.export_library()
        again = json.loads(self.export_library().stdout)
        self.assertEqual((again["added"], again["already_in_library"], again["excluded"]), ([], 1, {}))

    def test_a_rerun_restores_a_missing_index_row(self):
        self.proven_lesson()
        before = (self.library / "README.md").read_text()
        self.export_library()
        (self.library / "README.md").write_text(before)
        again = json.loads(self.export_library().stdout)
        self.assertEqual((again["added"], again["already_in_library"]), ([], 1))
        self.assertEqual(self.index_rows().count(("verify-the-count", "replayed")), 1)

    def test_titles_are_escaped_in_the_index(self):
        lessons = self.cwd / "lessons"
        lessons.mkdir()
        (lessons / "L90-name-the-service.md").write_text(
            TRANSCRIPT_LESSON.format(status="active").replace("title: Name the service", "title: Name A | [B]"))
        self.assertEqual(json.loads(self.export_library("--lessons", str(lessons)).stdout)["added"],
                         ["name-a-b-before-giving-setup-steps"])
        self.assertIn("| [Name A \\| \\[B\\] before giving setup steps](name-a-b-before-giving-setup-steps/rule.md) | judged (opinion) |",
                      (self.library / "README.md").read_text())

    def test_a_lesson_that_names_a_private_repository_is_excluded(self):
        revision = self.proven_lesson()
        revised = json.loads(self.run_cli("lesson", "--revise", revision, "--rule", "Keep example-one counts exact.").stdout)["id"]
        self.proven_lesson(revised)
        result = json.loads(self.export_library().stdout)
        self.assertEqual(result["added"], [])
        self.assertEqual(result["excluded"], {"verify-the-count": "privacy screen: private_repo"})
        self.assertFalse((self.library / "verify-the-count").exists())

    def oss_store(self):
        oss = self.cwd / "oss"
        self.store.rename(oss)
        return oss, sorted((p.relative_to(oss), p.read_bytes() if p.is_file() else None) for p in oss.rglob("*"))

    def test_a_lesson_proven_in_the_oss_store_joins_and_the_oss_store_is_unchanged(self):
        self.proven_lesson()
        oss, before = self.oss_store()
        self.assertEqual(json.loads(self.export_library().stdout)["added"], [])
        result = json.loads(self.export_library("--oss-store", str(oss)).stdout)
        self.assertEqual(result["added"], ["verify-the-count"])
        self.assertIn(("verify-the-count", "replayed"), self.index_rows())
        self.assertEqual(sorted((p.relative_to(oss), p.read_bytes() if p.is_file() else None) for p in oss.rglob("*")), before)
        self.assertIn("OSS store not found", self.export_library("--oss-store", str(self.cwd / "absent"), code=2).stderr)
        self.assertIn("library format", self.run_cli("export", "--output", str(self.cwd / "out"), "--oss-store", str(oss), code=2).stderr)

    def test_configured_oss_store_exports_without_entering_private_delivery(self):
        self.proven_lesson()
        oss, before = self.oss_store()
        with mock.patch.dict(os.environ, COLDFIX_OSS_STORE=str(oss)):
            result = json.loads(self.export_library().stdout)
        self.assertEqual(result["added"], ["verify-the-count"])
        self.assertEqual(self.frontmatter("verify-the-count")["proof"], "replayed")
        with mock.patch.dict(os.environ, COLDFIX_OSS_STORE=str(oss)):
            private = self.run_cli("export", "--format", "briefing", "--output", str(self.cwd / "briefing.md"))
        self.assertEqual(json.loads(private.stdout)["lesson_count"], 0)
        self.assertNotIn("Require the expected count.", (self.cwd / "briefing.md").read_text())
        self.assertEqual(sorted((p.relative_to(oss), p.read_bytes() if p.is_file() else None)
                                for p in oss.rglob("*")), before)

    def test_report_counts_oss_proof_separately_from_private_delivery(self):
        self.proven_lesson()
        oss, before = self.oss_store()
        result = subprocess.run([sys.executable, str(LIBRARY.parent / "learn_report.py"),
                                 "--store", str(self.store), "--oss-store", str(oss)],
                                cwd=self.cwd, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["outside_repos"], dict(active=1, lessons_proven=1, proof="replayed"))
        self.assertEqual(report["lessons_proven"], 0)
        self.assertEqual(report["lessons_delivered"],
                         dict(already_enforced_by_fix=0, added_by_coldfix=0, prose_only=0))
        self.assertEqual(sorted((p.relative_to(oss), p.read_bytes() if p.is_file() else None)
                                for p in oss.rglob("*")), before)

    def test_oss_repository_account_alone_is_screened(self):
        revision = self.proven_lesson(self.author_lesson(repos=("example-account/example-project", "example-other/project-two")))
        self.proven_lesson(json.loads(self.run_cli("lesson", "--revise", revision,
                          "--title", "Follow example-account guidance").stdout)["id"])
        oss, _ = self.oss_store()
        result = json.loads(self.export_library("--oss-store", str(oss)).stdout)
        self.assertEqual(result["added"], [])
        self.assertIn("private_repo", str(result["excluded"]))

    def test_an_oss_store_lesson_naming_its_own_repository_is_excluded(self):
        revision = self.proven_lesson()
        revised = json.loads(self.run_cli("lesson", "--revise", revision, "--rule", "Keep example-one counts exact.").stdout)["id"]
        self.proven_lesson(revised)
        oss, _ = self.oss_store()
        result = json.loads(self.export_library("--oss-store", str(oss)).stdout)
        self.assertEqual(result["added"], [])
        self.assertEqual(result["excluded"], {"verify-the-count": "privacy screen: private_repo"})

    def test_an_oss_lesson_title_already_in_the_library_is_screened_before_its_index_row(self):
        revision = self.proven_lesson()
        retitled = json.loads(self.run_cli("lesson", "--revise", revision, "--title", "Verify the example-one count").stdout)["id"]
        self.proven_lesson(retitled)
        oss, _ = self.oss_store()
        (self.library / "kept").mkdir()
        (self.library / "kept" / "rule.md").write_text("---\nid: kept\ntitle: Kept\nproof: judged\n---\n# Kept\n\nRequire the expected count.\n")
        result = json.loads(self.export_library("--oss-store", str(oss)).stdout)
        self.assertEqual(result["excluded"], {"kept": "privacy screen: private_repo"})
        self.assertNotIn("example-one", (self.library / "README.md").read_text())

    def test_a_proof_fixture_naming_an_oss_store_repository_is_refused(self):
        self.proven_lesson()
        oss, _ = self.oss_store()
        self.assert_still_judged(self.fixture_proof(self.PASSES_ON_GOOD, good=("fixed", "example-one.txt"), extra=("--oss-store", str(oss))),
                                 "privacy screen in fixtures/good/example-one.txt: private_repo")

    def test_export_into_the_oss_store_is_refused(self):
        self.proven_lesson()
        oss, before = self.oss_store()
        inside = oss / "library"
        shutil.copytree(self.library, inside)
        library_before = sorted((p.relative_to(inside), p.read_bytes() if p.is_file() else None) for p in inside.rglob("*"))
        refused = self.run_cli("export", "--format", "library", "--output", str(inside), "--oss-store", str(oss), code=2)
        self.assertIn("outside the OSS store", refused.stderr)
        self.assertEqual(sorted((p.relative_to(inside), p.read_bytes() if p.is_file() else None) for p in inside.rglob("*")), library_before)

    def test_a_transcript_lesson_keeps_its_slug_over_an_oss_lesson(self):
        revision = self.proven_lesson()
        retitled = json.loads(self.run_cli("lesson", "--revise", revision, "--title", "Name the service before giving setup steps").stdout)["id"]
        self.proven_lesson(retitled)
        oss, _ = self.oss_store()
        lessons = self.cwd / "lessons"
        lessons.mkdir()
        (lessons / "L90-name-the-service.md").write_text(TRANSCRIPT_LESSON.format(status="active"))
        result = json.loads(self.export_library("--lessons", str(lessons), "--oss-store", str(oss)).stdout)
        self.assertEqual(result["added"], ["name-the-service-before-giving-setup-steps"])
        self.assertIn("Identify which service", (self.library / "name-the-service-before-giving-setup-steps" / "rule.md").read_text())

    def test_judged_transcript_lessons_join_and_inactive_ones_do_not(self):
        lessons = self.cwd / "lessons"
        lessons.mkdir()
        (lessons / "L90-name-the-service.md").write_text(TRANSCRIPT_LESSON.format(status="active"))
        (lessons / "L91-vetoed.md").write_text(TRANSCRIPT_LESSON.format(status="retired").replace("Identify", "Ask"))
        alias = (self.library / "use-the-configured-ssh-alias" / "rule.md").read_text().split("\n\n")[1]
        (lessons / "L92-already-present.md").write_text(
            TRANSCRIPT_LESSON.format(status="active").replace("Identify which service the user means before giving setup steps.", alias))
        result = json.loads(self.export_library("--lessons", str(lessons)).stdout)
        self.assertEqual((result["added"], result["already_in_library"]), (["name-the-service-before-giving-setup-steps"], 1))
        rule = (self.library / "name-the-service-before-giving-setup-steps" / "rule.md").read_text()
        self.assertIn("## How to check\n\nCheck that the steps name the service the user asked about.", rule)
        readme = (self.library / "README.md").read_text()
        conduct = readme.split("## Agent conduct lessons")[1].split("\n## ")[0]
        self.assertIn("(name-the-service-before-giving-setup-steps/rule.md) | judged (opinion) |", conduct)
        self.assertEqual(self.frontmatter("name-the-service-before-giving-setup-steps")["proof"], "judged (opinion)")

    def test_repo_scoped_proven_lessons_in_the_folder_never_reach_the_library(self):
        lessons = self.cwd / "lessons"
        lessons.mkdir()
        (lessons / "L90-name-the-service.md").write_text(TRANSCRIPT_LESSON.format(status="active"))
        (lessons / "L91-pin-the-runner.md").write_text(
            TRANSCRIPT_LESSON.format(status="active")
            .replace("scope: transcript", "scope: repo:example/trial-private\nrepos: example/trial-private\n"
                     "source: coldfix-proven:lesson-1")
            .replace("verification: judged", "verification: proven")
            .replace("Name the service", "Pin the runner").replace("Identify which service", "Pin the runner"))
        result = json.loads(self.export_library("--lessons", str(lessons)).stdout)
        self.assertEqual(result["added"], ["name-the-service-before-giving-setup-steps"])
        exported = "".join(path.read_text() for path in self.library.rglob("*.md"))
        self.assertNotIn("trial-private", exported)
        self.assertNotIn("Pin the runner", exported)

    PASSES_ON_GOOD ="import json, os, sys\nok = os.path.exists('fixed')\nprint(json.dumps({'status': 'PASS' if ok else 'FAIL'}))\nsys.exit(0 if ok else 1)\n"

    def fixture_proof(self, check, bad=("broken",), good=("fixed",), name="use-the-configured-ssh-alias", extra=()):
        draft = self.cwd / "proofs" / name
        for side, files in (("bad", bad), ("good", good)):
            (draft / "fixtures" / side).mkdir(parents=True, exist_ok=True)
            for file in files:
                (draft / "fixtures" / side / file).write_text("synthetic\n")
        (draft / "check.py").write_text(check)
        return json.loads(self.export_library("--proofs", str(self.cwd / "proofs"), *extra).stdout)

    def assert_still_judged(self, result, reason, name="use-the-configured-ssh-alias"):
        self.assertEqual((result["proved"], result["proof_refused"]), ([], {name: reason}))
        self.assertEqual(self.frontmatter(name)["proof"], "judged (opinion)")
        self.assertIn((name, "judged (opinion)"), self.index_rows())
        self.assertEqual(sorted(p.name for p in (self.library / name).iterdir()), ["rule.md"])

    def test_a_valid_fixture_proof_makes_a_chat_lesson_rerunnable_and_keeps_it_an_opinion(self):
        result = self.fixture_proof(self.PASSES_ON_GOOD)
        self.assertEqual((result["proved"], result["proof_refused"]), (["use-the-configured-ssh-alias"], {}))
        self.assertEqual(self.frontmatter("use-the-configured-ssh-alias")["proof"], "rerunnable (opinion)")
        self.assertIn(("use-the-configured-ssh-alias", "rerunnable (opinion)"), self.index_rows())
        again = self.fixture_proof(self.PASSES_ON_GOOD)
        self.assertEqual(again["proof_refused"], {"use-the-configured-ssh-alias": "not a judged or replayed library lesson"})
        rerun = subprocess.run([sys.executable, str(self.library / "rerun.py"), "use-the-configured-ssh-alias"],
                               text=True, capture_output=True)
        self.assertEqual((rerun.returncode, rerun.stdout),
                         (0, "PASS use-the-configured-ssh-alias\nExecuted proofs: 1\n"))

    def test_a_check_that_passes_on_the_bad_fixture_is_refused(self):
        self.assert_still_judged(self.fixture_proof(self.PASSES_ON_GOOD, bad=("fixed",)),
                                 "check does not exit 1 (FAIL) on fixtures/bad and 0 (PASS) on fixtures/good")

    def test_a_check_that_fails_on_the_good_fixture_is_refused(self):
        self.assert_still_judged(self.fixture_proof(self.PASSES_ON_GOOD, good=("broken",)),
                                 "check does not exit 1 (FAIL) on fixtures/bad and 0 (PASS) on fixtures/good")

    def test_an_incomplete_check_is_refused(self):
        self.assert_still_judged(self.fixture_proof("import sys\nprint('{\"status\": \"INCOMPLETE\"}')\nsys.exit(2)\n"),
                                 "check was INCOMPLETE on a fixture")

    def test_a_fixture_naming_a_private_repository_is_refused(self):
        self.proven_lesson()
        self.assert_still_judged(self.fixture_proof(self.PASSES_ON_GOOD, good=("fixed", "example-one.txt")),
                                 "privacy screen in fixtures/good/example-one.txt: private_repo")

    def test_a_proof_for_a_lesson_that_is_not_judged_is_refused(self):
        result = self.fixture_proof(self.PASSES_ON_GOOD, name="no-such-lesson")
        self.assertEqual((result["proved"], result["proof_refused"]), ([], {"no-such-lesson": "not a judged or replayed library lesson"}))
        self.assertFalse((self.library / "no-such-lesson").exists())

    def test_a_proof_for_a_rerunnable_lesson_is_refused(self):
        (self.library / "kept").mkdir()
        (self.library / "kept" / "rule.md").write_text("---\nid: kept\ntitle: Kept\nproof: rerunnable\n---\n# Kept\n\nKeep it.\n")
        result = self.fixture_proof(self.PASSES_ON_GOOD, name="kept")
        self.assertEqual((result["proved"], result["proof_refused"]), ([], {"kept": "not a judged or replayed library lesson"}))
        self.assertEqual(self.frontmatter("kept")["proof"], "rerunnable")

    def test_a_valid_fixture_proof_makes_a_replayed_lesson_rerunnable(self):
        self.proven_lesson()
        self.export_library()
        result = self.fixture_proof(self.PASSES_ON_GOOD, name="verify-the-count")
        self.assertEqual((result["proved"], result["proof_refused"]), (["verify-the-count"], {}))
        self.assertEqual(self.frontmatter("verify-the-count")["proof"], "rerunnable")
        self.assertIn(("verify-the-count", "rerunnable"), self.index_rows())

    def test_a_lesson_already_in_the_library_as_judged_takes_the_level_of_its_source(self):
        self.proven_lesson()
        self.export_library()
        lessons = self.cwd / "lessons"
        lessons.mkdir()
        (lessons / "L90-name-the-service.md").write_text(TRANSCRIPT_LESSON.format(status="active"))
        self.export_library("--lessons", str(lessons))
        for name in ("verify-the-count", "name-the-service-before-giving-setup-steps"):
            rule = self.library / name / "rule.md"
            rule.write_text(re.sub(r"^proof: .*$", "proof: judged", rule.read_text(), count=1, flags=re.M))
        readme = self.library / "README.md"
        readme.write_text(re.sub(r"(/rule\.md\) \| )(?:replayed|judged \(opinion\))( \|)$", r"\1judged\2", readme.read_text(), flags=re.M))
        self.assertEqual(json.loads(self.export_library("--lessons", str(lessons)).stdout)["added"], [])
        self.assertEqual(self.frontmatter("verify-the-count")["proof"], "replayed")
        self.assertEqual(self.frontmatter("name-the-service-before-giving-setup-steps")["proof"], "judged (opinion)")
        rows = dict(self.index_rows())
        self.assertEqual((rows["verify-the-count"], rows["name-the-service-before-giving-setup-steps"]), ("replayed", "judged (opinion)"))

    def test_the_published_proof_is_the_snapshot_that_was_screened_and_run(self):
        late = self.cwd / "proofs" / "use-the-configured-ssh-alias" / "fixtures" / "good" / "late-example-one.txt"
        check = "import os, pathlib\npathlib.Path(os.environ['LATE_FIXTURE']).write_text('unscreened')\n" + self.PASSES_ON_GOOD
        with mock.patch.dict(os.environ, {"LATE_FIXTURE": str(late)}):
            result = self.fixture_proof(check)
        self.assertEqual(result["proved"], ["use-the-configured-ssh-alias"])
        self.assertTrue(late.exists())
        self.assertEqual(sorted(p.name for p in (self.library / "use-the-configured-ssh-alias" / "fixtures" / "good").iterdir()),
                         ["fixed"])

    def test_a_symlinked_fixtures_folder_is_refused(self):
        target = self.cwd / "elsewhere"
        for side, file in (("bad", "broken"), ("good", "fixed")):
            (target / side).mkdir(parents=True)
            (target / side / file).write_text("synthetic\n")
        draft = self.cwd / "proofs" / "use-the-configured-ssh-alias"
        draft.mkdir(parents=True)
        (draft / "check.py").write_text(self.PASSES_ON_GOOD)
        (draft / "fixtures").symlink_to(target)
        result = json.loads(self.export_library("--proofs", str(self.cwd / "proofs")).stdout)
        self.assert_still_judged(result, "proof contains a symlink")

    def test_an_empty_fixture_folder_naming_a_private_repository_is_refused(self):
        self.proven_lesson()
        (self.cwd / "proofs" / "use-the-configured-ssh-alias" / "fixtures" / "good" / "example-one").mkdir(parents=True)
        self.assert_still_judged(self.fixture_proof(self.PASSES_ON_GOOD),
                                 "privacy screen in fixtures/good/example-one: private_repo")

    def test_a_bad_side_check_cannot_make_the_good_fixture_pass(self):
        check = ("import os, pathlib\n"
                 "if os.path.basename(os.getcwd()) == 'bad':\n"
                 "    pathlib.Path('../good/fixed').write_text('planted')\n"
                 + self.PASSES_ON_GOOD)
        self.assert_still_judged(self.fixture_proof(check, good=("other",)),
                                 "check does not exit 1 (FAIL) on fixtures/bad and 0 (PASS) on fixtures/good")
