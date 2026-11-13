import json
import os
import re
import subprocess
import sys

FAILURE = "unresolved-merge-marker"
MARKER = re.compile(rb"^(<{7,}|>{7,})(?:[ \t]|\r?$)")


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


def hits():
    found = []
    for path in tracked_files():
        if not os.path.isfile(path) or os.path.islink(path):
            continue
        with open(path, "rb") as stream:
            content = stream.read()
        if b"\0" in content:
            continue
        found += [f"{path}:{number}" for number, line in enumerate(content.split(b"\n"), 1) if MARKER.match(line)]
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
