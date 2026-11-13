import ast
import json
import os
import re
import subprocess
import sys

FAILURE = "log-written-after-timeout"


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


def called(call):
    return call.func.attr if isinstance(call.func, ast.Attribute) else call.func.id if isinstance(call.func, ast.Name) else None


WRITERS = {"write", "write_text", "write_bytes", "writelines"}


def keyword(call, name):
    return next((item.value for item in call.keywords if item.arg == name), None)


def captures(call):
    if keyword(call, "timeout") is None:
        return False
    if called(call) in ("communicate", "check_output"):
        return True
    if called(call) != "run":
        return False
    capture, stdout = keyword(call, "capture_output"), keyword(call, "stdout")
    return (isinstance(capture, ast.Constant) and capture.value is True
            or stdout is not None and any(isinstance(item, (ast.Name, ast.Attribute)) and "PIPE" in ast.unparse(item)
                                          for item in ast.walk(stdout)))


def handles_timeout(nodes):
    return any(isinstance(node, ast.ExceptHandler) and node.type is not None and "TimeoutExpired" in ast.unparse(node.type)
               for node in nodes)


def lines(tree):
    for scope in scopes(tree):
        nodes = list(own_nodes(scope))
        calls = [node for node in nodes if isinstance(node, ast.Call) and captures(node)]
        if not calls or handles_timeout(nodes):
            continue
        outputs = {name.id for node in nodes if isinstance(node, ast.Assign) and node.value in calls
                   for target in node.targets for name in ast.walk(target) if isinstance(name, ast.Name)}
        for node in nodes:
            if isinstance(node, ast.Call) and called(node) in WRITERS and any(
                    isinstance(item, ast.Attribute) and item.attr in ("stdout", "stderr", "output")
                    or isinstance(item, ast.Name) and item.id in outputs
                    for argument in node.args for item in ast.walk(argument)):
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
