#!/usr/bin/env python3
"""List, and with --apply remove, clean git worktrees under <repo>/.worktrees whose work is already in origin/main.

Exit codes: 0 pass, 1 a removal failed, 2 usage or error. --json prints one object with schema_version 1."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

BASE = "origin/main"


class CleanupError(Exception):
    pass


def git(cwd, *args, check=True):
    result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True)
    result.stdout, result.stderr = os.fsdecode(result.stdout), os.fsdecode(result.stderr)
    if check and result.returncode:
        raise CleanupError(f"git {' '.join(args)} failed with exit {result.returncode}")
    return result


def worktrees(repo):
    entries = []
    for block in git(repo, "worktree", "list", "--porcelain", "-z").stdout.rstrip("\0").split("\0\0"):
        entry = {}
        for line in block.split("\0"):
            key, _, value = line.partition(" ")
            entry[key] = value
        entries.append(entry)
    return entries


def merged_by_pr(repo, branch, head):
    origin = git(repo, "remote", "get-url", "origin", check=False)
    url = origin.stdout.strip()
    if url.startswith("git@"):
        url = "ssh://" + url.replace(":", "/", 1)
    match = re.fullmatch(
        r"(?:https?://|ssh://(?:git@)?)([A-Za-z0-9.-]+)(?::[0-9]+)?/"
        r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?", url)
    if origin.returncode or not match:
        return False
    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--repo", f"{match[1]}/{match[2]}", "--state", "merged", "--head", branch, "--base", BASE.split("/", 1)[1],
             "--json", "headRefOid"], cwd=repo, text=True, capture_output=True)
        return result.returncode == 0 and any(pr["headRefOid"] == head for pr in json.loads(result.stdout))
    except (OSError, ValueError, KeyError):
        return False


def decide(repo, entry, is_main):
    path, head = entry["worktree"], entry.get("HEAD", "")
    branch = entry.get("branch", "").removeprefix("refs/heads/") or None
    row = dict(path=path, branch=branch, head=head, decision="keep")
    bound = Path(repo).resolve() / ".worktrees"
    if is_main:
        row["reason"] = "main checkout"
    elif bound not in Path(path).resolve().parents:
        row["reason"] = f"outside {bound}; never removed"
    elif "prunable" in entry:
        row["reason"] = "directory missing; use git worktree prune"
    elif "locked" in entry:
        row["reason"] = "locked"
    elif git(path, "status", "--porcelain", "--untracked-files=all", "--ignore-submodules=none").stdout.strip():
        row["reason"] = "dirty"
    elif any(entry and (entry[0].islower() or entry[0] == "S")
             for entry in git(path, "ls-files", "-v", "-z").stdout.split("\0")):
        row["reason"] = "index has assume-unchanged or skip-worktree entries"
    elif git(repo, "merge-base", "--is-ancestor", head, BASE, check=False).returncode == 0:
        row.update(decision="remove", reason=f"{'branch tip' if branch else 'detached HEAD'} is contained in {BASE}")
    elif branch is None:
        row["reason"] = f"detached HEAD is not contained in {BASE}"
    elif merged_by_pr(repo, branch, head):
        row.update(decision="remove", reason="merged PR (squash or rebase) with this exact head")
    else:
        row["reason"] = f"unmerged or unpushed commits not found in {BASE} or a merged PR"
    return row


def nested_blocker(rows, row):
    if row["decision"] != "remove":
        return None
    parent = Path(row["path"]).resolve()
    for other in rows:
        if other is not row and Path(other["path"]).resolve().is_relative_to(parent) \
                and (other["decision"] != "remove" or other.get("removed") is False):
            return other
    return None


def run(repo, apply):
    entries = worktrees(repo)
    repo = entries[0]["worktree"]
    os.chdir(repo)
    git(repo, "rev-parse", "--verify", "--quiet", BASE + "^{commit}")
    rows = [decide(repo, entry, index == 0) for index, entry in enumerate(entries)]
    for row in sorted(rows, key=lambda row: len(Path(row["path"]).resolve().parts), reverse=True):
        blocker = nested_blocker(rows, row)
        if blocker:
            row.update(decision="keep", reason=f"contains worktree that is not removed: {blocker['path']}")
        if apply and row["decision"] == "remove":
            result = git(repo, "worktree", "remove", row["path"], check=False)
            row["removed"] = result.returncode == 0
            if not row["removed"]:
                row["reason"] += f"; git worktree remove failed: {result.stderr.strip()}"
    return rows


def error_result():
    return json.dumps(dict(schema_version=1, exit_code=2), indent=2)


class Parser(argparse.ArgumentParser):
    def error(self, message):
        argv = sys.argv[1:] if self.argv is None else self.argv
        options = argv[:argv.index("--")] if "--" in argv else argv
        if any(len(flag) > 2 and "--json".startswith(flag) for flag in (arg.partition("=")[0] for arg in options)):
            print(error_result())
        self.exit(2, "worktree_cleanup: invalid arguments; see --help\n")


def main(argv=None):
    parser = Parser(description=__doc__)
    parser.argv = argv
    sys.stdout.reconfigure(errors="backslashreplace")
    parser.add_argument("--repo", default=".", help="any checkout of the repository (default: current directory)")
    parser.add_argument("--apply", action="store_true", help="run git worktree remove (never --force) on the merged, clean worktrees")
    parser.add_argument("--json", action="store_true", help="print JSON instead of plain text")
    args = parser.parse_args(argv)
    try:
        rows = run(args.repo, args.apply)
    except (CleanupError, OSError, RuntimeError) as error:
        message = error if isinstance(error, CleanupError) else "cannot resolve the repository or a worktree path"
        print(f"worktree_cleanup: {message}", file=sys.stderr)
        if args.json:
            print(error_result())
        return 2
    exit_code = 1 if any(row.get("removed") is False for row in rows) else 0
    if args.json:
        print(json.dumps(dict(schema_version=1, exit_code=exit_code, worktrees=rows), indent=2))
    else:
        print("applied" if args.apply else "dry run: nothing changed; pass --apply to remove")
        for row in rows:
            verb = row["decision"]
            if args.apply and verb == "remove":
                verb = "removed" if row["removed"] else "failed"
            elif verb == "remove":
                verb = "would remove"
            print(f"{verb}: {row['path']} ({row['branch'] or 'detached'}): {row['reason']}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
