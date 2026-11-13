import ast
import json
import os
import subprocess
import sys

FAILURE = "jsonl-split-with-splitlines"


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


def splitlines_call(node):
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "splitlines"


def json_names(tree):
    modules, functions = {"json"}, set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.asname or alias.name for alias in node.names if alias.name == "json")
        elif isinstance(node, ast.ImportFrom) and node.module == "json":
            functions.update(alias.asname or alias.name for alias in node.names if alias.name == "loads")
    return modules, functions


def splitlines_loops(tree):
    modules, functions = json_names(tree)
    scope_of = {}

    def visit(node, scope):
        for child in ast.iter_child_nodes(node):
            scope_of[child] = scope
            visit(child, child if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) else scope)

    visit(tree, tree)
    assignments = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    split = len(node.targets) == 1 and splitlines_call(node.value)
                    assignments.setdefault((scope_of[node], target.id), []).append((node.lineno, split and node.value.lineno))

    def split_line(name, loop):
        earlier = [line for line in assignments.get((scope_of[loop], name), []) if line[0] < loop.lineno]
        return max(earlier)[1] if earlier else None

    def loads(call, name):
        return (isinstance(call, ast.Call) and call.args
                and (isinstance(call.func, ast.Attribute) and call.func.attr == "loads"
                     and isinstance(call.func.value, ast.Name) and call.func.value.id in modules
                     or isinstance(call.func, ast.Name) and call.func.id in functions)
                and any(isinstance(part, ast.Name) and part.id == name for part in ast.walk(call.args[0])))

    for node in ast.walk(tree):
        if isinstance(node, (ast.For, ast.AsyncFor)):
            loops = [(node.target, node.iter, node)]
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            loops = [(generator.target, generator.iter, node) for generator in node.generators]
        else:
            continue
        for target, source, body in loops:
            if (isinstance(source, ast.Call) and isinstance(source.func, ast.Name) and source.func.id == "reversed"
                    and len(source.args) == 1):
                source = source.args[0]
            if (isinstance(source, ast.Call) and isinstance(source.func, ast.Name) and source.func.id == "enumerate"
                    and source.args and isinstance(target, ast.Tuple) and len(target.elts) == 2):
                source, target = source.args[0], target.elts[1]
            if not isinstance(target, ast.Name) or not any(loads(call, target.id) for call in ast.walk(body)):
                continue
            if splitlines_call(source):
                yield source.lineno
            elif isinstance(source, ast.Name) and split_line(source.id, body):
                yield split_line(source.id, body)


def hits():
    found, unparsable = [], []
    for path in tracked_files():
        if not path.endswith(".py") or not os.path.isfile(path) or os.path.islink(path):
            continue
        try:
            with open(path, "rb") as stream:
                tree = ast.parse(stream.read())
        except (SyntaxError, ValueError):
            unparsable.append(path)
            continue
        found += [f"{path}:{line}" for line in sorted(set(splitlines_loops(tree)))]
    if unparsable:
        raise ValueError("cannot parse " + ", ".join(unparsable))
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
