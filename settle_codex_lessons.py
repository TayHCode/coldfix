#!/usr/bin/env python3
"""Import Codex P1/P2 findings that a later settle fixed, with replay proof, as Coldfix failure records. Rerunnable."""

import argparse
import ast
import difflib
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile

import coldfix as c
from failure_ingest import import_records, record

BADGE = re.compile(r"(?m)^(?P<path>\S+): \*\*<sub><sub>!\[(?P<severity>P[12]) Badge\][^\n]*?</sub></sub>\s+(?P<title>.+?)\*\*\s*$")
COMMENT_TITLE = re.compile(r"\*\*<sub><sub>!\[P[12] Badge\][^\n]*?</sub></sub>\s+(?P<title>.+?)\*\*")
START = re.compile(r"(?m)^start (\S+)(?: \S+ (\S+)#(\d+) ([0-9a-f]{40}))?")
END = re.compile(r"(?m)^end (\S+)\s*$")
OPEN = re.compile(r"(?m)^pr: \w+ ([0-9a-f]{40})")
LOG_PATH = re.compile(r"(?m)^log: (\S+?)/\.git/")
URL = re.compile(r"https://github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)")
PASSED = re.compile(r"(?m)^PASS: tests=\d+ head=([0-9a-f]{12,40})(?:\s|$)")
MERGED = re.compile(r"(?m)^MERGED ([0-9a-f]{40})\r?$")
UNCLEAN = re.compile(r"(?m)^no clean codex receipt at head")
TEST_FILE = re.compile(r"(?:^|/)(?:test_[^/]*|[^/]*_test)\.py\Z")
TEST_SUPPORT = re.compile(r"(?:^|/)(?:tests?|fixtures?)/")
TEST_NAME = re.compile(r"\btest_[A-Za-z0-9_]+\b")
HUNK = re.compile(r"(?m)^@@ -(\d+)(?:,(\d+))? ")
LOAD_ERRORS = ("ImportError", "ModuleNotFoundError")
BOT = "chatgpt-codex-connector[bot]"
DIFF_CHARS = 8000
OUTPUT_CHARS = 3000
REPLAY_SECONDS = 600
REPLAY = r"""
import json, os, sys, unittest
sys.path[:0] = ["tests", "."]
root = os.path.realpath(".")
results, executed = {}, {}
class Recorder(unittest.TextTestResult):
    def startTest(self, test):
        super().startTest(test)
        self.lines = set()
        sys.settrace(self.trace)
    def trace(self, frame, event, arg):
        if event == "line":
            filename = os.path.realpath(frame.f_code.co_filename)
            relative = os.path.relpath(filename, root)
            if relative != ".." and not relative.startswith("../"):
                self.lines.add((relative, frame.f_lineno))
        return self.trace
    def stopTest(self, test):
        sys.settrace(None)
        executed[test.id()] = sorted([list(line) for line in self.lines])
        super().stopTest(test)
    def addSuccess(self, test):
        super().addSuccess(test); results.setdefault(test.id(), ["ok", ""])
    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            kind = "fail" if issubclass(err[0], test.failureException) else "error"
            results.setdefault(test.id(), [kind, self._exc_info_to_string(err, test)])
    def addFailure(self, test, err):
        super().addFailure(test, err); results[test.id()] = ["fail", self._exc_info_to_string(err, test)]
    def addError(self, test, err):
        super().addError(test, err); results[test.id()] = ["error", self._exc_info_to_string(err, test)]
suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(name) for name in sys.argv[1:])
summary = unittest.TextTestRunner(stream=open("/dev/null", "w"), resultclass=Recorder).run(suite)
print(json.dumps({"results": results, "executed": executed,
                  "successful": summary.wasSuccessful(), "tests_run": summary.testsRun}))
sys.exit(0 if summary.wasSuccessful() else 1)
"""


@dataclass
class Settle:
    source: str
    occurred: str
    head: str
    slug: str = ""
    pr: int = 0
    repo: str = ""
    passed: bool = False
    merged: bool = False
    findings: list = field(default_factory=list)


def stamp(value):
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(path):
    path = Path(path)
    text = path.read_text(encoding="utf-8", errors="replace")
    opened = OPEN.search(text)
    if not opened:
        return None
    start, link = START.search(text), URL.search(text)
    occurred = stamp(datetime.fromtimestamp(path.stat().st_mtime, timezone.utc))
    head = opened.group(1)
    merged = any(match.group(1) == head for match in MERGED.finditer(text))
    passed = merged or any(head.startswith(match.group(1)) for match in PASSED.finditer(text))
    settle = Settle(path.name, occurred, head, passed=passed, merged=merged)
    reported_at = ""
    if start:
        try:
            settle.occurred = stamp(datetime.fromisoformat(start.group(1)))
            reported_at = settle.occurred
        except ValueError:
            pass
        if start.group(2):
            settle.slug, settle.pr = start.group(2), int(start.group(3))
    if not settle.pr and link:
        settle.slug, settle.pr = link.group(1), int(link.group(2))
    where = LOG_PATH.search(text)
    settle.repo = where.group(1) if where else ""
    finished = END.search(text)
    if finished:
        try:
            value = datetime.fromisoformat(finished.group(1))
            if value.tzinfo is not None and stamp(value) >= reported_at:
                reported_at = stamp(value)
        except ValueError:
            pass
    if UNCLEAN.search(text):
        settle.findings = [{**m.groupdict(), "reported_at": reported_at} for m in BADGE.finditer(text)]
    return settle


def git(repo, *args, timeout=None):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, errors="replace", timeout=timeout)


def gh_api(path):
    result = subprocess.run(["gh", "api", "--paginate", "--slurp", path], capture_output=True, text=True)
    if result.returncode != 0:
        return []
    pages = json.loads(result.stdout)
    return [row for page in pages for row in page] if all(isinstance(page, list) for page in pages) else pages


def slug_of(repo, timeout=None):
    url = git(repo, "remote", "get-url", "origin", timeout=timeout).stdout.strip()
    match = re.search(r"github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?\Z", url)
    return match.group(1) if match else ""


def locate(settle, projects):
    dirs = ([settle.repo] if settle.repo else []) + sorted(str(p.parent) for p in Path(projects).glob("*/.git"))
    for repo in dirs:
        if Path(repo).is_dir() and git(repo, "cat-file", "-e", settle.head + "^{commit}").returncode == 0:
            return repo
    return ""


def resolve(settle, projects, api):
    repo = locate(settle, projects)
    if not repo:
        return "missing-commits"
    settle.repo = repo
    settle.slug = settle.slug or slug_of(repo)
    if not settle.pr and settle.slug:
        numbers = {row["number"] for row in api(f"repos/{settle.slug}/commits/{settle.head}/pulls?per_page=100")}
        settle.pr = next(iter(numbers)) if len(numbers) == 1 else 0
    return "" if settle.slug and settle.pr else "no-pr-join"


def ancestor(repo, older, newer):
    return older != newer and git(repo, "merge-base", "--is-ancestor", older, newer).returncode == 0


def distance(repo, older, newer):
    return int(git(repo, "rev-list", "--count", f"{older}..{newer}").stdout.strip() or 0)


def comment_title(row):
    found = COMMENT_TITLE.search(row["body"])
    return found["title"].strip() if found else None


def comment_text(row):
    body = row["body"].replace("<!-- codex-code-review-finding:v1 -->", "")
    body = COMMENT_TITLE.sub("", body, count=1)
    body = re.sub(r"\n*Useful\?.*\Z", "", body, flags=re.S)
    return " ".join(body.split())


def comment_line(row):
    return row.get("original_line") or row.get("line")


COMMENT_STOPWORDS = {"a", "after", "and", "as", "at", "be", "been", "before", "but", "by", "can", "could",
                     "did", "do", "does", "for", "from", "had", "has", "have", "in", "into", "is", "it",
                     "may", "might", "no", "not", "of", "on", "or", "out", "over", "same", "should", "than",
                     "the", "then", "these", "this", "those", "to", "under", "up", "was", "were", "when",
                     "will", "with", "would", "finding", "issue", "text"}


def related_comment_text(first, second):
    left = set(re.findall(r"[a-z0-9_]+", comment_text(first).lower())) - COMMENT_STOPWORDS
    right = set(re.findall(r"[a-z0-9_]+", comment_text(second).lower())) - COMMENT_STOPWORDS
    shared = left & right
    return len(shared) >= 2 and len(shared) / max(1, min(len(left), len(right))) >= 0.5


def scope_identity(source, line, path):
    if line is None or not path.endswith(".py"):
        return None
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    scopes = [node for node in ast.walk(tree)
              if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
              and min([node.lineno, *(decorator.lineno for decorator in node.decorator_list)]) <= line <= node.end_lineno]
    scopes.sort(key=lambda node: (-(node.end_lineno - min([node.lineno, *(decorator.lineno for decorator in node.decorator_list)])),
                                  min([node.lineno, *(decorator.lineno for decorator in node.decorator_list)])))
    return tuple((type(node).__name__, node.name) for node in scopes)


def statement_identity(source, line):
    tree = ast.parse(source)
    decorators = [decorator for node in ast.walk(tree)
                  if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                  for decorator in node.decorator_list
                  if decorator.lineno <= line <= decorator.end_lineno]
    if decorators:
        node = min(decorators, key=lambda item: (item.end_lineno - item.lineno, item.lineno))
        return ast.dump(node, include_attributes=False)
    cases = [case for node in ast.walk(tree) if isinstance(node, ast.Match) for case in node.cases
             if case.pattern.lineno <= line <= max(case.pattern.end_lineno,
                                                   getattr(case.guard, "end_lineno", case.pattern.end_lineno))]
    if cases:
        case = min(cases, key=lambda item: (item.pattern.end_lineno - item.pattern.lineno,
                                            item.pattern.lineno))
        dump = lambda item: ast.dump(item, include_attributes=False) if item is not None else None
        return ("match_case", dump(case.pattern), dump(case.guard))
    try_nodes = (ast.Try, getattr(ast, "TryStar", ast.Try))
    handlers = [handler for node in ast.walk(tree) if isinstance(node, try_nodes) for handler in node.handlers
                if handler.lineno <= line <= max(handler.lineno, getattr(handler.type, "end_lineno", handler.lineno))]
    if handlers:
        handler = min(handlers, key=lambda item: (getattr(item.type, "end_lineno", item.lineno) - item.lineno,
                                                  item.lineno))
        dump = lambda item: ast.dump(item, include_attributes=False) if item is not None else None
        return ("except_handler", dump(handler.type), handler.name)
    statements = [node for node in ast.walk(tree)
                  if isinstance(node, ast.stmt) and node.lineno <= line <= node.end_lineno]
    if statements:
        node = min(statements, key=lambda item: (item.end_lineno - item.lineno, item.lineno))
        dump = lambda item: ast.dump(item, include_attributes=False) if item is not None else None
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return (type(node).__name__, node.name, dump(node.args), tuple(map(dump, node.decorator_list)),
                    dump(node.returns), node.type_comment, tuple(map(dump, getattr(node, "type_params", ()))))
        if isinstance(node, ast.ClassDef):
            return (type(node).__name__, node.name, tuple(map(dump, node.bases)),
                    tuple(map(dump, node.keywords)), tuple(map(dump, node.decorator_list)),
                    tuple(map(dump, getattr(node, "type_params", ()))))
        if isinstance(node, (ast.If, ast.While)):
            return (type(node).__name__, dump(node.test))
        if isinstance(node, (ast.For, ast.AsyncFor)):
            return (type(node).__name__, dump(node.target), dump(node.iter), node.type_comment)
        if isinstance(node, (ast.With, ast.AsyncWith)):
            return (type(node).__name__, tuple(map(dump, node.items)), node.type_comment)
        if isinstance(node, ast.Match):
            return (type(node).__name__, dump(node.subject))
        return ast.dump(node, include_attributes=False)
    return source.splitlines()[line - 1].strip()


def config_identity(source, line, path):
    lines = source.splitlines(keepends=True)
    offset = sum(len(value) for value in lines[:line - 1])
    line_end = offset + (len(lines[line - 1]) if line <= len(lines) else 0)
    if path.lower().endswith(".json"):
        decoder = json.JSONDecoder()
        members = re.finditer(r'"(?:\\.|[^"\\])*"\s*:', source)
        matches = []
        for member in members:
            value_start = member.end()
            while value_start < len(source) and source[value_start].isspace():
                value_start += 1
            try:
                value, value_end = decoder.raw_decode(source, value_start)
                if member.start() < line_end and offset < value_end:
                    key = json.loads(member.group().rsplit(":", 1)[0])
                    matches.append((member.start(), value_end, key, value))
            except (json.JSONDecodeError, ValueError):
                continue
        if matches:
            on_line = [item for item in matches if offset <= item[0] < line_end]
            selected = on_line or [min(matches, key=lambda item: item[1] - item[0])]
            return json.dumps([(key, value) for _, _, key, value in selected],
                              sort_keys=True, separators=(",", ":"))
        try:
            return json.dumps(json.loads(source), sort_keys=True, separators=(",", ":"))
        except json.JSONDecodeError:
            return source
    return lines[line - 1].strip()


def carried_location(repo, earlier, current, source_cache):
    if not comment_line(earlier) or not comment_line(current):
        return False
    if not earlier["path"].endswith(".py"):
        if comment_text(earlier) != comment_text(current):
            return False
        source_lines = []
        for row in (earlier, current):
            key = (row["original_commit_id"], row["path"])
            if key not in source_cache:
                source_cache[key] = git(repo, "show", f"{key[0]}:{key[1]}").stdout
            lines = source_cache[key].splitlines()
            line = comment_line(row)
            if line > len(lines):
                return False
            source_lines.append(config_identity(source_cache[key], line, row["path"]))
        return source_lines[0] == source_lines[1]
    identities = []
    for row in (earlier, current):
        key = (row["original_commit_id"], row["path"])
        if key not in source_cache:
            source_cache[key] = git(repo, "show", f"{key[0]}:{key[1]}").stdout
        source = source_cache[key]
        lines = source.splitlines()
        line = comment_line(row)
        identity = scope_identity(source, line, row["path"])
        if identity is None or line > len(lines):
            return False
        identities.append((identity, statement_identity(source, line)))
    if identities[0][0] != identities[1][0]:
        return False
    same_text = comment_text(earlier) == comment_text(current)
    same_scope = identities[0][0] == identities[1][0]
    same_source_line = identities[0][1] == identities[1][1]
    return same_scope and same_source_line and (same_text or related_comment_text(earlier, current))


def finding_text(bad, api):
    comments = api(f"repos/{bad.slug}/pulls/{bad.pr}/comments?per_page=100")
    found = []
    used = set()
    source_cache = {}
    for finding in bad.findings:
        reported_at = finding.get("reported_at", bad.occurred)
        same = [row for row in comments if row["id"] not in used and row["user"]["login"] == BOT
                and row["path"] == finding["path"] and comment_title(row) == finding["title"]
                and row["created_at"] <= reported_at]
        at_head = [row for row in same if row["original_commit_id"] == bad.head]
        earlier = [row for row in same if row["original_commit_id"] != bad.head]
        current = min(at_head, key=lambda row: (row["created_at"], row["id"])) if at_head else None
        duplicate = [row for row in earlier if current and row["created_at"] <= current["created_at"]
                     and ancestor(bad.repo, row["original_commit_id"], bad.head)
                     and carried_location(bad.repo, row, current, source_cache)]
        if current:
            candidates = duplicate or at_head
        else:
            candidates = [row for row in earlier
                          if ancestor(bad.repo, row["original_commit_id"], bad.head)
                          and carried_location(bad.repo, row,
                                               dict(row, original_commit_id=bad.head, original_line=None), source_cache)]
            if len(candidates) != 1:
                return None
        match = min(candidates, key=lambda row: (row["created_at"], row["id"])) if candidates else None
        if match is None:
            return None
        used.add(match["id"])
        test_names = sorted({name for reply in comments
                             if reply.get("in_reply_to_id") == match["id"]
                             and reply["user"]["login"] != BOT
                             for name in TEST_NAME.findall(reply["body"])})
        found.append(dict(severity=finding["severity"], title=finding["title"], path=match["path"], line=comment_line(match),
                          reviewed_head=match["original_commit_id"],
                          text=match["body"].replace("<!-- codex-code-review-finding:v1 -->", "").strip(),
                          url=match["html_url"], created_at=match["created_at"], test_names=test_names))
    return found


def run_tests(tree, names):
    process = subprocess.Popen([sys.executable, "-B", "-c", REPLAY, *names], cwd=tree, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True, start_new_session=True)
    try:
        stdout, _ = process.communicate(timeout=REPLAY_SECONDS)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        return None, "replay-timeout"
    try:
        result = json.loads(stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        return None, "replay-invalid-output"
    if not isinstance(result, dict) or not isinstance(result.get("results"), dict) or not isinstance(result.get("executed"), dict):
        return None, "replay-invalid-output"
    if type(result.get("successful")) is not bool or type(result.get("tests_run")) is not int or result["tests_run"] < 0:
        return None, "replay-invalid-output"
    if process.returncode != (0 if result["successful"] else 1):
        return None, "replay-invalid-output"
    if result["tests_run"] == 0:
        return None, "replay-no-tests"
    return result, ""


def load_error(test, output):
    tail = output.splitlines()[-2:]
    if "unittest.loader._FailedTest" in test or any(line.startswith(LOAD_ERRORS) for line in tail):
        return True
    if not any(line.startswith("FileNotFoundError") for line in tail):
        return False
    frames = re.findall(r'(?m)^\s+File "([^"]+)", line \d+', output)
    project_frames = [path for path in frames if path.startswith(("./", ".\\"))]
    return bool(project_frames and (TEST_FILE.search(project_frames[-1]) or TEST_SUPPORT.search(project_frames[-1])))


def stage(tree, name, source):
    relative = Path(name)
    target = tree / relative
    for ancestor in reversed(relative.parents):
        if ancestor == Path("."):
            continue
        parent = tree / ancestor
        if parent.is_symlink() or parent.is_file():
            parent.unlink()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)
    else:
        target.unlink(missing_ok=True)
    shutil.copy2(source, target, follow_symlinks=False)


def assertion_site(output, tree, test):
    if not re.search(r"(?m)^AssertionError(?::|$)", output):
        return None
    frames = re.findall(r'(?m)^\s+File "([^"]+)", line (\d+), in (\S+)', output)
    for filename, line, function in reversed(frames):
        relative = os.path.relpath(os.path.realpath(filename), os.path.realpath(tree))
        if relative != ".." and not relative.startswith("../"):
            if not TEST_FILE.search(relative) or function != test.rsplit(".", 1)[-1]:
                return None
            try:
                nodes = list(ast.walk(ast.parse(Path(filename).read_text(encoding="utf-8"))))
            except (OSError, UnicodeError, SyntaxError, ValueError, RecursionError):
                return None
            line = int(line)
            statements = [node for node in nodes if isinstance(node, ast.stmt) and node.lineno == line]
            assertions = [node for node in nodes if getattr(node, "lineno", None) == line and
                          (isinstance(node, ast.Assert) or isinstance(node, ast.Call) and
                           (isinstance(node.func, ast.Attribute) and node.func.attr.startswith("assert") or
                            isinstance(node.func, ast.Name) and node.func.id.startswith("assert")))]
            return (relative, line) if len(statements) == 1 and len(assertions) <= 1 else None
    return None


def changed_line_bytes(bad_tree, fixed_tree, finding):
    path, line = finding["path"], finding["line"]
    relative = Path(path)
    if not path.endswith(".py") or type(line) is not int or relative.is_absolute() or ".." in relative.parts:
        return None
    old_path, new_path = bad_tree / relative, fixed_tree / relative
    if old_path.is_symlink() or new_path.is_symlink() or not old_path.is_file() or not new_path.is_file():
        return None
    if any((root / parent).is_symlink() for root in (bad_tree, fixed_tree) for parent in relative.parents):
        return None
    old, new = old_path.read_bytes().splitlines(keepends=True), new_path.read_bytes().splitlines(keepends=True)
    for operation, i, j, k, n in difflib.SequenceMatcher(a=old, b=new, autojunk=False).get_opcodes():
        if operation == "replace" and j - i == n - k == 1 and i + 1 == line:
            if old.count(old[i]) != 1 or new.count(new[k]) != 1:
                return None
            return new_path, b"".join(new), b"".join(new[:k] + [old[i]] + new[n:])
    return None


def replay(repo, bad, fixed, scratch, findings=()):
    changed = {kind: [name for name in git(repo, "diff", "-z", "--name-only", "--no-renames", f"--diff-filter={kind}", bad, fixed).stdout.split("\0") if name]
               for kind in ("AMT", "D")}
    names, removed = changed["AMT"], changed["D"]
    files = [name for name in names if TEST_FILE.search(name)]
    if not files:
        return None, "no-test-in-fix"
    modules = [name[:-3].replace("/", ".") for name in files]
    Path(scratch).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=scratch) as work:
        trees = {}
        for label, head in (("bad", bad), ("fixed", fixed)):
            trees[label] = Path(work) / label
            trees[label].mkdir()
            archive = subprocess.run(["git", "-C", str(repo), "archive", head], capture_output=True)
            subprocess.run(["tar", "-x", "-C", str(trees[label])], input=archive.stdout, check=True)
        for name in removed:
            if TEST_FILE.search(name) or TEST_SUPPORT.search(name):
                target = trees["bad"] / name
                if target.is_symlink() or not target.is_dir():
                    target.unlink(missing_ok=True)
                else:
                    shutil.rmtree(target)
        for name in [name for name in names if TEST_FILE.search(name) or TEST_SUPPORT.search(name)]:
            stage(trees["bad"], name, trees["fixed"] / name)
        before_run, reason = run_tests(trees["bad"], modules)
        if before_run is None:
            return None, reason
        if before_run["successful"]:
            return None, "no-failing-replay"
        after_run, reason = run_tests(trees["fixed"], modules)
        if after_run is None:
            return None, reason
        if not after_run["successful"]:
            return None, "replay-failed"
        before, after = before_run["results"], after_run["results"]
        causal = set()
        for finding in findings:
            if finding["reviewed_head"] != bad:
                continue
            change = changed_line_bytes(trees["bad"], trees["fixed"], finding)
            if change is None:
                continue
            target, original, reverted = change
            for test, (state, output) in before.items():
                if (state != "fail" or test.rsplit(".", 1)[-1] not in finding.get("test_names", [])
                        or after.get(test, [""])[0] != "ok"):
                    continue
                site = assertion_site(output, trees["bad"], test)
                if site is None:
                    continue
                try:
                    target.write_bytes(reverted)
                    mutation, _ = run_tests(trees["fixed"], [test])
                finally:
                    target.write_bytes(original)
                if (mutation is None or mutation["successful"] or mutation["tests_run"] != 1
                        or mutation["results"].get(test, [""])[0] != "fail"):
                    continue
                if assertion_site(mutation["results"][test][1], trees["fixed"], test) != site:
                    continue
                restored, _ = run_tests(trees["fixed"], [test])
                if (restored and restored["successful"] and restored["tests_run"] == 1
                        and restored["results"].get(test, [""])[0] == "ok"):
                    causal.add((finding["path"], finding["line"], test, site[0], site[1]))
        bad_tree = trees["bad"]
        before = {test: [state, output.replace(str(bad_tree.resolve()), ".").replace(str(bad_tree), ".")]
                  for test, (state, output) in before.items()}
    proven = [(test, state) for test, (state, output) in before.items()
              if state == "fail" and not load_error(test, output) and after.get(test, ["", ""])[0] == "ok"]
    if not proven:
        return None, "no-failing-replay"
    tests = [dict(id=test, bad=state, fixed="ok", executed=before_run["executed"].get(test, []),
                  **({"causal": [dict(path=path, line=line, assertion_path=assertion_path,
                                     assertion_line=assertion_line, reverted="fail", restored="ok")
                                  for path, line, name, assertion_path, assertion_line in sorted(causal) if name == test]}
                     if findings else {}))
             for test, state in proven]
    return dict(files=files, tests=tests, bad_output=before[proven[0][0]][1][-OUTPUT_CHARS:]), ""


def covered(repo, bad, fixed, finding, tests):
    line, path = finding["line"], finding["path"]
    test_names = set(finding.get("test_names", []))
    if line is None or not path.endswith(".py") or not test_names:
        return False
    named_tests = []
    for name in test_names:
        matches = [test for test in tests if test["id"].rsplit(".", 1)[-1] == name]
        if len(matches) == 1:
            named_tests.append(matches[0])
    if not named_tests:
        return False
    hunks = HUNK.findall(git(repo, "diff", "--unified=0", bad, fixed, "--", finding["path"]).stdout)
    changed_line = any(int(count or 1) > 0 and int(start) <= line < int(start) + int(count or 1)
                       for start, count in hunks)
    exercised_line = any(any(proof["path"] == path and proof["line"] == line for proof in test.get("causal", [])) and
                         any(executed_path == path and executed_line == line
                             for executed_path, executed_line in test["executed"])
                         for test in named_tests)
    return changed_line and exercised_line


def prove(repo, bad, candidates, findings, scratch):
    proven, reason, remaining = [], "", list(findings)
    for fixed in candidates:
        proof, reason = replay(repo, bad.head, fixed.head, scratch, findings)
        if proof is None:
            continue
        bound = [f for f in findings if f["reviewed_head"] == bad.head and covered(repo, bad.head, fixed.head, f, proof["tests"])]
        newly_bound = [f for f in remaining if f in bound]
        if newly_bound:
            proof["counterfactuals"] = [dict(test=test["id"], **item)
                                        for test in proof["tests"] for item in test["causal"]
                                        if any(item["path"] == finding["path"] and item["line"] == finding["line"]
                                               and test["id"].rsplit(".", 1)[-1] in finding.get("test_names", [])
                                               for finding in bound)]
            for test in proof["tests"]:
                test.pop("executed")
                test.pop("causal")
            proven.append((fixed, proof, bound, [f for f in findings if f not in bound]))
            remaining = [f for f in remaining if f not in bound]
            if not remaining:
                break
        else:
            reason = "finding-not-covered"
    return proven, "" if proven else reason


def evidence(bad, fixed, findings, unverified, proof, diff):
    return dict(type="settle-codex-lesson", repo=bad.slug, pr=bad.pr, findings=findings, unverified_findings=unverified, bad_head=bad.head, fixed_head=fixed.head,
                failing_settle=dict(log=bad.source, occurred=bad.occurred),
                fixing_settle=dict(log=fixed.source, occurred=fixed.occurred, outcome="merged" if fixed.merged else "passed"),
                diff=diff, replay=proof)


def diff_of(repo, older, newer):
    patch = git(repo, "diff", "--unified=3", older, newer).stdout
    return dict(commits=git(repo, "log", "--format=%h %s", f"{older}..{newer}").stdout.strip().split("\n"),
                stat=git(repo, "diff", "--stat", older, newer).stdout.strip(),
                patch=patch[:DIFF_CHARS], patch_truncated=len(patch) > DIFF_CHARS)


def event(lesson, bad):
    return dict(kind="review", repo=bad.slug, command="codex review finding fixed", error=c.encoded(lesson).strip(),
                agent="codex-review", thread=f"https://github.com/{bad.slug}/pull/{bad.pr}", occurred=bad.occurred,
                source_ref=f"settle-codex:{bad.slug}:{bad.pr}:{bad.head[:12]}:{lesson['fixed_head'][:12]}")


def titles(row):
    lesson = json.loads(row["error"])
    return verified_titles(row) | unverified_titles(row)


def verified_titles(row):
    return {(f["path"], f["title"]) for f in json.loads(row["error"])["findings"]}


def unverified_titles(row):
    return {(f["path"], f["title"]) for f in json.loads(row["error"]).get("unverified_findings", [])}


def finding_attribution(verified, unverified, bad_head=None):
    return sorted(
        [dict(path=f["path"], title=f["title"], status="verified", reviewed_head=f.get("reviewed_head", bad_head))
         for f in verified]
        + [dict(path=f["path"], title=f["title"], status="unverified", reviewed_head=f.get("reviewed_head"))
           for f in unverified],
        key=lambda item: (item["path"], item["title"], item["status"], item["reviewed_head"] or ""),
    )


def attribution(row):
    lesson = json.loads(row["error"])
    return finding_attribution(lesson["findings"], lesson.get("unverified_findings", []), lesson.get("bad_head"))


def shared_attribution_changed(previous, current):
    before = {(row["path"], row["title"]): (row["status"], row["reviewed_head"]) for row in previous}
    after = {(row["path"], row["title"]): (row["status"], row["reviewed_head"]) for row in current}
    return any(before[key] != after[key] for key in before.keys() & after.keys())


def merged(candidates, unresolved):
    first = {}
    for settle in candidates:
        key = (settle.slug, settle.pr, settle.head) if not unresolved[settle.source] else settle.source
        keep = first.setdefault(key, settle)
        keep.findings += [f for f in settle.findings if (f["path"], f["title"]) not in {(k["path"], k["title"]) for k in keep.findings}]
    return list(first.values())


def run(root, projects, settle_dir, scratch, api=gh_api, dry_run=False):
    settles = [s for s in map(parse, sorted(Path(settle_dir).glob("settle-*.out"))) if s]
    skipped, ready, pairs = [], {}, []
    unresolved = {settle.source: resolve(settle, projects, api) for settle in settles}
    candidates = merged(sorted((s for s in settles if s.findings), key=lambda s: (s.occurred, s.source)), unresolved)
    stored = {row.get("source_ref"): row for row in c.load_store(root)[1]["failures"].values()}
    for bad in candidates:
        skip = lambda reason: skipped.append(dict(source=bad.source, reason=reason, bad_head=bad.head))
        if unresolved[bad.source]:
            skip(unresolved[bad.source])
            continue
        later = [s for s in settles if not unresolved[s.source] and (s.slug, s.pr) == (bad.slug, bad.pr)
                 and s.occurred > bad.occurred and ancestor(bad.repo, bad.head, s.head)]
        if not later:
            skip("no-later-settle")
            continue
        passing = [s for s in later if s.passed]
        if not passing:
            skip("later-not-passed")
            continue
        pairs.append(bad)
        findings = finding_text(bad, api)
        if findings is None:
            skip("finding-text-missing")
            continue
        order = sorted(passing, key=lambda s: (distance(bad.repo, bad.head, s.head), s.occurred, s.source))
        proven, reason = prove(bad.repo, bad, order, findings, scratch)
        if not proven:
            corrected = finding_attribution([], findings)
            existing = None
            for candidate in order:
                ref = f"settle-codex:{bad.slug}:{bad.pr}:{bad.head[:12]}:{candidate.head[:12]}"
                row = stored.get(ref)
                if row:
                    verified = [f for f in findings if f["reviewed_head"] == bad.head
                                and (f["path"], f["title"]) in verified_titles(row)]
                    corrected = finding_attribution(verified, [f for f in findings if f not in verified])
                if row and shared_attribution_changed(attribution(row), corrected):
                    existing = row
                    break
            if existing:
                skipped.append(dict(source=bad.source, reason="attribution-changed-in-stored-lesson", bad_head=bad.head,
                                    source_ref=existing["source_ref"], previous_attribution=attribution(existing),
                                    corrected_attribution=corrected))
            else:
                skipped.append(dict(source=bad.source, reason=reason, bad_head=bad.head,
                                    unverified_findings=[dict(path=f["path"], title=f["title"],
                                                              reviewed_head=f["reviewed_head"], url=f["url"])
                                                         for f in findings]))
            continue
        for fixed, proof, bound, unverified in proven:
            item = event(evidence(bad, fixed, bound, unverified, proof, diff_of(bad.repo, bad.head, fixed.head)), bad)
            ready.setdefault(item["source_ref"], item)
    ingested = dict(imported=0, skipped=0)
    new = [item for ref, item in ready.items() if ref not in stored]
    for ref, item in ready.items():
        if ref not in stored:
            continue
        old = stored[ref]
        lesson = json.loads(item["error"])
        newly_proven = verified_titles(item) & unverified_titles(old)
        if newly_proven:
            skipped.append(dict(source=lesson["failing_settle"]["log"], reason="newly-proven-findings-not-updated",
                                bad_head=lesson["bad_head"],
                                findings=[dict(path=path, title=title) for path, title in sorted(newly_proven)]))
        previous = [f for f in attribution(old) if (f["path"], f["title"]) not in newly_proven]
        if shared_attribution_changed(previous, attribution(item)):
            skipped.append(dict(source=lesson["failing_settle"]["log"], reason="attribution-changed-in-stored-lesson",
                                bad_head=lesson["bad_head"], source_ref=ref,
                                previous_attribution=attribution(old), corrected_attribution=attribution(item)))
        if titles(item) - titles(old):
            skipped.append(dict(source=lesson["failing_settle"]["log"], reason="findings-not-in-stored-lesson",
                                bad_head=lesson["bad_head"]))
    if new and not dry_run:
        ingested = import_records(c.ordinary_path(root), [record(item) for item in new])
    return dict(settles=len(settles), candidates=len(candidates), pairs=len(pairs), proven=len(ready),
                ingested=ingested["imported"], already_present=len(ready) - len(new) + ingested["skipped"],
                skipped=skipped, sources=list(ready))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", default=".coldfix")
    parser.add_argument("--projects", required=True)
    parser.add_argument("--settle-dir", default="/tmp")
    parser.add_argument("--scratch", default=".worktrees/.replay")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    print(json.dumps(run(args.store, args.projects, args.settle_dir, args.scratch, dry_run=args.dry_run), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
