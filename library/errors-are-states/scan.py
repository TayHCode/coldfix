import ast
import re
import json
import os
import subprocess
import sys

FAILURE = "non-fail-opens-gate"


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


FAILED = re.compile(r"(?i)fail(?:ed|ure)?")
SCRIPT = re.compile(r"""\breturn\b[^;\n]*!==?\s*(["'`])(?i:fail(?:ed|ure)?)\1|"""
                    r"""\bif\s*\([^;\n]*!==?\s*(["'`])(?i:fail(?:ed|ure)?)\2\s*\)\s*\{?\s*return\s+true\b""")


def not_failed(node):
    return (isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], ast.NotEq)
            and any(isinstance(side, ast.Constant) and isinstance(side.value, str) and FAILED.fullmatch(side.value)
                    for side in (node.left, node.comparators[0])))


def opens(node):
    return isinstance(node, ast.If) and not_failed(node.test) and any(
        isinstance(statement, ast.Return) and isinstance(statement.value, ast.Constant) and statement.value.value is True
        for statement in node.body)


def hits():
    files = tracked_files()
    found = [f"{path}:{node.lineno}" for path, tree in python_trees(files) for node in ast.walk(tree)
             if isinstance(node, ast.Return) and not_failed(node.value) or opens(node)]
    for path in files:
        if path.endswith((".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts")) and regular(path):
            lines = read(path).decode("utf-8", "replace").split("\n")
            found += [f"{path}:{number}" for number, line in enumerate(lines, 1) if SCRIPT.search(line)]
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
