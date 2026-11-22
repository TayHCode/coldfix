"""worktree_cleanup behavior through its real CLI against temporary git repositories."""

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import worktree_cleanup


CLI = Path(worktree_cleanup.__file__).resolve()
FAKE_GH = '#!/bin/sh\ncat "$FAKE_GH_JSON"\n'


class WorktreeCleanupTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        gh = self.bin / "gh"
        gh.write_text(FAKE_GH)
        gh.chmod(0o755)
        self.gh_json = self.tmp / "gh.json"
        self.gh_json.write_text("[]")
        self.env = dict(os.environ, PATH=f"{self.bin}{os.pathsep}{os.environ['PATH']}",
                        FAKE_GH_JSON=str(self.gh_json), GIT_CONFIG_GLOBAL="/dev/null",
                        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
        self.origin = self.tmp / "origin.git"
        self.repo = self.tmp / "repo"
        self.git(self.tmp, "init", "--bare", "-b", "main", str(self.origin))
        self.git(self.tmp, "clone", "-q", str(self.origin), str(self.repo))
        self.git(self.repo, "checkout", "-q", "-b", "main")
        self.commit(self.repo, "base.txt", "base")
        self.git(self.repo, "push", "-q", "-u", "origin", "main")
        self.git(self.repo, "remote", "set-url", "--push", "origin", str(self.origin))
        self.git(self.repo, "remote", "set-url", "origin", "https://github.com/origin-owner/project.git")
        self.wt = self.repo / ".worktrees"
        (self.repo / ".git" / "info" / "exclude").write_text(".worktrees/\n")

    def git(self, cwd, *args):
        result = subprocess.run(["git", "-C", str(cwd), *args], env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def commit(self, cwd, name, text):
        (Path(cwd) / name).write_text(text)
        self.git(cwd, "add", name)
        self.git(cwd, "commit", "-q", "-m", text)
        return self.git(cwd, "rev-parse", "HEAD")

    def worktree(self, name, *commits, detach=False):
        path = self.wt / name
        if detach:
            self.git(self.repo, "worktree", "add", "-q", "--detach", str(path), "main")
        else:
            self.git(self.repo, "worktree", "add", "-q", "-b", name, str(path), "main")
        for index, text in enumerate(commits):
            self.commit(path, f"{name}-{index}.txt", text)
        return path

    def land(self, *branches, squash=None):
        for branch in branches:
            self.git(self.repo, "merge", "-q", "--no-edit", branch)
        if squash:
            self.git(self.repo, "merge", "-q", "--squash", squash)
            self.git(self.repo, "commit", "-q", "-m", f"squash {squash}")
        self.git(self.repo, "push", "-q", "origin", "main")

    def cleanup(self, *args):
        result = subprocess.run([sys.executable, str(CLI), "--repo", str(self.repo), *args],
                                env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def decisions(self, *args):
        result = json.loads(self.cleanup("--json", *args))
        self.assertEqual((result["schema_version"], result["exit_code"]), (1, 0))
        rows = result["worktrees"]
        return {row["branch"] or Path(row["path"]).name: row for row in rows}

    def scenario(self):
        self.worktree("merged-clean", "m1")
        self.worktree("merged-dirty", "m2")
        self.worktree("unmerged", "u1")
        self.worktree("squashed", "s1", "s2")
        self.worktree("squashed-then-more", "t1")
        self.worktree("merged-detached", "d1", detach=True)
        self.worktree("unmerged-detached", "d2", detach=True)
        (self.wt / "merged-dirty" / "scratch.txt").write_text("uncommitted")
        self.land("merged-clean", "merged-dirty", squash="squashed")
        self.git(self.wt / "merged-detached", "checkout", "-q", "--detach", "main")
        self.commit(self.wt / "squashed-then-more", "t2.txt", "extra after pr")
        tip = self.git(self.wt / "squashed", "rev-parse", "HEAD")
        old = self.git(self.wt / "squashed-then-more", "rev-parse", "HEAD~1")
        self.gh_json.write_text(json.dumps([{"headRefOid": tip}, {"headRefOid": old}]))

    def snapshot(self):
        return (self.git(self.repo, "worktree", "list", "--porcelain"),
                self.git(self.repo, "branch", "--list", "--format=%(refname) %(objectname)"),
                sorted(str(p.relative_to(self.tmp)) for p in (self.wt).rglob("*") if ".git" not in p.parts))

    def test_dry_run_changes_nothing_and_reports_every_worktree(self):
        self.scenario()
        before = self.snapshot()
        rows = self.decisions()
        out = self.cleanup()
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(all("removed" not in row for row in rows.values()))
        self.assertEqual(sorted(k for k, row in rows.items() if row["decision"] == "remove"),
                         ["merged-clean", "merged-detached", "squashed"])
        self.assertIn("dry run", out)
        self.assertEqual(out.count("would remove:"), 3)

    def test_apply_removes_only_clean_merged_worktrees(self):
        self.scenario()
        out = self.cleanup("--apply")
        self.assertEqual(out.count("removed:"), 3)
        wt = self.wt
        self.assertEqual(sorted(p.name for p in wt.iterdir()),
                         ["merged-dirty", "squashed-then-more", "unmerged", "unmerged-detached"])
        self.assertEqual((wt / "merged-dirty" / "scratch.txt").read_text(), "uncommitted")
        self.assertTrue(self.repo.is_dir())
        self.assertIn("merged-clean", self.git(self.repo, "branch", "--list", "merged-clean"))

    def test_kept_reasons(self):
        self.scenario()
        rows = self.decisions("--apply")
        reasons = {name: row["reason"] for name, row in rows.items() if row["decision"] == "keep"}
        self.assertEqual(reasons["main"], "main checkout")
        self.assertEqual(reasons["merged-dirty"], "dirty")
        self.assertIn("unmerged or unpushed", reasons["unmerged"])
        self.assertIn("unmerged or unpushed", reasons["squashed-then-more"])
        self.assertIn("detached HEAD is not contained", reasons["unmerged-detached"])

    def test_untracked_file_keeps_worktree_when_status_hides_untracked_files(self):
        path = self.worktree("hidden", "h1")
        self.land("hidden")
        self.git(self.repo, "config", "status.showUntrackedFiles", "no")
        (path / "scratch.txt").write_text("uncommitted")
        self.assertEqual(self.decisions("--apply")["hidden"]["reason"], "dirty")
        self.assertEqual((path / "scratch.txt").read_text(), "uncommitted")

    def test_apply_is_repeatable(self):
        self.scenario()
        self.cleanup("--apply")
        after = self.snapshot()
        self.cleanup("--apply")
        self.assertEqual(self.snapshot(), after)

    def check_flagged_tracked_file_is_kept(self, flag):
        path = self.worktree("flagged", "merged commit")
        self.land("flagged")
        self.git(path, "update-index", flag, "base.txt")
        tracked = path / "base.txt"
        tracked.write_text("hidden uncommitted edit")
        self.assertEqual(self.git(path, "status", "--porcelain"), "")
        before = self.decisions()["flagged"]
        after = self.decisions("--apply")["flagged"]
        self.assertTrue(tracked.is_file(), "--apply deleted a flagged tracked file")
        self.assertEqual(tracked.read_text(), "hidden uncommitted edit")
        for row in (before, after):
            self.assertEqual(row["decision"], "keep")
            self.assertEqual(row["reason"], "index has assume-unchanged or skip-worktree entries")

    def test_assume_unchanged_edit_keeps_merged_worktree(self):
        self.check_flagged_tracked_file_is_kept("--assume-unchanged")

    def test_skip_worktree_edit_keeps_merged_worktree(self):
        self.check_flagged_tracked_file_is_kept("--skip-worktree")

    def two_removable_worktrees(self):
        self.worktree("a", "a1")
        self.worktree("b", "b1")
        self.worktree("kept", "k1")
        self.land("a", "b")

    def assert_only_kept_remains(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(p.name for p in (self.wt).iterdir()), ["kept"])
        self.assertTrue(self.repo.is_dir())

    def test_apply_with_repo_pointing_at_a_removable_worktree(self):
        self.two_removable_worktrees()
        result = subprocess.run([sys.executable, str(CLI), "--repo", str(self.wt / "a"), "--apply"],
                                env=self.env, text=True, capture_output=True)
        self.assert_only_kept_remains(result)
        self.assertEqual(result.stdout.count("removed:"), 2)

    def test_apply_from_inside_a_removable_worktree_with_default_repo(self):
        self.two_removable_worktrees()
        result = subprocess.run([sys.executable, str(CLI), "--apply"], cwd=self.wt / "a",
                                env=self.env, text=True, capture_output=True)
        self.assert_only_kept_remains(result)
        self.assertEqual(result.stdout.count("removed:"), 2)

    def test_merged_pr_query_targets_origin_not_gh_default(self):
        path = self.worktree("unmerged", "only in other repository")
        head = self.git(path, "rev-parse", "HEAD")
        self.gh_json.write_text(json.dumps([{"headRefOid": head}]))
        log = self.tmp / "gh-args.json"
        self.env["FAKE_GH_LOG"] = str(log)
        self.env["GH_HOST"] = "enterprise.example.invalid"
        (self.bin / "gh").write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "args = sys.argv[1:]\n"
            "Path(os.environ['FAKE_GH_LOG']).write_text(json.dumps(args))\n"
            "repo = args[args.index('--repo') + 1] if '--repo' in args else 'upstream/project'\n"
            "print('[]' if repo == 'github.com/origin-owner/project' else Path(os.environ['FAKE_GH_JSON']).read_text())\n")
        for url in ("https://github.com/origin-owner/project.git",
                    "git@github.com:origin-owner/project.git",
                    "ssh://git@github.com/origin-owner/project.git"):
            with self.subTest(url=url):
                self.git(self.repo, "remote", "set-url", "origin", url)
                row = self.decisions()["unmerged"]
                self.assertEqual(row["decision"], "keep")
                args = json.loads(log.read_text())
                self.assertIn("--repo", args)
                self.assertEqual(args[args.index("--repo") + 1], "github.com/origin-owner/project")
                self.assertTrue(path.is_dir())

    def test_merged_pr_query_uses_enterprise_origin_host(self):
        path = self.worktree("enterprise", "enterprise commit")
        head = self.git(path, "rev-parse", "HEAD")
        self.gh_json.write_text(json.dumps([{"headRefOid": head}]))
        log = self.tmp / "gh-args"
        self.env["FAKE_GH_LOG"] = str(log)
        (self.bin / "gh").write_text(
            '#!/bin/sh\nprintf "%s\\n" "$@" > "$FAKE_GH_LOG"\ncat "$FAKE_GH_JSON"\n')
        for url in ("https://enterprise.example.invalid/sample-org/repo.git",
                    "git@enterprise.example.invalid:sample-org/repo.git",
                    "ssh://git@enterprise.example.invalid:2222/sample-org/repo.git"):
            with self.subTest(url=url):
                self.git(self.repo, "remote", "set-url", "origin", url)
                self.assertEqual(self.decisions()["enterprise"]["decision"], "remove")
                args = log.read_text().splitlines()
                self.assertEqual(args[args.index("--repo") + 1],
                                 "enterprise.example.invalid/sample-org/repo")

    def test_unparseable_origin_keeps_unmerged_worktree(self):
        path = self.worktree("unmerged", "unmerged commit")
        head = self.git(path, "rev-parse", "HEAD")
        self.gh_json.write_text(json.dumps([{"headRefOid": head}]))
        log = self.tmp / "unexpected-gh-call"
        self.env["FAKE_GH_LOG"] = str(log)
        (self.bin / "gh").write_text(
            '#!/bin/sh\ntouch "$FAKE_GH_LOG"\ncat "$FAKE_GH_JSON"\n')
        for url in (str(self.origin), "https:///sample-org/repo", "https://github.com/sample-org",
                    "https://github.com//repo", "https://github.com/sample-org/repo/extra"):
            with self.subTest(url=url):
                self.git(self.repo, "remote", "set-url", "origin", url)
                self.assertEqual(self.decisions("--apply")["unmerged"]["decision"], "keep")
                self.assertTrue(path.is_dir())
                self.assertFalse(log.exists())

    def check_merged_worktree_path(self, separator):
        branch = "merged-separator"
        path = self.worktree(branch, "merged")
        unusual_path = path.with_name("merged" + separator + "path")
        self.git(self.repo, "worktree", "move", str(path), str(unusual_path))
        self.land(branch)
        row = self.decisions()[branch]
        self.assertEqual(row["path"], str(unusual_path))
        self.assertEqual(row["decision"], "remove")
        self.assertTrue(unusual_path.is_dir())
        row = self.decisions("--apply")[branch]
        self.assertEqual(row["path"], str(unusual_path))
        self.assertTrue(row["removed"])
        self.assertFalse(unusual_path.exists())

    def test_merged_worktree_path_with_unicode_line_separator(self):
        self.check_merged_worktree_path("\u2028")

    def test_merged_worktree_path_with_newline(self):
        self.check_merged_worktree_path("\n")

    def nested_pair(self, child_dirty):
        parent = self.worktree("parent", "p1")
        (self.repo / ".git" / "info" / "exclude").write_text(".worktrees/\n")
        child = parent / ".worktrees" / "child"
        self.git(self.repo, "worktree", "add", "-q", "-b", "child", str(child), "main")
        self.commit(child, "child-0.txt", "c1")
        if child_dirty:
            (child / "scratch.txt").write_text("uncommitted")
        self.land("parent", "child")
        return parent, child

    def test_parent_with_dirty_nested_worktree_is_kept(self):
        parent, child = self.nested_pair(child_dirty=True)
        for apply in ((), ("--apply",)):
            rows = self.decisions(*apply)
            self.assertEqual(rows["parent"]["decision"], "keep")
            self.assertIn(str(child), rows["parent"]["reason"])
            self.assertEqual(rows["child"]["reason"], "dirty")
        self.assertEqual((child / "scratch.txt").read_text(), "uncommitted")
        self.assertTrue(parent.is_dir())

    def test_parent_with_removable_nested_worktree_removes_both(self):
        parent, child = self.nested_pair(child_dirty=False)
        rows = self.decisions()
        self.assertEqual([rows["parent"]["decision"], rows["child"]["decision"]], ["remove", "remove"])
        rows = self.decisions("--apply")
        self.assertTrue(rows["child"]["removed"] and rows["parent"]["removed"])
        self.assertFalse(parent.exists())

    def test_merged_worktree_outside_the_bound_is_kept_and_logged(self):
        outside = self.tmp / "legacy" / "merged-outside"
        self.git(self.repo, "worktree", "add", "-q", "-b", "merged-outside", str(outside), "main")
        self.commit(outside, "outside.txt", "o1")
        self.land("merged-outside")
        self.assertIn(f"keep: {outside} (merged-outside): outside", self.cleanup("--apply"))
        self.assertTrue((outside / "outside.txt").is_file())
        row = self.decisions("--apply")["merged-outside"]
        self.assertEqual(row["decision"], "keep")
        self.assertEqual(row["reason"], f"outside {self.wt.resolve()}; never removed")
        self.assertNotIn("removed", row)
        self.assertTrue((outside / "outside.txt").is_file())
        odd = os.fsencode(self.tmp / "legacy") + b"/odd-\xff"
        self.git(self.repo, "worktree", "add", "-q", "-b", "odd-name", os.fsdecode(odd), "main")
        row = self.decisions()["odd-name"]
        self.assertEqual((row["decision"], row["path"]), ("keep", os.fsdecode(odd)))
        self.assertIn("(odd-name): outside", self.cleanup())

    def test_carriage_return_in_a_worktree_path_is_kept_intact(self):
        path = self.wt / "cr\rname"
        self.git(self.repo, "worktree", "add", "-q", "-b", "cr-name", str(path), "main")
        self.commit(path, "cr.txt", "c1")
        self.land("cr-name")
        row = self.decisions("--apply")["cr-name"]
        self.assertEqual((row["path"], row["decision"], row["removed"]), (str(path), "remove", True))
        self.assertFalse(path.exists())

    def test_symlink_loop_in_path_resolution_exits_two_without_echo(self):
        self.addCleanup(os.chdir, os.getcwd())
        for json_flag in ((), ("--json",)):
            with self.subTest(json=bool(json_flag)), \
                    patch.object(Path, "resolve", side_effect=RuntimeError("Symlink loop from secret")), \
                    patch("sys.stdout", new=io.TextIOWrapper(io.BytesIO())) as out, \
                    patch("sys.stderr", new=io.StringIO()) as err:
                code = worktree_cleanup.main(["--repo", str(self.repo), *json_flag])
                out.flush()
                printed = out.buffer.getvalue().decode()
            self.assertEqual((code, err.getvalue()),
                             (2, "worktree_cleanup: cannot resolve the repository or a worktree path\n"))
            self.assertEqual(printed and json.loads(printed),
                             {"schema_version": 1, "exit_code": 2} if json_flag else "")

    def test_errors_exit_two_with_a_versioned_json_result(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        self.git(self.repo, "update-ref", "-d", "refs/remotes/origin/main")
        usage = "worktree_cleanup: invalid arguments; see --help\n"
        cases = [(("--repo", str(repo)), None) for repo in (plain, self.repo, self.tmp / "secret-missing")]
        cases += [(("--secret-flag",), usage), (("--json=secret",), usage), (("--", "--json", "secret"), usage)]
        for args, stderr in cases:
            for json_flag in ((), ("--json",)):
                with self.subTest(args=args, json=bool(json_flag)):
                    result = subprocess.run([sys.executable, str(CLI), *json_flag, *args],
                                            env=self.env, text=True, capture_output=True)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    if stderr:
                        self.assertEqual(result.stderr, stderr)
                    else:
                        self.assertRegex(result.stderr, r"^worktree_cleanup: git [^\n]* failed with exit \d+\n$")
                    self.assertNotIn("secret", result.stdout + result.stderr)
                    self.assertNotIn("Traceback", result.stderr)
                    if json_flag or args == ("--json=secret",):
                        self.assertEqual(json.loads(result.stdout), {"schema_version": 1, "exit_code": 2})
                    else:
                        self.assertEqual(result.stdout, "")

    def test_squash_requires_merged_pr_for_this_head(self):
        self.worktree("squashed", "s1", "s2")
        self.land(squash="squashed")
        self.assertEqual(self.decisions()["squashed"]["decision"], "keep")


if __name__ == "__main__":
    unittest.main()
