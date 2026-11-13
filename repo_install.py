"""Install the Coldfix CI reviewer workflow and git hook into a repository."""

import json
import os
from pathlib import Path
import subprocess
import tempfile

import coldfix as c


HOOKS_PATH = ".githooks"

WORKFLOW = """name: coldfix-review
on:
  pull_request:
    types: [opened, ready_for_review]
permissions:
  pull-requests: write
jobs:
  request-review:
    if: github.event.pull_request.head.repo.full_name == github.repository && !github.event.pull_request.draft
    runs-on: ubuntu-latest
    steps:
      - run: gh pr comment "$PR" --repo "$REPO" --body "@codex review"
        env:
          GH_TOKEN: ${{ github.token }}
          PR: ${{ github.event.pull_request.number }}
          REPO: ${{ github.repository }}
"""

RULES_WORKFLOW = """name: coldfix-rules
on:
  pull_request:
  push:
permissions:
  contents: read
jobs:
  rules:
    if: >-
      (github.event_name == 'push' && github.ref == format('refs/heads/{0}', github.event.repository.default_branch)) ||
      (github.event_name == 'pull_request' && github.event.pull_request.head.repo.full_name == github.repository)
    runs-on: __RUNS_ON__
    steps:
      - uses: actions/checkout@v4
        with:
          persist-credentials: false
          fetch-depth: 0
      - run: python3 .githooks/pre-push
"""

HOOK = r'''#!/usr/bin/env python3
import json
from pathlib import Path
import re
import subprocess
import sys

root = Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], text=True,
                           capture_output=True, check=True).stdout.strip())
registry = root / ".coldlocker" / "rules.json"
rules = json.loads(registry.read_text()) if registry.is_file() else {}
failed = []
unparsed = []
missing = []
for name, entry in sorted(rules.items()):
    path = root / ".coldlocker" / "rules" / (name + ".md")
    if entry.get("pending", True):
        continue
    if not path.is_file():
        missing.append(name)
        continue
    match = re.search(r"^## Verify\n.*?^(`{3,})sh\n(.*?)^\1$", path.read_text(), re.S | re.M)
    if not match:
        unparsed.append(name)
    elif subprocess.run(["bash", "-c", match.group(2)], cwd=root).returncode:
        failed.append(name)
if missing:
    print("coldfix: rule file missing for active rule(s): " + ", ".join(missing), file=sys.stderr)
if unparsed:
    print("coldfix: cannot parse the Verify block of rule(s): " + ", ".join(unparsed), file=sys.stderr)
if failed:
    print("coldfix: failing rule checks: " + ", ".join(failed), file=sys.stderr)
if failed or unparsed or missing:
    sys.exit(1)
'''

FILES = (
    (".github/workflows/coldfix-review.yml", WORKFLOW, 0o644),
    (HOOKS_PATH + "/pre-push", HOOK, 0o755),
    (".github/workflows/coldfix-rules.yml", RULES_WORKFLOW, 0o644),
)


def git_config(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "config", *args], text=True, capture_output=True)


def plan(repo, runs_on):
    c.require((repo / ".git").exists(), "target must be a git repository root")
    steps = []
    for relative, content, mode in FILES:
        content = content.replace("__RUNS_ON__", json.dumps(runs_on))
        path = c.ordinary_path(repo / relative)
        if not path.exists():
            steps.append(("create", relative, path, content, mode))
            continue
        c.require(path.is_file(), relative + " exists and is not a file")
        c.require(path.read_text(encoding="utf-8") == content, relative + " exists with different content; left unchanged")
        steps.append(("chmod", relative, path, None, mode) if path.stat().st_mode & 0o777 != mode
                     else ("unchanged", relative, path, None, mode))
    current = git_config(repo, "--get", "core.hooksPath")
    value = current.stdout.strip()
    c.require(current.returncode in (0, 1), "cannot read git config")
    c.require(value in ("", HOOKS_PATH), "core.hooksPath is already set to another directory; left unchanged")
    steps.append(("unchanged" if value else "set", "git config core.hooksPath " + HOOKS_PATH, None, None, None))
    return steps


def write(path, content, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".coldfix-install-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def apply(repo, steps):
    for action, _, path, content, mode in steps:
        if action == "create":
            write(path, content, mode)
        elif action == "chmod":
            os.chmod(path, mode)
        elif action == "set":
            result = git_config(repo, "--local", "core.hooksPath", HOOKS_PATH)
            c.require(result.returncode == 0, "cannot write git config")


def install(args):
    repo = c.ordinary_path(args.repo)
    steps = plan(repo, args.runs_on)
    if not args.dry_run:
        apply(repo, steps)
    return "\n".join(f"{'would ' if args.dry_run and action != 'unchanged' else ''}{action}: {label}"
                     for action, label, *_ in steps)
