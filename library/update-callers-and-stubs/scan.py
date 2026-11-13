import ast
import json
import os
import subprocess
import sys

FAILURE = "call-passes-removed-keyword"


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


FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


def bound_names(tree, definitions):
    names = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node not in definitions:
            names.append(node.name)
        elif isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            names.append(node.id)
        elif isinstance(node, ast.arg):
            names.append(node.arg)
        elif isinstance(node, ast.alias):
            names.append((node.asname or node.name).split(".")[0])
    return set(names)


def lines(tree):
    top = [node for node in tree.body if isinstance(node, FUNCTIONS)]
    counts = {}
    for node in top:
        counts[node.name] = counts.get(node.name, 0) + 1
    rebound = bound_names(tree, top)
    accepted = {node.name: {item.arg for item in node.args.args + node.args.kwonlyargs}
                for node in top if counts[node.name] == 1 and node.name not in rebound
                and not node.decorator_list and node.args.kwarg is None}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in accepted and any(
                item.arg is not None and item.arg not in accepted[node.func.id] for item in node.keywords):
            yield node.lineno


def hits():
    found = []
    for path, tree in python_trees(tracked_files()):
        found += [f"{path}:{line}" for line in sorted(set(lines(tree)))]
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
