import ast
import re
import json
import os
import subprocess
import sys

FAILURE = "nested-unbounded-quantifier"


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


try:
    import re._parser as parser
    import re._constants as constants
except ImportError:
    import sre_parse as parser
    import sre_constants as constants

FUNCTIONS = {"compile", "search", "match", "fullmatch", "findall", "finditer", "sub", "subn", "split"}
FLAGS_AT = {"compile": 1, "sub": 4, "subn": 4, "split": 3}
REPEATS = {constants.MAX_REPEAT, constants.MIN_REPEAT}


SAMPLE = [chr(code) for code in range(128)] + [" ", "é", " ", "中"]
CATEGORIES = {constants.CATEGORY_DIGIT: r"\d", constants.CATEGORY_NOT_DIGIT: r"\D", constants.CATEGORY_SPACE: r"\s",
              constants.CATEGORY_NOT_SPACE: r"\S", constants.CATEGORY_WORD: r"\w", constants.CATEGORY_NOT_WORD: r"\W"}
ZERO_WIDTH = {constants.AT, constants.ASSERT, constants.ASSERT_NOT}


def unbounded_repeat(op, value):
    return op in REPEATS and value[1] == constants.MAXREPEAT


def flatten(sequence):
    items = []
    for op, value in sequence:
        items += flatten(value[-1]) if op == constants.SUBPATTERN else [(op, value)]
    return items


def matches(op, value, char, flags):
    if op == constants.LITERAL:
        return ord(char) == value or bool(flags & re.IGNORECASE) and char.lower() == chr(value).lower()
    if op == constants.NOT_LITERAL:
        return not matches(constants.LITERAL, value, char, flags)
    if op == constants.ANY:
        return char != "\n" or bool(flags & re.DOTALL)
    if op == constants.RANGE:
        return value[0] <= ord(char) <= value[1] or bool(flags & re.IGNORECASE) and any(
            value[0] <= ord(other) <= value[1] for other in (char.lower(), char.upper()))
    if op == constants.CATEGORY:
        return bool(re.fullmatch(CATEGORIES[value], char))
    if op == constants.IN:
        negate = bool(value) and value[0][0] == constants.NEGATE
        return any(matches(item, argument, char, flags) for item, argument in value[negate:]) != negate
    raise KeyError(op)


def charset(item, flags):
    try:
        return {char for char in SAMPLE if matches(*item, char, flags)}
    except (KeyError, TypeError):
        return None


def first(sequence, flags):
    for op, value in flatten(sequence):
        if op in ZERO_WIDTH:
            continue
        if op == constants.BRANCH:
            sets = [first(branch, flags) for branch in value[1]]
            return None if None in sets else set().union(*sets)
        if op in REPEATS and value[0] > 0:
            return first(value[2], flags)
        return None if op in REPEATS else charset((op, value), flags)
    return None


def ambiguous(sequence, wrap, flags):
    for index, (op, value) in enumerate(sequence):
        rest = sequence[index + 1:]
        if op == constants.BRANCH:
            if any(ambiguous(flatten(branch) + rest, wrap, flags) for branch in value[1]):
                return True
        elif unbounded_repeat(op, value):
            inner = flatten(value[2])
            chars = charset(inner[0], flags) if len(inner) == 1 else None
            following = first(rest, flags) if any(item[0] not in ZERO_WIDTH for item in rest) else wrap
            if chars is None or following is None or chars & following:
                return True
        elif op != getattr(constants, "ATOMIC_GROUP", None) and any(unbounded(child) for child in children(op, value)):
            return True
    return False


def children(op, value):
    if op in REPEATS or op == getattr(constants, "POSSESSIVE_REPEAT", None):
        return [value[2]]
    if op == constants.SUBPATTERN:
        return [value[-1]]
    if op == constants.BRANCH:
        return value[1]
    if op in (constants.ASSERT, constants.ASSERT_NOT):
        return [value[1]]
    if op == constants.GROUPREF_EXISTS:
        return [part for part in value[1:] if part is not None]
    return []


def unbounded(pattern):
    for op, value in pattern:
        if unbounded_repeat(op, value):
            return True
        if op != getattr(constants, "ATOMIC_GROUP", None) and any(unbounded(child) for child in children(op, value)):
            return True
    return False


def scoped(op, value, flags):
    return (flags | value[1]) & ~value[2] if op == constants.SUBPATTERN else flags


def starts_at_input_start(sequence, flags):
    while sequence and sequence[0][0] == constants.SUBPATTERN:
        flags = scoped(*sequence[0], flags)
        sequence = sequence[0][1][-1]
    return bool(sequence) and sequence[0][0] == constants.AT and (
        sequence[0][1] == constants.AT_BEGINNING_STRING or sequence[0][1] == constants.AT_BEGINNING and not flags & re.MULTILINE)


def repeats_single_characters(pattern, flags):
    for op, value in pattern:
        if unbounded_repeat(op, value) and not (len(flatten(value[2])) == 1 and charset(flatten(value[2])[0], flags) is not None):
            return False
        if not all(repeats_single_characters(child, flags) for child in children(op, value)):
            return False
    return True


def runs_once_without_backtracking(sequence, flags):
    return starts_at_input_start(sequence, flags) and repeats_single_characters(sequence, flags)


def nested(pattern, flags):
    for op, value in pattern:
        if (unbounded_repeat(op, value) and not runs_once_without_backtracking(value[2], flags) and unbounded(value[2])
                and ambiguous(flatten(value[2]), first(value[2], flags), flags)):
            return True
        if op != getattr(constants, "ATOMIC_GROUP", None) and any(
                nested(child, scoped(op, value, flags)) for child in children(op, value)):
            return True
    return False


def regex_calls(tree):
    modules = {"re", "regex"} | {alias.asname for node in ast.walk(tree) if isinstance(node, ast.Import)
                                 for alias in node.names if alias.name in ("re", "regex") and alias.asname}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in FUNCTIONS
                and isinstance(node.func.value, ast.Name) and node.func.value.id in modules
                and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, (str, bytes))):
            yield node


def flag_value(node):
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return flag_value(node.left) | flag_value(node.right)
    if isinstance(node, ast.Attribute) and isinstance(getattr(re, node.attr, None), re.RegexFlag):
        return int(getattr(re, node.attr))
    if isinstance(node, ast.Constant) and type(node.value) is int:
        return node.value
    return 0


def call_flags(call):
    position = FLAGS_AT.get(call.func.attr, 2)
    values = [keyword.value for keyword in call.keywords if keyword.arg == "flags"] + call.args[position:position + 1]
    return flag_value(values[0]) if values else 0


def hits():
    found = []
    for path, tree in python_trees(tracked_files()):
        for call in regex_calls(tree):
            try:
                pattern = parser.parse(call.args[0].value, call_flags(call))
            except Exception:
                continue
            if nested(pattern, pattern.state.flags):
                found.append(f"{path}:{call.lineno}")
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
