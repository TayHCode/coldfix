import ast
import json
import os
import subprocess
import sys

FAILURE = "umask-filtered-shared-mode"


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


CREATORS = {"mkdir", "makedirs", "touch", "open", "mkfifo"}
RESETS = {"chmod", "lchmod"}


def called(node):
    return node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id if isinstance(node.func, ast.Name) else None


def shared_mode(call):
    if called(call) not in CREATORS or called(call) == "open" and not (
            isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name) and call.func.value.id == "os"):
        return False
    method = isinstance(call.func, ast.Attribute) and not (isinstance(call.func.value, ast.Name) and call.func.value.id == "os")
    position = 0 if method else 2 if called(call) == "open" else 1
    values = [keyword.value for keyword in call.keywords if keyword.arg == "mode"] + call.args[position:position + 1]
    return any(isinstance(value, ast.Constant) and type(value.value) is int and 0 < value.value <= 0o7777
               and value.value & 0o022 for value in values)


def target(call):
    if isinstance(call.func, ast.Attribute) and not (isinstance(call.func.value, ast.Name) and call.func.value.id == "os"):
        return ast.dump(call.func.value)
    named = [keyword.value for keyword in call.keywords if keyword.arg in ("path", "name")]
    return ast.dump(named[0]) if named else ast.dump(call.args[0]) if call.args else None


def descriptors(call, body):
    names = set()
    for statement in body:
        for node in ast.walk(statement):
            if isinstance(node, ast.Assign) and node.value is call:
                names |= {name.id for item in node.targets for name in ast.walk(item) if isinstance(name, ast.Name)}
    for statement in body:
        for node in ast.walk(statement):
            opened = node.value if isinstance(node, ast.Assign) else node.context_expr if isinstance(node, ast.withitem) else None
            bound = node.targets if isinstance(node, ast.Assign) else [node.optional_vars] if isinstance(node, ast.withitem) else []
            if (isinstance(opened, ast.Call) and called(opened) == "fdopen" and opened.args
                    and isinstance(opened.args[0], ast.Name) and opened.args[0].id in names):
                names |= {name.id for item in bound if item for name in ast.walk(item) if isinstance(name, ast.Name)}
    return names


def corrected(call, calls, body):
    own = target(call)
    if any(called(other) == "umask" or called(other) in RESETS and own is not None and target(other) == own for other in calls):
        return True
    names = descriptors(call, body) if called(call) == "open" else set()
    return any(called(other) == "fchmod" and other.args and descriptor(other.args[0]) in names for other in calls)


def descriptor(node):
    if isinstance(node, ast.Call) and not node.args and isinstance(node.func, ast.Attribute) and node.func.attr == "fileno":
        node = node.func.value
    return node.id if isinstance(node, ast.Name) else None


def scopes(tree):
    yield tree, [node for node in tree.body if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node, node.body


def hits():
    found = set()
    for path, tree in python_trees(tracked_files()):
        for scope, body in scopes(tree):
            calls = [node for statement in body for node in ast.walk(statement) if isinstance(node, ast.Call)]
            if scope is not tree:
                inner = {node for child in ast.walk(scope) if child is not scope
                         and isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) for node in ast.walk(child)}
                calls = [call for call in calls if call not in inner]
            found.update((path, call.lineno) for call in calls if shared_mode(call) and not corrected(call, calls, body))
    return [f"{path}:{line}" for path, line in sorted(found)]


def main():
    try:
        found = hits()
    except Exception as error:
        print(json.dumps({"status": "INCOMPLETE", "reason": repr(error)}))
        return 2
    print(json.dumps({"status": "FAIL", "failure": FAILURE, "hits": found} if found else {"status": "PASS", "hits": []}))
    return 1 if found else 0


sys.exit(main())
