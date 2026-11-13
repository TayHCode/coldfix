import ast
import json
import os
import re
import subprocess
import sys

FAILURE = "cloud-init-status-2-fails"


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


def called(call):
    return call.func.attr if isinstance(call.func, ast.Attribute) else call.func.id if isinstance(call.func, ast.Name) else None


SHELL_FILES = (".sh", ".bash", ".yml", ".yaml")
SHELL = re.compile(r"cloud-init\s+status\b[^|#\n]*\|\|\s*(?:exit|return|false)\b")


def status_call(call):
    return any("cloud-init" in text for text in strings(call)) and any(re.search(r"\bstatus\b", text) for text in strings(call))


def fails_on_nonzero(node):
    if not isinstance(node, ast.Compare) or "returncode" not in ast.unparse(node):
        return False
    values = [node.left, *node.comparators]
    return any(isinstance(op, (ast.NotEq, ast.Gt)) for op in node.ops) and any(
        isinstance(value, ast.Constant) and value.value == 0 for value in values)


def allows_two(nodes):
    return any(isinstance(node, ast.Constant) and node.value == 2 and type(node.value) is int for node in nodes)


def lines(tree):
    for scope in scopes(tree):
        nodes = list(own_nodes(scope))
        calls = [node for node in nodes if isinstance(node, ast.Call) and status_call(node)]
        if not calls:
            continue
        for call in calls:
            check = next((item.value for item in call.keywords if item.arg == "check"), None)
            if called(call) in ("check_call", "check_output") or isinstance(check, ast.Constant) and check.value is True:
                yield call.lineno
        results = {name.id for node in nodes if isinstance(node, ast.Assign) and any(call in ast.walk(node.value) for call in calls)
                   for target in node.targets for name in ast.walk(target) if isinstance(name, ast.Name)}
        if not allows_two(nodes):
            yield from (node.lineno for node in nodes if fails_on_nonzero(node) and any(
                isinstance(item, ast.Name) and item.id in results or item in calls for item in ast.walk(node)))


def hits():
    files = tracked_files()
    found = [f"{path}:{line}" for path, tree in python_trees(files) for line in sorted(set(lines(tree)))]
    for path in files:
        if path.endswith(".py") or not regular(path):
            continue
        content = read(path)
        if b"\0" not in content and (path.endswith(SHELL_FILES) or content.startswith(b"#!")):
            found += [f"{path}:{number}" for number, line in enumerate(content.decode("utf-8", "replace").split("\n"), 1)
                      if SHELL.search(line)]
    return sorted(found, key=lambda hit: (hit.rsplit(":", 1)[0], int(hit.rsplit(":", 1)[1])))


def main():
    try:
        found = hits()
    except Exception as error:
        print(json.dumps({"status": "INCOMPLETE", "reason": repr(error)}))
        return 2
    print(json.dumps({"status": "FAIL", "failure": FAILURE, "hits": found} if found else {"status": "PASS", "hits": []}))
    return 1 if found else 0


sys.exit(main())
