import ast
import json
import os
import subprocess
import sys

FAILURE = "bare-tool-under-restricted-path"


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


RUNNERS = {"run", "Popen", "call", "check_call", "check_output", "create_subprocess_exec"}


def keeps_path(value):
    return any("PATH" in text for text in strings(value)) or any(
        isinstance(item, ast.Name) and item.id in ("environ", "PATH")
        or isinstance(item, ast.Attribute) and item.attr in ("environ", "defpath", "get_exec_path")
        for item in ast.walk(value))


def path_values(node):
    if isinstance(node, ast.Dict):
        return [value for key, value in zip(node.keys, node.values)
                if isinstance(key, ast.Constant) and key.value == "PATH"]
    if isinstance(node, ast.Assign):
        return [node.value for target in node.targets
                if isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant) and target.slice.value == "PATH"]
    if isinstance(node, ast.Call):
        return [keyword.value for keyword in node.keywords if keyword.arg == "PATH"]
    return []


def restricting(node):
    return any(not keeps_path(value) for value in path_values(node))


def bare_tool(call, restricted):
    env = next((keyword.value for keyword in call.keywords if keyword.arg == "env"), None)
    if called(call) not in RUNNERS or not call.args or not (
            isinstance(env, ast.Name) and env.id in restricted or env is not None and restricting(env)):
        return False
    command = call.args[0]
    first = command.elts[0] if isinstance(command, (ast.List, ast.Tuple)) and command.elts else command
    return (isinstance(first, ast.Constant) and isinstance(first.value, str) and first.value != ""
            and "/" not in first.value and "\\" not in first.value and " " not in first.value)


def lines(tree):
    for scope in scopes(tree):
        nodes = list(own_nodes(scope))
        restricted = {}
        for node in nodes:
            if isinstance(node, ast.Assign) and (restricting(node) or restricting(node.value)):
                for target in node.targets:
                    name = target.value if isinstance(target, ast.Subscript) else target
                    if isinstance(name, ast.Name):
                        restricted.setdefault(name.id, node.lineno)
            if (isinstance(node, ast.Call) and called(node) == "update" and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and (restricting(node) or any(restricting(argument) for argument in node.args))):
                restricted.setdefault(node.func.value.id, node.lineno)
        yield from (node.lineno for node in nodes if isinstance(node, ast.Call)
                    and bare_tool(node, {name for name, line in restricted.items() if line < node.lineno}))


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
