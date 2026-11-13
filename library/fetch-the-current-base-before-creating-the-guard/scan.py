import ast
import json
import os
import subprocess
import sys

FAILURE = "stale-base-after-merge"


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


def strings(node):
    return [item.value for item in ast.walk(node) if isinstance(item, ast.Constant) and isinstance(item.value, str)]


def called(call):
    return call.func.attr if isinstance(call.func, ast.Attribute) else call.func.id if isinstance(call.func, ast.Name) else None


LOOPS = (ast.For, ast.AsyncFor, ast.While)


def git_step(node, word):
    return any(text == word or f" {word} " in f" {text} " for text in strings(node))


def named(node, word):
    return any(isinstance(item, ast.Call) and word in (called(item) or "").lower() for item in ast.walk(node))


def worktree_from_origin(call):
    texts = strings(call)
    return git_step(call, "worktree") and git_step(call, "add") and any("origin/" in text for text in texts)


def lines(tree):
    for loop in ast.walk(tree):
        if not isinstance(loop, LOOPS):
            continue
        body = ast.Module(body=loop.body, type_ignores=[])
        if not (git_step(body, "merge") or named(body, "merge")) or git_step(body, "fetch") or named(body, "fetch"):
            continue
        yield from (node.lineno for node in ast.walk(body) if isinstance(node, ast.Call) and worktree_from_origin(node))


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
