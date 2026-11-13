import json
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.getcwd())
FAILURE = "untracked-worktree"


import shutil
import subprocess
import tempfile


def holds():
    with tempfile.TemporaryDirectory() as temporary:
        copy = os.path.join(temporary, "tree")
        shutil.copytree(os.getcwd(), copy)
        probe = os.path.join(copy, ".worktrees", "probe")
        os.makedirs(probe)
        open(os.path.join(probe, "x"), "w").close()
        subprocess.run(["git", "init", "-q"], cwd=copy, check=True)
        status = subprocess.run(["git", "-c", "core.excludesFile=" + os.devnull, "status", "--porcelain",
                                 "--untracked-files=all"], cwd=copy, check=True, capture_output=True, text=True)
    return ".worktrees/" not in status.stdout


def main():
    try:
        ok = holds()
    except Exception as error:
        print(json.dumps({"status": "INCOMPLETE", "reason": repr(error)}))
        return 2
    print(json.dumps({"status": "PASS"} if ok else {"status": "FAIL", "failure": FAILURE}))
    return 0 if ok else 1


sys.exit(main())
