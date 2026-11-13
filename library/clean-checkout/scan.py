import shutil
import tempfile
import json
import os
import subprocess
import sys

FAILURE = "untracked-worktree"


def tracked_files():
    listed = subprocess.run(["git", "-c", "core.fsmonitor=false", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                            capture_output=True, text=True)
    if listed.returncode == 0:
        return sorted(set(path for path in listed.stdout.split("\0") if path))
    found = []
    for folder, folders, names in os.walk("."):
        folders[:] = sorted(name for name in folders if name != ".git")
        found += [os.path.relpath(os.path.join(folder, name)) for name in sorted(names)]
    return found


def regular(path):
    return os.path.isfile(path) and not os.path.islink(path)


def read(path):
    with open(path, "rb") as stream:
        return stream.read()


def hits():
    if not os.path.isdir(".worktrees") or os.path.islink(".worktrees"):
        return []
    with tempfile.TemporaryDirectory() as temporary:
        if regular(".gitignore"):
            shutil.copyfile(".gitignore", os.path.join(temporary, ".gitignore"))
        subprocess.run(["git", "init", "-q"], cwd=temporary, check=True)
        ignored = subprocess.run(["git", "-c", "core.excludesFile=" + os.devnull, "-c", "core.fsmonitor=false",
                                  "check-ignore", "-q", "--no-index", ".worktrees/probe/x"], cwd=temporary)
    if ignored.returncode not in (0, 1):
        raise RuntimeError("git check-ignore failed")
    return [] if ignored.returncode == 0 else [".gitignore:1"]


def main():
    try:
        found = hits()
    except Exception as error:
        print(json.dumps({"status": "INCOMPLETE", "reason": repr(error)}))
        return 2
    print(json.dumps({"status": "FAIL", "failure": FAILURE, "hits": found} if found else {"status": "PASS", "hits": []}))
    return 1 if found else 0


sys.exit(main())
