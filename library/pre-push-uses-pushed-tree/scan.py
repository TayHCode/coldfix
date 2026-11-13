import ast
import json
import os
import re
import subprocess
import sys

FAILURE = "pre-push-reads-working-tree"


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


HOOK = re.compile(r"(?i)pre[-_]?push")
EXISTS = {"exists", "isfile", "lexists", "is_file", "isdir", "is_dir"}
SHELL_LOOP = re.compile(r"\bfor\s+(\w+)\s+in\b|\bread\b((?:\s+-\w+)*(?:\s+\w+)+)")
SHELL_ASSIGN = re.compile(r"\b(\w+)=[\"']?\$\(")
SHELL_TEST = re.compile(r"(?:\[\[?|\btest)\s+-[efsd]\s+\"?\$\{?(\w+)")


def loop_names(node):
    targets = [node.target] if isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)) else []
    return {name.id for target in targets for name in ast.walk(target) if isinstance(name, ast.Name)}


def tested(call):
    if called(call) not in EXISTS:
        return None
    subject = call.args[0] if call.args else call.func.value if isinstance(call.func, ast.Attribute) else None
    if isinstance(subject, ast.Call) and called(subject) in ("Path", "PurePath") and subject.args:
        subject = subject.args[0]
    return subject.id if isinstance(subject, ast.Name) else None


CHANGED = ("--name-only", "diff")


def from_git(node, listed):
    return any(word in text for text in strings(node) for word in CHANGED) or any(
        isinstance(item, ast.Name) and item.id in listed for item in ast.walk(node))


def python_lines(tree):
    listed = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and from_git(node.value, set()):
            listed |= {name.id for target in node.targets for name in ast.walk(target) if isinstance(name, ast.Name)}
    for loop in ast.walk(tree):
        sources = [item for item in ([loop] if isinstance(loop, (ast.For, ast.AsyncFor)) else getattr(loop, "generators", []))
                   if from_git(item.iter, listed)]
        names = set().union(set(), *(loop_names(item) for item in sources))
        if not names:
            continue
        yield from (node.lineno for node in ast.walk(loop) if isinstance(node, ast.Call) and tested(node) in names)


def shell_lines(text):
    names, listed = set(), set()
    for number, line in enumerate(text.split("\n"), 1):
        changed = "--name-only" in line or "git diff" in line
        assigned = set(SHELL_ASSIGN.findall(line))
        listed = listed | assigned if changed else listed - assigned
        from_git = changed or any(re.search(rf"\$\{{?{name}\b", line) for name in listed)
        for loop in SHELL_LOOP.finditer(line):
            bound = {loop.group(1)} if loop.group(1) else {word for word in loop.group(2).split() if not word.startswith("-")}
            names = names | bound if from_git else names - bound
        if any(test.group(1) in names for test in SHELL_TEST.finditer(line)):
            yield number


def hits():
    files = [path for path in tracked_files() if HOOK.search(path)]
    found = [f"{path}:{line}" for path, tree in python_trees(files) for line in sorted(set(python_lines(tree)))]
    for path in files:
        if path.endswith(".py") or not regular(path):
            continue
        content = read(path)
        if b"\0" not in content:
            found += [f"{path}:{line}" for line in shell_lines(content.decode("utf-8", "replace"))]
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
