import json
import os
import re
import subprocess
import sys

FAILURE = "current-invocation-in-failure-handler"
FIX = ("Read os.environ['MONITOR_INVOCATION_ID'] (systemd sets it for OnFailure= handlers) before falling back to the "
       "unit's current InvocationID, because a later run can replace it before the handler reads the journal. Return "
       "unknown, not an empty set, when the journal cannot be read, and test a later run starting before the handler.")
SOURCES = (".py", ".sh", ".bash", "")
INVOCATION = re.compile(rb"(?<![\w])InvocationID\b")
MONITOR_READ = re.compile(rb"""(?:environ(?:\.get)?\s*[\[(]\s*|getenv\s*\(\s*)["']MONITOR_INVOCATION_ID["']|\$\{?MONITOR_INVOCATION_ID\b""")


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
        if os.path.splitext(path)[1] not in SOURCES or not os.path.isfile(path) or os.path.islink(path):
            continue
        with open(path, "rb") as stream:
            content = stream.read()
        if b"\0" in content or b"journalctl" not in content or MONITOR_READ.search(content):
            continue
        found += [f"{path}:{number}" for number, line in enumerate(content.split(b"\n"), 1) if INVOCATION.search(line)]
    return found


def main():
    try:
        found = hits()
    except Exception as error:
        print(json.dumps({"status": "INCOMPLETE", "reason": repr(error)}))
        return 2
    print(json.dumps({"status": "FAIL", "failure": FAILURE, "hits": found, "fix": FIX} if found else {"status": "PASS", "hits": []}))
    return 1 if found else 0


sys.exit(main())
