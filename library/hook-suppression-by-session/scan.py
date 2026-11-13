import ast
import json
import os
import re
import subprocess
import sys

FAILURE = "shared-session-fallback"


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


SESSION_ID = re.compile(r"(?i)session_?(?:id|key)")


def names_session(node):
    return any(isinstance(item, ast.Name) and SESSION_ID.search(item.id)
               or isinstance(item, ast.Attribute) and SESSION_ID.search(item.attr)
               or isinstance(item, ast.Constant) and isinstance(item.value, str) and SESSION_ID.search(item.value)
               for item in ast.walk(node))


def filled(node):
    return isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.strip() != ""


STATEFUL = re.compile(r"(?i)key|path|file|marker|state|dir")
SINKS = {"join", "Path", "open", "exists", "touch", "mkdir", "makedirs", "write_text", "write_bytes", "read_text",
         "unlink", "remove", "add", "get", "setdefault", "pop"}


def reaches_state(node, parents, scope):
    child = node
    while child in parents and not isinstance(child, ast.stmt):
        parent = parents[child]
        if (isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.Div)
                or isinstance(parent, ast.Call) and child is not parent.func and called(parent) in SINKS
                or isinstance(parent, ast.Subscript) and child is parent.slice):
            return True
        if isinstance(parent, ast.Return):
            return isinstance(scope, FUNCTIONS) and STATEFUL.search(scope.name) is not None
        child = parent
    return False


def used_for_state(node, parents, scope, nodes):
    if reaches_state(node, parents, scope):
        return True
    statement = node
    while statement in parents and not isinstance(statement, ast.stmt):
        statement = parents[statement]
    targets = getattr(statement, "targets", None) or [getattr(statement, "target", None)]
    bound = {name.id for target in targets if target for name in ast.walk(target) if isinstance(name, ast.Name)}
    return any(isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load) and item.id in bound
               and reaches_state(item, parents, scope) for item in nodes)


def fallback(node):
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
        return filled(node.values[-1]) and any(names_session(value) for value in node.values[:-1])
    return (isinstance(node, ast.Call) and called(node) in ("get", "getenv", "setdefault") and len(node.args) >= 2
            and names_session(node.args[0]) and filled(node.args[1]))


def lines(tree):
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    for scope in scopes(tree):
        nodes = list(own_nodes(scope))
        yield from (node.lineno for node in nodes if fallback(node) and used_for_state(node, parents, scope, nodes))


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
