import ast
import re
import json
import os
import subprocess
import sys

FAILURE = "path-prefix-bypass"


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


SEGMENT = re.compile(r"/[\w.-]+(?:/[\w.-]+)*")
SCRIPT = re.compile(r"""\.startsWith\(\s*(["'`])(/[\w.-]+(?:/[\w.-]+)*)\1\s*\)""")


def constants(tree):
    return {target.id: node.value.value for node in tree.body if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
            for target in node.targets if isinstance(target, ast.Name)}


def prefixes(node, names):
    items = node.elts if isinstance(node, ast.Tuple) else [node]
    for item in items:
        value = item.value if isinstance(item, ast.Constant) else names.get(item.id) if isinstance(item, ast.Name) else None
        if isinstance(value, str) and SEGMENT.fullmatch(value):
            yield value


def hits():
    files = tracked_files()
    found = []
    for path, tree in python_trees(files):
        names = constants(tree)
        found += [f"{path}:{node.lineno}" for node in ast.walk(tree)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "startswith"
                  and node.args and any(prefixes(node.args[0], names))]
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
