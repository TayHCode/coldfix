import json
import os
import re
import subprocess
import sys

FAILURE = "last-commit-range"


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


PATTERN = re.compile(r"\bgit\b.*\b(?:diff|diff-tree|log)\b.*\bHEAD(?:~1?|\^1?)(?![\w~^])")


def applies(path, text):
    return path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml"))


def hits():
    found = []
    for path in tracked_files():
        if not regular(path):
            continue
        content = read(path)
        if b"\0" in content:
            continue
        text = content.decode("utf-8", "replace")
        if applies(path, text):
            found += [f"{path}:{number}" for number, line in enumerate(text.split("\n"), 1) if PATTERN.search(line)]
    return found


def main():
    try:
        found = hits()
    except Exception as error:
        print(json.dumps({"status": "INCOMPLETE", "reason": repr(error)}))
        return 2
    print(json.dumps({"status": "FAIL", "failure": FAILURE, "hits": found} if found else {"status": "PASS", "hits": []}))
    return 1 if found else 0


sys.exit(main())
