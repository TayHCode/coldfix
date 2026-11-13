import ast
import fnmatch
import re
import json
import os
import subprocess
import sys

FAILURE = "undiscovered-test-file"


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


def python_trees(files):
    trees, unparsable = [], []
    for path in files:
        if not path.endswith(".py") or not regular(path):
            continue
        try:
            trees.append((path, ast.parse(read(path))))
        except (SyntaxError, ValueError):
            unparsable.append(path)
    if unparsable:
        raise ValueError("cannot parse " + ", ".join(unparsable))
    return trees


DEFAULTS = ("test*.py", "*_test.py")
CONFIGURED = re.compile(rb"""(?:\bdiscover\b[^\n]*?\s-p\s+|\bpython_files\s*=\s*)["']?([^\s"']+)""")


def configured_patterns(files):
    patterns = set(DEFAULTS)
    for path in files:
        if regular(path) and os.path.getsize(path) < 1_000_000:
            content = read(path)
            if b"\0" not in content:
                patterns.update(match.decode("utf-8", "replace") for match in CONFIGURED.findall(content))
    return patterns


def runs_itself(tree):
    return any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "main"
               and isinstance(node.func.value, ast.Name) and node.func.value.id == "unittest" for node in ast.walk(tree))


def test_classes(tree):
    for node in tree.body:
        if (isinstance(node, ast.ClassDef)
                and any(isinstance(base, ast.Name) and base.id.endswith("TestCase")
                        or isinstance(base, ast.Attribute) and base.attr.endswith("TestCase") for base in node.bases)
                and any(isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test")
                        for item in node.body)):
            yield node


def hits():
    files = tracked_files()
    trees = python_trees(files)
    patterns = configured_patterns(files)
    found = []
    for path, tree in trees:
        if any(fnmatch.fnmatch(os.path.basename(path), pattern) for pattern in patterns) or runs_itself(tree):
            continue
        found += [f"{path}:{node.lineno}" for node in test_classes(tree)]
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
