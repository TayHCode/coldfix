import json
import os
import re
import subprocess
import sys

FAILURE = "lint-exit-code-only"


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


PATTERN = re.compile(r"^\s*return\s+\(?\s*[\w.]*(?:exit_?code|returncode|exitCode|exit_status)\s*"
                     r"(?:===?\s*1|in\s*[(\[{]\s*0\s*,\s*1\s*,?\s*[)\]}])\s*\)?\s*;?\s*(?:#.*|//.*)?$")


def applies(path, text):
    return re.search(r"(?i)lint", path + "\n" + text) is not None


FUNCTION = re.compile(r"^\s*(?:async\s+)?(?:def|function)\b|=>")
DIAGNOSTIC = re.compile(r"(?i)usage:|\berror:")
READS_OUTPUT = re.compile(r"\b(?:stdout|stderr|output)\b")


def guarded(lines, index):
    for line in reversed(lines[:index]):
        if DIAGNOSTIC.search(line):
            return True
        if FUNCTION.search(line):
            return False
        if READS_OUTPUT.search(line):
            return True
    return False


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
            lines = text.split("\n")
            found += [f"{path}:{number}" for number, line in enumerate(lines, 1)
                      if PATTERN.search(line) and not guarded(lines, number - 1)]
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
