import ast
import json
import os
import subprocess
import sys

FAILURE = "comment-cutoff-ignores-edits"


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


def scopes(tree):
    return [tree] + [node for node in ast.walk(tree) if isinstance(node, FUNCTIONS)]


def own_nodes(scope):
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, FUNCTIONS):
            stack.extend(ast.iter_child_nodes(node))


def strings(node):
    return [item.value for item in ast.walk(node) if isinstance(item, ast.Constant) and isinstance(item.value, str)]


ORDERING = (ast.Lt, ast.LtE, ast.Gt, ast.GtE)


def mentions(node, word):
    return any(word in text for text in strings(node)) or any(
        isinstance(item, ast.Name) and word in item.id or isinstance(item, ast.Attribute) and word in item.attr
        for item in ast.walk(node))


def ordered(node):
    return isinstance(node, ast.Compare) and any(isinstance(op, ORDERING) for op in node.ops)


def lines(tree):
    for scope in scopes(tree):
        compares = [node for node in own_nodes(scope) if ordered(node) and mentions(node, "merged_at")]
        if any(mentions(node, "updated_at") for node in compares):
            continue
        yield from (node.lineno for node in compares if mentions(node, "created_at"))


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
