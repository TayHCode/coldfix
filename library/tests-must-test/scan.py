import ast
import json
import os
import re
import subprocess
import sys

FAILURE = "uncollected-test"


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


def pytest_repo(files):
    for path in files:
        if os.path.basename(path) in ("pytest.ini", "conftest.py"):
            return True
        if path in ("pyproject.toml", "setup.cfg", "tox.ini") and os.path.isfile(path) and not os.path.islink(path):
            with open(path, "rb") as stream:
                if b"pytest" in stream.read():
                    return True
    return False


def test_cases(tree):
    return [node for node in tree.body if isinstance(node, ast.ClassDef)
            and any(isinstance(base, ast.Name) and base.id.endswith("TestCase")
                    or isinstance(base, ast.Attribute) and base.attr.endswith("TestCase") for base in node.bases)]


def imports_pytest(tree):
    return any(isinstance(node, ast.Import) and any(alias.name.split(".")[0] == "pytest" for alias in node.names)
               or isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "pytest"
               for node in ast.walk(tree))


def hits():
    files = tracked_files()
    found, unparsable = [], []
    for path in files:
        if (not (os.path.basename(path).startswith("test") and path.endswith(".py"))
                or not os.path.isfile(path) or os.path.islink(path)):
            continue
        try:
            with open(path, "rb") as stream:
                tree = ast.parse(stream.read())
        except (SyntaxError, ValueError):
            unparsable.append(path)
            continue
        if test_cases(tree) and not imports_pytest(tree):
            methods = [method for cls in test_cases(tree) for method in cls.body
                       if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) and method.name.startswith("test")]
            called = {node.func.id for method in methods for node in ast.walk(method)
                      if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
            found += [f"{path}:{node.lineno}" for node in tree.body
                      if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and re.match(r"test(_|[A-Z0-9]|$)", node.name)
                      and node.name not in called]
    if unparsable:
        raise ValueError("cannot parse " + ", ".join(unparsable))
    return [] if pytest_repo(files) else found


def main():
    try:
        found = hits()
    except Exception as error:
        print(json.dumps({"status": "INCOMPLETE", "reason": repr(error)}))
        return 2
    print(json.dumps({"status": "FAIL", "failure": FAILURE, "hits": found} if found else {"status": "PASS", "hits": []}))
    return 1 if found else 0


sys.exit(main())
