import ast
import json
import os
import subprocess
import sys

FAILURE = "uncaught-cli-json"


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


def called(call):
    return call.func.attr if isinstance(call.func, ast.Attribute) else call.func.id if isinstance(call.func, ast.Name) else None


DECODE = {"ValueError", "JSONDecodeError", "Exception", "BaseException"}
DEPTH = {"RecursionError", "RuntimeError", "Exception", "BaseException"}
TRIES = tuple(kind for kind in (ast.Try, getattr(ast, "TryStar", None)) if kind)


def cli_input(node):
    if isinstance(node, ast.IfExp):
        return cli_input(node.body) or cli_input(node.orelse)
    if isinstance(node, ast.BoolOp):
        return any(cli_input(value) for value in node.values)
    if isinstance(node, ast.Subscript):
        node = node.value
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name):
            return node.func.id == "input"
        node = node.func.value if isinstance(node.func, ast.Attribute) and node.func.attr == "read" else None
    return (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and (node.value.id == "sys" and node.attr in ("stdin", "argv") or node.value.id in ("args", "arguments", "options", "opts")))


def json_read(node):
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("load", "loads")
            and isinstance(node.func.value, ast.Name) and node.func.value.id == "json" and node.args and cli_input(node.args[0]))


def caught(handlers):
    names = set()
    for handler in handlers:
        if handler.type is None:
            return DECODE | DEPTH
        names |= {item.id if isinstance(item, ast.Name) else item.attr
                  for item in ast.walk(handler.type) if isinstance(item, (ast.Name, ast.Attribute))}
    if any(name.isupper() for name in names):
        return DECODE | DEPTH
    return names


def guards(node, parents):
    handlers = []
    while node in parents and not isinstance(node, FUNCTIONS):
        parent = parents[node]
        if isinstance(parent, TRIES) and node in parent.body:
            handlers += parent.handlers
        node = parent
    return handlers, node


def safe(handlers):
    names = caught(handlers)
    return bool(names & DECODE) and bool(names & DEPTH)


def lines(tree):
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    for node in ast.walk(tree):
        if not json_read(node):
            continue
        handlers, enclosing = guards(node, parents)
        if safe(handlers):
            continue
        callers = [call for call in ast.walk(tree) if isinstance(enclosing, FUNCTIONS) and isinstance(call, ast.Call)
                   and called(call) == enclosing.name]
        if callers and all(safe(handlers + guards(call, parents)[0]) for call in callers):
            continue
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
