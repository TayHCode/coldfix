import ast
import fnmatch
import re
import json
import os
import subprocess
import sys

FAILURE = "undiscovered-test-file"
FIX = ("Register each listed test file in the config that discovers it: add its path or a matching glob to a vitest "
       "config's include list, or rename the Python file to match test*.py or a configured pattern. Then confirm the "
       "runner's test count goes up.")


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


DEFAULTS = ("test*.py", "*_test.py")
CONFIGURED = re.compile(rb"""(?:\bdiscover\b[^\n]*?\s-p\s+|\bpython_files\s*=\s*)["']?([^\s"']+)""")


def configured_patterns(files):
    patterns = set(DEFAULTS)
    for path in files:
        if regular(path) and os.path.getsize(path) < 1_000_000:
            content = read(path)
            if b"\0" not in content:
                patterns.update(match.decode("utf-8", "replace") for match in CONFIGURED.findall(content))
    return patterns


def runs_itself(tree):
    return any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "main"
               and isinstance(node.func.value, ast.Name) and node.func.value.id == "unittest" for node in ast.walk(tree))


def test_classes(tree):
    for node in tree.body:
        if (isinstance(node, ast.ClassDef)
                and any(isinstance(base, ast.Name) and base.id.endswith("TestCase")
                        or isinstance(base, ast.Attribute) and base.attr.endswith("TestCase") for base in node.bases)
                and any(isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test")
                        for item in node.body)):
            yield node


VITEST_CONFIG = re.compile(r"(?:^|/)vitest(?:\.[\w-]+)*\.config\.[cm]?[jt]s$")
VITEST_TEST = re.compile(r"\.(?:test|spec)\.[cm]?[jt]sx?$")
VITEST_IMPORT = re.compile(rb"""(?:from|require\(|import\()\s*["']vitest["']""")
INCLUDE = re.compile(r"\binclude\s*:\s*\[")
CLOSE = re.compile(r"\s*\]")
LITERAL = re.compile(r"""\s*(["'`])([^"'`]*)\1\s*,?""")
COVERAGE = re.compile(r"\bcoverage\s*:\s*\{[^{}]*\}")
SKIPPED = {"node_modules", "dist", "fixtures", "archive"}
DEFAULT_INCLUDE = "**/*.{test,spec}.?(c|m)[jt]s?(x)"


def glob_regex(pattern):
    out, index = "", 0
    while index < len(pattern):
        char = pattern[index]
        if pattern.startswith("**/", index):
            out, index = out + "(?:.*/)?", index + 3
            continue
        if pattern.startswith("**", index):
            out, index = out + ".*", index + 2
            continue
        if char in "+!*" and pattern.startswith("(", index + 1):
            raise ValueError("unsupported extglob " + pattern)
        if char in "?@" and pattern.startswith("(", index + 1):
            end = pattern.index(")", index)
            out += "(?:" + "|".join(map(re.escape, pattern[index + 2:end].split("|"))) + ")" + ("?" if char == "?" else "")
            index = end + 1
            continue
        if char == "{":
            end = pattern.index("}", index)
            out += "(?:" + "|".join(map(re.escape, pattern[index + 1:end].split(","))) + ")"
            index = end + 1
            continue
        if char == "[":
            end = pattern.index("]", index)
            out += pattern[index:end + 1]
            index = end + 1
            continue
        out += "[^/]*" if char == "*" else "[^/]" if char == "?" else re.escape(char)
        index += 1
    return re.compile(out + "$")


def vitest_includes(path):
    text = COVERAGE.sub("", read(path).decode("utf-8", "replace"))
    starts = list(INCLUDE.finditer(text))
    if not starts:
        return [glob_regex(DEFAULT_INCLUDE)]
    globs = []
    for start in starts:
        index = start.end()
        while item := LITERAL.match(text, index):
            globs.append(item.group(2))
            index = item.end()
        if not CLOSE.match(text, index):
            return None
    try:
        return [glob_regex(item.removeprefix("./")) for item in globs]
    except (ValueError, re.error):
        return None


def unregistered_vitest(files):
    configs = [path for path in files if VITEST_CONFIG.search(path) and regular(path)]
    if not configs:
        return []
    matchers = []
    for config in configs:
        globs = vitest_includes(config)
        if globs is None:
            return []
        base = os.path.dirname(config)
        matchers += [(base + "/" if base else "", regex) for regex in globs]
    found = []
    for path in files:
        if not VITEST_TEST.search(path) or SKIPPED.intersection(path.split("/")[:-1]) or not regular(path):
            continue
        if not VITEST_IMPORT.search(read(path)):
            continue
        if not any(path.startswith(base) and regex.match(path[len(base):]) for base, regex in matchers):
            found.append(f"{path}:1")
    return found


def hits():
    files = tracked_files()
    trees = python_trees(files)
    patterns = configured_patterns(files)
    found = []
    for path, tree in trees:
        if any(fnmatch.fnmatch(os.path.basename(path), pattern) for pattern in patterns) or runs_itself(tree):
            continue
        found += [f"{path}:{node.lineno}" for node in test_classes(tree)]
    return found + unregistered_vitest(files)


def main():
    try:
        found = hits()
    except Exception as error:
        print(json.dumps({"status": "INCOMPLETE", "reason": repr(error)}))
        return 2
    print(json.dumps({"status": "FAIL", "failure": FAILURE, "hits": found, "fix": FIX} if found else {"status": "PASS", "hits": []}))
    return 1 if found else 0


sys.exit(main())
