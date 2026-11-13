import re
import urllib.parse
import json
import os
import subprocess
import sys

FAILURE = "broken-relative-link"


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


INLINE = re.compile(r"!?\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+[^)]*)?\)")
DEFINITION = re.compile(r"^ {0,3}\[[^\]]+\]:\s*<?([^\s>]+)>?")
CODE = re.compile(r"(`+).*?\1")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def targets(text):
    fence = None
    for number, line in enumerate(text.split("\n"), 1):
        opened = FENCE.match(line)
        if fence:
            if opened and opened[1][0] == fence[0] and len(opened[1]) >= len(fence) and not line.strip()[len(opened[1]):].strip():
                fence = None
            continue
        if opened:
            fence = opened[1]
            continue
        line = CODE.sub("", line)
        for target in INLINE.findall(line) + DEFINITION.findall(line):
            yield number, target


def broken(path, target):
    if SCHEME.match(target) or target.startswith(("#", "/", "{", "$", "<")):
        return False
    target = urllib.parse.unquote(target.split("#", 1)[0].split("?", 1)[0])
    if not target:
        return False
    resolved = os.path.normpath(os.path.join(os.path.dirname(path), target))
    return resolved == ".." or resolved.startswith(".." + os.sep) or not os.path.exists(resolved)


def hits():
    found = []
    for path in tracked_files():
        if path.lower().endswith((".md", ".markdown")) and regular(path):
            text = read(path).decode("utf-8", "replace")
            found += [f"{path}:{number}" for number, target in targets(text) if broken(path, target)]
    return sorted(set(found), key=lambda hit: (hit.rsplit(":", 1)[0], int(hit.rsplit(":", 1)[1])))


def main():
    try:
        found = hits()
    except Exception as error:
        print(json.dumps({"status": "INCOMPLETE", "reason": repr(error)}))
        return 2
    print(json.dumps({"status": "FAIL", "failure": FAILURE, "hits": found} if found else {"status": "PASS", "hits": []}))
    return 1 if found else 0


sys.exit(main())
