import re
import json
import os
import subprocess
import sys

FAILURE = "committed-credential"


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


CREDENTIAL = re.compile(
    rb"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    rb"sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16}|"
    rb"xox[baprs]-[A-Za-z0-9-]{10,}|glpat-[A-Za-z0-9_-]{20,}|"
    rb"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})\b|"
    rb"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----|[a-z][a-z0-9+.-]*://[\w.%-]+:[^\s/@${}<>]+@"
)


USERINFO = re.compile(rb"://[^:]+:([^@]+)@")


def values(line):
    found = set()
    for match in CREDENTIAL.finditer(line):
        found.add(match.group())
        found.update(USERINFO.findall(match.group()))
    return found


def redact(path, secrets):
    text = CREDENTIAL.sub(b"[redacted]", path.encode("utf-8", "surrogateescape"))
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, b"[redacted]")
    return text.decode("utf-8", "replace")


def hits():
    found, secrets = [], set()
    for path in tracked_files():
        if regular(path):
            for number, line in enumerate(read(path).split(b"\n"), 1):
                if CREDENTIAL.search(line):
                    found.append((path, number))
                    secrets |= values(line)
    return [f"{redact(path, secrets)}:{number}" for path, number in found]


def main():
    try:
        found = hits()
    except Exception as error:
        print(json.dumps({"status": "INCOMPLETE", "reason": type(error).__name__}))
        return 2
    print(json.dumps({"status": "FAIL", "failure": FAILURE, "hits": found} if found else {"status": "PASS", "hits": []}))
    return 1 if found else 0


sys.exit(main())
