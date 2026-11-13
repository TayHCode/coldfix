#!/usr/bin/env python3
"""Read CI history and prove narrow local checks. No dependencies beyond gh and Git."""

import argparse
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import itertools
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

import coldfix as c
import replay_isolation
from ci_vm_executor import CleanupFailed, replay_tree as vm_replay_tree
from failure_ingest import SOURCE_ERRORS, instant, integer, repo_name
from github_sources import FAILED, SHA, private_store

HISTORY_START = datetime(2018, 1, 1, tzinfo=timezone.utc)
PAGE_LIMIT = 10  # GitHub's workflow search exposes at most 1,000 runs.
API_TIMEOUT = 60
EXPECTED = "ci-local-diagnostic"


class Skipped(c.ColdfixError):
    pass


def need(value):
    c.require(value, "invalid or incomplete CI metadata")


def run(command, timeout=API_TIMEOUT, env=None):
    try:
        with environment(dict(os.environ) if env is None else env):
            result = c.process(command, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired, OverflowError):
        raise Skipped("tool unavailable or operation timed out") from None
    if result.returncode:
        raise Skipped("read operation unavailable")
    return result.stdout


class BudgetPause(RuntimeError):
    pass


def abandoned(row, snapshot):
    return (snapshot is not None and row.get('status') == 'queued' and row.get('latest_check_runs_count') == 0
            and all(isinstance(row.get(key), str) and c.time_key(instant(row[key])) + timedelta(days=1) <= c.time_key(snapshot)
                    for key in ('created_at', 'updated_at')))


def unsettled(value, snapshot=None):
    rows = [value] if isinstance(value, dict) and 'status' in value else []
    if isinstance(value, dict):
        rows += [row for key in ('workflow_runs', 'jobs', 'check_suites', 'check_runs') for row in value.get(key, ())]
    return any(isinstance(row, dict) and row.get('status', 'completed') != 'completed' and not abandoned(row, snapshot)
               for row in rows)


TRANSIENT = {"TIMEOUT_OR_TOOL_UNAVAILABLE", "SOURCE_OR_CLONE_UNAVAILABLE", "DEPENDENCY_OR_INFRASTRUCTURE_UNAVAILABLE"}


def frozen(endpoint, value, snapshot=None):
    unbounded = (endpoint.startswith('actions/runs?') and '&created=' not in endpoint
                 or endpoint.startswith('commits?') and '&until=' not in endpoint)
    return not unbounded and not unsettled(value, snapshot)


def after_snapshot(github, timestamp):
    return github.snapshot is not None and (not timestamp or c.time_key(instant(timestamp)) > c.time_key(github.snapshot))


class HistorySession:
    def __init__(self, cache, remaining, snapshot=None):
        self.cache = cache
        self.remaining = remaining
        self.snapshot = snapshot
        self.requests = 0

    def read(self, repo, endpoint):
        path = self.cache / (c.digest(c.encoded([repo, endpoint])) + '.json')
        if path.exists():
            return json.loads(path.read_text(), object_pairs_hook=c.unique_object)
        if self.remaining <= 1000:
            raise BudgetPause
        self.requests += 1
        self.remaining -= 1
        raw = run(['gh', 'api', '--include', '--hostname', 'github.com', '--method', 'GET',
                   f'repos/{repo}/{endpoint}'])
        headers, separator, body = raw.partition(b'\r\n\r\n')
        if not separator:
            headers, separator, body = raw.partition(b'\n\n')
        remaining = re.search(rb'^x-ratelimit-remaining:\s*(\d+)', headers, re.I | re.M)
        need(separator and remaining)
        self.remaining = int(remaining[1])
        value = json.loads(body, object_pairs_hook=c.unique_object)
        if frozen(endpoint, value, self.snapshot):
            c.atomic_create(path, c.encoded(value))
        return value


class GitHub:
    def __init__(self, repo, full_history=False, session=None, snapshot=None):
        self.full_history = full_history
        self.snapshot = snapshot
        self.session = session
        self.repo = repo_name(repo)
        self.requests = 0
        self.incomplete = []
        self.held = []
        self.abandoned = 0
        self.cache = {}

    def api(self, endpoint):
        if endpoint not in self.cache:
            if self.session is not None:
                before = self.session.requests
                try:
                    self.cache[endpoint] = self.session.read(self.repo, endpoint)
                finally:
                    self.requests += self.session.requests - before
                return self.cache[endpoint]
            self.requests += 1
            raw = run(["gh", "api", "--hostname", "github.com", "--method", "GET",
                       f"repos/{self.repo}/{endpoint}"])
            try:
                self.cache[endpoint] = json.loads(raw, object_pairs_hook=c.unique_object)
            except (ValueError, RecursionError):
                raise Skipped("invalid GitHub JSON") from None
        return self.cache[endpoint]

    def pages(self, endpoint, key=None, query="", bounded=False, limit=None):
        rows, seen, total = [], {}, None
        page = 1
        size = min(100, limit) if limit else 100
        while True:
            value = self.api(f"{endpoint}?per_page={size}&page={page}{query}")
            batch = value[key] if key else value
            if key:
                count = value["total_count"]
                need(type(count) is int and count >= 0 and (total is None or count == total))
                total = count
            need(isinstance(batch, list) and len(batch) <= size)
            fresh = 0
            for row in batch:
                if limit and len(rows) == limit:
                    break
                need(isinstance(row, dict))
                identity = row.get("id", row.get("sha"))
                need(isinstance(identity, (str, int)))
                if identity in seen:
                    need(seen[identity] == row)
                    continue
                seen[identity] = row
                rows.append(row)
                fresh += 1
            need(fresh or not batch)
            need(total is None or len(rows) <= total)
            if limit and len(rows) >= limit and total != len(rows) and len(batch) == size:
                self.incomplete.append(endpoint + " reached selected item bound")
                return rows
            if len(batch) < size or total == len(rows):
                need(total is None or total == len(rows))
                return rows
            if bounded and not self.full_history and page == PAGE_LIMIT:
                self.incomplete.append(endpoint + " reached 1000-item history bound")
                return rows
            page += 1

    def successor(self, before, after):
        if before == after:
            return False
        need(SHA.fullmatch(before) and SHA.fullmatch(after))
        value = self.api(f"compare/{before}...{after}")
        if value["base_commit"]["sha"] != before:
            return False
        if value["status"] == "ahead":
            return value["merge_base_commit"]["sha"] == before
        commits = value.get("commits") or []
        if (value["status"] != "diverged" or not SHA.fullmatch(value["merge_base_commit"]["sha"])
                or len(commits) != value.get("ahead_by") or commits[-1].get("sha") != after):
            return False
        committed = lambda row: c.time_key(instant(row["commit"]["committer"]["date"]))
        return committed(commits[-1]) > committed(value["base_commit"])


def discover(owner):
    need(isinstance(owner, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", owner))
    # repo list is the explicitly requested discovery exception to REST GET-only.
    limit = 100
    while True:
        value = json.loads(run(["gh", "repo", "list", owner, "--visibility", "private",
                               "--limit", str(limit), "--json", "nameWithOwner,isPrivate"]))
        need(isinstance(value, list) and len(value) <= limit)
        repos = [repo_name(row["nameWithOwner"]) for row in value if row["isPrivate"] is True]
        need(len(repos) == len(value) and len(set(repos)) == len(repos))
        if len(value) < limit:
            return repos
        limit *= 2


@dataclass
class Failure:
    source: str
    run: dict
    job: dict
    green: tuple | None = None
    reason: str | None = None


def context(row, repo):
    repository = row.get("head_repository") or row.get("repository")
    name = repository.get("full_name") if isinstance(repository, dict) else None
    branch = row.get("head_branch")
    prs = row.get("pull_requests", [])
    need(isinstance(prs, list))
    numbers = frozenset(integer(pr["number"]) for pr in prs)
    return name.casefold() if isinstance(name, str) else None, branch, numbers


def same_context(red, green, repo):
    rr, rb, rp = context(red, repo)
    gr, gb, gp = context(green, repo)
    # PR heads must remain in the same repository. A recycled branch is insufficient.
    return bool(rr) and rr == gr and (bool(rp & gp) if rp or gp else bool(rb) and rb == gb)


def later(red, green):
    for row in (red, green):
        need(SHA.fullmatch(row["head_sha"]))
        instant(row["created_at"])
    return red["head_sha"] != green["head_sha"] and c.time_key(green["created_at"]) > c.time_key(red["created_at"])


def history_runs(github):
    windows, runs, seen = [(HISTORY_START, c.time_key(instant(github.snapshot)))], [], set()
    while windows:
        start, end = windows.pop()
        query = "&created=" + "..".join(moment.strftime("%Y-%m-%dT%H:%M:%SZ") for moment in (start, end))
        if github.api(f"actions/runs?per_page=100&page=1{query}")["total_count"] > PAGE_LIMIT * 100:
            middle = start + (end - start) / 2
            need(end - start > timedelta(seconds=1))
            windows += [(start, middle.replace(microsecond=0)), (middle.replace(microsecond=0) + timedelta(seconds=1), end)]
            continue
        for row in github.pages("actions/runs", "workflow_runs", query=query, bounded=True):
            if row["id"] not in seen:
                seen.add(row["id"])
                runs.append(row)
    return runs


def collect(github, maximum):
    selection = maximum
    maximum = math.inf if maximum is None else maximum
    if github.full_history and github.snapshot:
        runs = history_runs(github)
    else:
        runs = github.pages("actions/runs", "workflow_runs", query="" if github.full_history else "&status=completed", bounded=True)
    if github.full_history and unsettled(dict(workflow_runs=runs)):
        github.incomplete.append("workflow runs not completed; rescan after they finish")
    attempts = []
    for row in runs:
        for number in range(1, integer(row["run_attempt"])):
            try:
                previous = github.api(f"actions/runs/{integer(row['id'])}/attempts/{number}")
                need(previous["id"] == row["id"] and previous["head_sha"] == row["head_sha"] and previous["run_attempt"] == number
                     and previous["workflow_id"] == row["workflow_id"] and previous["path"] == row["path"])
                attempts.append(previous)
            except SOURCE_ERRORS:
                github.incomplete.append("prior workflow attempt unavailable; current runs retained")
    runs += attempts
    runs.sort(key=lambda r: c.time_key(instant(r["created_at"])), reverse=True)
    failures, actions_checks = [], set()

    def jobs(row):
        value = github.pages(f"actions/runs/{integer(row['id'])}/attempts/{integer(row['run_attempt'])}/jobs", "jobs")
        for job in value:
            url = job.get("check_run_url", "")
            if url:
                actions_checks.add(url.rsplit("/", 1)[-1])
        return value

    for red in runs:
        if red["conclusion"] not in FAILED:
            continue
        try:
            red_jobs = [job for job in jobs(red) if job["conclusion"] in FAILED]
        except SOURCE_ERRORS:
            github.incomplete.append("workflow job enumeration unavailable; earlier results retained")
            failures.append(Failure("workflow", red, {}, reason="workflow job enumeration unavailable"))
            if len(failures) >= maximum:
                return failures[:maximum]
            continue
        if not red_jobs and red["conclusion"] == "action_required":
            github.held.append(red)
            continue
        if not red_jobs:
            failures.append(Failure("workflow", red, {}, reason="workflow has no executed failed job; runner or startup unavailable"))
        for job in red_jobs:
            found = Failure("workflow", red, job)
            if not any(step.get("conclusion") in FAILED for step in job.get("steps", [])):
                found.reason = "workflow has no executed failed job; runner or startup unavailable"
                failures.append(found)
                if len(failures) >= maximum:
                    github.incomplete.append("max-failures selection cap reached; failures_found is a lower bound")
                    return failures[:maximum]
                continue
            pairing_unavailable = False
            try:
                classifications = (red.get('classification'), job.get('classification'))
                flaky = any(isinstance(value, dict) and value.get('category') == 'flake' for value in classifications)
                for observed in runs:
                    if (observed['id'], observed['run_attempt']) == (red['id'], red['run_attempt']):
                        continue
                    if (observed['head_sha'] == red['head_sha'] and observed['workflow_id'] == red['workflow_id']
                            and observed['path'] == red['path'] and context(observed, github.repo)[0] == github.repo
                            and any(value['name'] == job['name'] and value['conclusion'] == 'success' for value in jobs(observed))):
                        flaky = True
                        break
                if flaky:
                    found.reason = 'flaky failure has a same-commit passing job or explicit flake classification'
            except SOURCE_ERRORS:
                found.reason = 'flake detection source unavailable'
                github.incomplete.append(found.reason)
            for green in reversed(runs):
                if found.reason:
                    break
                try:
                    if (green["workflow_id"] == red["workflow_id"]
                            and green["path"] == red["path"] and green["event"] == red["event"]
                            and same_context(red, green, github.repo) and later(red, green)
                            and github.successor(red["head_sha"], green["head_sha"])):
                        matches = [j for j in jobs(green) if j["name"] == job["name"]]
                        if len(matches) == 1 and matches[0]["conclusion"] == "success":
                            found.green = (green, matches[0])
                            break
                except SOURCE_ERRORS:
                    pairing_unavailable = True
                    github.incomplete.append("workflow pairing source unavailable; earlier results retained")
            if found.green is None and found.reason is None:
                found.reason = "workflow pairing source unavailable" if pairing_unavailable else "no later green sharing history for the same workflow, job and branch/PR"
            failures.append(found)
            if len(failures) >= maximum:
                github.incomplete.append("max-failures selection cap reached; failures_found is a lower bound")
                return failures[:maximum]
        if len(failures) >= maximum:
            github.incomplete.append("max-failures selection cap reached; failures_found is a lower bound")
            return failures[:maximum]

    try:
        # External check runs have no trusted workflow command. Enumerate and retain them held.
        checks = []
        def external_checks():
            for commit in github.pages("commits", query="&until=" + github.snapshot if github.snapshot else "", bounded=True, limit=selection):
                sha = commit["sha"]
                need(SHA.fullmatch(sha))
                for suite in github.pages(f"commits/{sha}/check-suites", "check_suites", limit=selection):
                    if after_snapshot(github, suite.get("created_at")):
                        continue
                    if abandoned(suite, github.snapshot):
                        github.abandoned += 1
                    elif unsettled(suite):
                        github.incomplete.append("external check suite not completed; rescan after it finishes")
                        continue
                    for check in github.pages(f"check-suites/{integer(suite['id'])}/check-runs", "check_runs", "&filter=all", limit=selection):
                        if str(check["id"]) in actions_checks or (check.get("app") or {}).get("slug") == "github-actions":
                            continue
                        if unsettled(check):
                            github.incomplete.append("external check not completed; rescan after it finishes")
                            continue
                        check = dict(check, created_at=check.get("started_at") or check.get("completed_at"),
                                     head_branch=suite.get("head_branch"), head_repository=suite.get("repository"))
                        if after_snapshot(github, check["created_at"]):
                            continue
                        yield check
        external_failures = set()
        for check in external_checks():
            checks.append(check)
            if check["conclusion"] in FAILED:
                external_failures.add(check["id"])
            if len(failures) + len(external_failures) >= maximum:
                github.incomplete.append("external check scan stopped at max-failures selection cap")
                break
        seen = set()
        for check in checks:
            if check["id"] in seen or check["conclusion"] not in FAILED:
                continue
            seen.add(check["id"])
            found = Failure("check", check, check, reason="external check has no supported workflow command")
            for green in checks:
                if (green["conclusion"] == "success" and green["name"] == check["name"]
                        and (green.get("app") or {}).get("id") == (check.get("app") or {}).get("id")
                        and same_context(check, green, github.repo) and later(check, green)
                        and github.successor(check["head_sha"], green["head_sha"])):
                    found.green = (green, green)
                    break
            failures.append(found)
            if len(failures) >= maximum:
                github.incomplete.append("max-failures selection cap reached; failures_found is a lower bound")
                break
    except SOURCE_ERRORS:
        github.incomplete.append("external check enumeration unavailable; workflow results retained")
    return failures


def scalar(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = json.loads(value) if value[0] == '"' else value[1:-1].replace("''", "'")
    return value


def supported(command):
    if any(char in command for char in "\n\r;&|><`$\\\x00"):
        raise Skipped("unsupported shell command")
    args = shlex.split(command)
    need(args)
    head = args[0]
    tail = args[1:]
    if head in ("python", "python3") and len(tail) >= 2 and tail[0] == "-m":
        head, tail = tail[1], tail[2:]
        args = [sys.executable, "-m", head, *tail]
    if head == "unittest" and args[0] == sys.executable:
        kind = "test"
    elif head in ("pytest", "mypy"):
        kind = "test" if head == "pytest" else "lint"
    elif head == "ruff" and tail and tail[0] == "check" and not any(a.startswith("--fix") for a in tail):
        kind = "lint"
    elif head == "tsc" and "--noEmit" in tail and not any(a in ("--build", "-b") for a in tail):
        kind = "lint"
    else:
        raise Skipped("unsupported test, lint or type-check command")
    if any(a.startswith(("--config", "--cache-dir", "--junit", "--output", "--report")) for a in tail):
        raise Skipped("unsupported command output or configuration option")
    if any(a.startswith("/") or ".." in Path(a).parts for a in tail if not a.startswith("-")):
        raise Skipped("command path escapes checkout")
    if any(a.startswith(("--install-types", "--watch", "--eval", "--require", "--import")) or a in ("-w", "-e", "-r") for a in tail):
        raise Skipped("command has unsupported execution or installation options")
    if head == "tsc" and any(a.startswith("--noEmit=") or a == "false" for a in tail):
        raise Skipped("type check must retain noEmit")
    try:
        c.public_text(command, [])
        need(not c.privacy_counts(command))
    except c.ColdfixError:
        raise Skipped("command privacy screen blocked") from None
    return args, kind


SETUP_PYTHON_INPUTS = {"python-version", "cache", "cache-dependency-path", "check-latest", "allow-prereleases"}
PIP_FLAGS = {"--upgrade", "-U", "--quiet", "-q", "--no-cache-dir", "--disable-pip-version-check"}
PIP_SPEC = re.compile(r"[A-Za-z0-9][\w.-]*(?:\[[\w,.-]+\])?(?:[<>=!~]=?[\w.*+-]+(?:,[<>=!~]=?[\w.*+-]+)*)?|\.(?:\[[\w,.-]+\])?")
PIP_FILE = re.compile(r"(?!/)[\w./-]+")
MATRIX_USE = re.compile(r"\$\{\{\s*matrix\.([\w-]+)\s*\}\}")


def hold(condition):
    if not condition:
        raise Skipped("unsupported workflow preparation or matrix")


def pip_install(command):
    if any(char in command for char in "\n\r;&|`$\\\x00"):
        raise Skipped("unsupported shell command")
    shell = shlex.shlex(command, posix=True, punctuation_chars="<>")
    shell.whitespace_split = True
    shell.commenters = ""
    args = list(shell)
    if any(re.fullmatch(r"[<>]+", arg) for arg in args):
        raise Skipped("unsupported shell redirection")
    if args[:3] in (["python", "-m", "pip"], ["python3", "-m", "pip"]):
        args = args[3:]
    elif args[:1] in (["pip"], ["pip3"]):
        args = args[1:]
    else:
        raise Skipped("unsupported preparation command")
    hold(args[:1] == ["install"])
    rest, n = [], 1
    while n < len(args):
        arg = args[n]
        if arg in ("-r", "-e"):
            n += 1
            target = args[n] if n < len(args) else ""
            hold(PIP_FILE.fullmatch(target) and ".." not in Path(target).parts and (arg == "-r" or PIP_SPEC.fullmatch(target)))
            rest += [arg, target]
        elif arg not in PIP_FLAGS and not PIP_SPEC.fullmatch(arg):
            raise Skipped("unsupported pip install option")
        else:
            rest.append(arg)
        n += 1
    hold(rest)
    try:
        c.public_text(command, [])
        hold(not c.privacy_counts(command))
    except c.ColdfixError:
        raise Skipped("command privacy screen blocked") from None
    return ["install", *rest]


def matrix_axes(block):
    start = [i for i, line in enumerate(block) if re.match(r"    strategy\s*:", line)]
    if not start:
        return {}
    hold(block[start[0]].strip() == "strategy:")
    axes, inside, key = {}, False, None
    for line in block[start[0] + 1:]:
        if not line.strip():
            continue
        if not line.startswith("     "):
            break
        if re.fullmatch(r"      matrix:\s*", line):
            inside = True
        elif re.fullmatch(r"      (?:fail-fast|max-parallel):\s*\S+", line):
            inside = False
        elif inside and (m := re.fullmatch(r"        ((?!include\b|exclude\b)[A-Za-z_][\w-]*):\s*(.*)", line)):
            key = m[1]
            items = re.fullmatch(r"\[(.*)\]", m[2])
            axes[key] = [scalar(v) for v in items[1].split(",")] if items else []
            hold(items or not m[2])
        elif inside and key and (m := re.fullmatch(r"          - (.+)", line)):
            axes[key].append(scalar(m[1]))
        else:
            raise Skipped("workflow matrix unsupported")
    hold(axes and all(values and all(re.fullmatch(r"[\w.+-]+", v) for v in values) for values in axes.values()))
    return axes


def expand_matrix(block):
    axes = matrix_axes(block)
    if not set(MATRIX_USE.findall("\n".join(block))) <= set(axes):
        raise Skipped("workflow matrix value unavailable")
    for values in itertools.product(*axes.values()):
        entry = dict(zip(axes, values))
        yield entry, [MATRIX_USE.sub(lambda m: entry[m[1]], line) for line in block]


def workflow_key(line):
    """Read only simple mapping keys; leave YAML escapes and complex keys unsupported."""
    match = re.fullmatch(r"( *(?:- )?)(?:([A-Za-z_][\w-]*)|'([A-Za-z_][\w-]*)'|\"([A-Za-z_][\w-]*)\") *:(?=\s|$)(.*)", line)
    if not match:
        raise Skipped("workflow mapping key unsupported")
    return match[1] + next(key for key in match.group(2, 3, 4) if key is not None) + ":" + match[5]


def workflow_mapping_lines(content):
    lines = []
    scalar_indent = matrix_indent = None
    for line in content.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            lines.append(line)
            continue
        indent = len(line) - len(line.lstrip())
        if scalar_indent is not None and indent > scalar_indent:
            lines.append(line)
            continue
        scalar_indent = None
        if matrix_indent is not None and indent <= matrix_indent:
            matrix_indent = None
        if matrix_indent is not None and re.fullmatch(r" *- [^:#]+", line):
            lines.append(line)
            continue
        normalized = workflow_key(line)
        lines.append(normalized)
        if normalized.strip() == "matrix:":
            matrix_indent = indent
        if re.fullmatch(r" *[|>][-+0-9]* *(?:#.*)?", normalized.split(":", 1)[1]):
            scalar_indent = indent + (2 if line.lstrip().startswith("- ") else 0)
    return lines


def workflow_jobs(content):
    # shortcut: parse a conservative Actions YAML subset; hold complex YAML until a real parser is justified.
    if "\t" in content:
        raise Skipped("workflow tabs unsupported")
    lines = workflow_mapping_lines(content)
    content = "\n".join(lines)
    if (re.search(r"\$\{\{(?!\s*matrix\.[\w-]+\s*\}\})|[&*!][A-Za-z_]|^\s*(?:env|services|container|defaults):", content, re.M)
            or re.search(r"^\s*needs:", content, re.M)):
        raise Skipped("workflow needs secrets, services, environment or unsupported YAML")
    roots = [i for i, line in enumerate(lines) if line == "jobs:"]
    if len(roots) != 1:
        raise Skipped("workflow jobs mapping unsupported")
    lines = lines[roots[0] + 1:]
    end = next((i for i, line in enumerate(lines) if line and not line[0].isspace() and not line.startswith("#")), len(lines))
    lines = lines[:end]
    starts = [i for i, line in enumerate(lines) if re.fullmatch(r"  [A-Za-z_][\w-]*:\s*", line)]
    for n, start in enumerate(starts):
        block = lines[start:starts[n + 1] if n + 1 < len(starts) else len(lines)]
        job_id = lines[start].strip()[:-1]
        explicit = next((scalar(m[1]) for line in block if (m := re.fullmatch(r"    name:\s*(.+)", line))), None)
        for number, (entry, expanded) in enumerate(expand_matrix(block)):
            name = next((scalar(m[1]) for line in expanded if (m := re.fullmatch(r"    name:\s*(.+)", line))), job_id)
            if explicit is None and entry:
                names = ([name] if number == 0 else []) + [name + " (" + ", ".join(entry.values()) + ")"]
            else:
                names = [name]
            yield names, expanded


def workflow_plan(content, job):
    choices = [block for names, block in workflow_jobs(content) if job["name"] in names]
    if len(choices) != 1:
        raise Skipped("workflow job identity unsupported or ambiguous")
    block = choices[0]
    keys = re.findall(r"^    ([\w-]+):", "\n".join(block), re.M)
    if len(keys) != len(set(keys)):
        raise Skipped("duplicate workflow job fields")
    runner = next((scalar(m[1]) for line in block if (m := re.fullmatch(r"    runs-on:\s*(.+)", line))), "")
    if sys.platform != "linux" or not re.fullmatch(r"ubuntu-(?:latest|\d{2}\.\d{2})", runner):
        raise Skipped("unsupported OS or runner")
    if re.search(r"^    (?:if|uses):", "\n".join(block), re.M):
        raise Skipped("conditional or reusable workflow job unsupported")
    runtime = sys.executable
    runtime_name = "python3"
    setup = []
    setup_commands = []
    steps = []
    for line in block:
        if re.match(r"      - ", line):
            steps.append([])
        if steps:
            steps[-1].append(line)
    names = [step["name"] for step in job.get("steps", [])]
    if len(names) != len(set(names)):
        raise Skipped("failed workflow step identity ambiguous")
    failed_names = {step["name"] for step in job.get("steps", []) if step["conclusion"] in FAILED}
    if len(failed_names) != 1:
        raise Skipped("failed workflow step identity unavailable")
    candidates, checkouts = [], 0
    for step in steps:
        body = "\n".join(step)
        keys = re.findall(r"^      (?:- |  )([\w-]+):", body, re.M)
        if len(keys) != len(set(keys)) or "run" in keys and "uses" in keys:
            raise Skipped("duplicate or conflicting workflow step fields")
        action = re.search(r"^      (?:- |  )uses:\s*(.+)$", body, re.M)
        if action:
            if not re.fullmatch(r"actions/(?:checkout|setup-python)@(?:v\d+|[a-f0-9]{40})", scalar(action[1].split()[0])):
                raise Skipped("workflow requires an unsupported action")
            if set(keys) - {"name", "id", "uses", "with"}:
                raise Skipped("workflow action settings unsupported")
            if scalar(action[1]).startswith("actions/checkout@"):
                checkouts += 1
                if "with" in keys:
                    raise Skipped("workflow checkout inputs unsupported")
            elif "with" in keys:
                inputs = re.findall(r"^          ([\w-]+):", body, re.M)
                if "python-version" not in inputs or set(inputs) - SETUP_PYTHON_INPUTS:
                    raise Skipped("workflow action inputs unsupported")
                version = scalar(re.search(r"^          python-version:\s*(.+)$", body, re.M)[1])
                if re.fullmatch(r"3(?:\.\d+)?", version):
                    runtime_name = "python" + version
                    runtime = shutil.which(runtime_name, path=os.defpath) or sys.executable
                else:
                    runtime, runtime_name = sys.executable, "python3"
        match = re.search(r"^      (?:- |  )run:\s*(.*)$", body, re.M)
        if not match:
            continue
        name_match = re.search(r"^      (?:- |  )name:\s*(.+)$", body, re.M)
        value = match[1]
        if value in ("|", "|-"):
            following = body[match.end():].splitlines()
            parts = []
            for line in following:
                if line.strip() and not line.startswith("          "):
                    break
                if line.strip():
                    parts.append(line[10:])
            value = "\n".join(parts)
        else:
            following = body[match.end():].splitlines()
            for line in following:
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                if len(line) - len(line.lstrip()) > 8:
                    raise Skipped("multiline run scalar unsupported")
                break
            value = scalar(value)
        step_name = scalar(name_match[1]) if name_match else "Run " + value
        if re.search(r"^      (?:- |  )(?:if|shell|working-directory|env):", body, re.M):
            raise Skipped("step requires conditional, shell, directory or environment support")
        if checkouts != 1:
            raise Skipped("workflow requires one default checkout before its check")
        if step_name not in failed_names:
            setup.append(pip_install(value))
            setup_commands.append(value)
            continue
        argv, kind = supported(value)
        if argv[0] in ("pytest", "mypy", "ruff"):
            argv = [runtime, "-m", *argv]
        elif argv[0] == sys.executable:
            argv[0] = runtime
        if setup and argv[0] == "tsc":
            raise Skipped("pip setup before a TypeScript check unsupported")
        candidate = dict(command=argv, source_command=value, kind=kind, runtime_name=runtime_name, setup=setup)
        if setup_commands:
            candidate['setup_commands'] = setup_commands
        candidates.append(candidate)
        break
    if checkouts != 1:
        raise Skipped("workflow requires one default checkout before its check")
    if len(candidates) != 1:
        raise Skipped("failed workflow step command unsupported or ambiguous")
    return candidates[0]


def clean_env(home):
    toolchain = {"COLDFIX_" + name.upper(): str(Path(tool).absolute()) if (tool := shutil.which(name)) else ""
                 for name in ("tsc", "node")}
    return dict(PATH=os.path.dirname(sys.executable) + os.pathsep + os.defpath, HOME=str(home), **toolchain,
                LANG="C.UTF-8", LC_ALL="C.UTF-8", PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1", CI="1",
                GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")


@contextmanager
def environment(values):
    previous = dict(os.environ)
    os.environ.clear()
    os.environ.update(values)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(previous)


# Source output stays in an unlinked file; framework outcomes, not display text, prove test success.
TEST_BRIDGE = r'''
import json, sys
path, framework, *flags = sys.argv[1:]
sys.argv = [framework, *flags]
data = dict(passed=[], failed=[], errors=[], expected=[])
if framework == "unittest":
    import unittest
    class Result(unittest.TextTestResult):
        def startTest(self, test):
            self.passing_subtests = []
            super().startTest(test)
        def addSuccess(self, test):
            data["passed"].extend(self.passing_subtests)
            data["passed"].append(getattr(test, "id")())
            super().addSuccess(test)
        def addSubTest(self, test, subtest, error):
            if error is None:
                self.passing_subtests.append(getattr(subtest, "id")())
            super().addSubTest(test, subtest, error)
    class Runner(unittest.TextTestRunner):
        resultclass = Result
    program = unittest.main(module=None, argv=sys.argv, testRunner=Runner, exit=False)
    result = program.result
    data["failed"] = [getattr(test, "id")() for test, _ in result.failures]
    data["errors"] = [getattr(test, "id")() for test, _ in result.errors]
    data["expected"] = [getattr(test, "id")() for test, _ in result.expectedFailures]
    data["expected"] += [getattr(test, "id")() for test in result.unexpectedSuccesses]
    code = 0 if result.wasSuccessful() else 1
else:
    import pytest
    class Reports:
        def __init__(self):
            self.skipped = set()
        def pytest_runtest_logreport(self, report):
            if report.outcome == "skipped":
                self.skipped.add(report.nodeid)
            if report.when not in ("setup", "call", "teardown") or report.outcome not in ("passed", "failed", "skipped"):
                data["errors"].append(report.nodeid)
            elif hasattr(report, "wasxfail"):
                data["expected"].append(report.nodeid)
            elif report.when == "call" and report.passed:
                data["passed"].append(report.nodeid)
            elif report.failed:
                data["failed" if report.when == "call" else "errors"].append(report.nodeid)
    reports = Reports()
    code = int(pytest.main(flags, plugins=[reports]))
    data["passed"] = [node for node in data["passed"] if node not in reports.skipped]
with open(path, "w", encoding="utf-8") as output:
    print(json.dumps(data), file=output)
sys.exit(code)
'''

CHECK = r'''
import hashlib, json, subprocess, sys
from shutil import which
from os import environ
from sys import exit as stop, stderr
from tempfile import TemporaryFile, TemporaryDirectory
from os.path import abspath
from re import search, I

def finish(status, reason, diagnostic="", tests=None):
    print(json.dumps({"status": status, **({"failure": expected} if status == "FAIL" else {})}))
    print(json.dumps({"reason": reason, "diagnostic": diagnostic, "tests": tests or []}), file=stderr)
    stop({"PASS": 0, "FAIL": 1, "INCOMPLETE": 2}[status])

results = None
try:
    with (open(environ["COLDFIX_GUEST_LOG"], "w+b") if environ.get("COLDFIX_GUEST_LOG") else TemporaryFile()) as output, TemporaryDirectory(prefix=".ci-home-", dir=".") as home, TemporaryDirectory(prefix="coldfix-venv-", dir=environ.get("HOME")) as venv:
        command = list(command)
        if command[0] in ("python3", "python%d.%d" % sys.version_info[:2]):
            command[0] = sys.executable
        elif command[0].startswith("python"):
            command[0] = which(command[0], path=environment["PATH"]) or command[0]
        argv = command
        if command[0] == "tsc":
            tsc = environ.get("COLDFIX_TSC", which("tsc"))
            node = environ.get("COLDFIX_NODE", which("node"))
            if not tsc or not node:
                finish("INCOMPLETE", "local tool unavailable")
            argv = [abspath(node), abspath(tsc), *command[1:]]
        report = None
        if kind == "test":
            if framework not in ("unittest", "pytest"):
                finish("INCOMPLETE", "structured test results unavailable")
            report = abspath(home) + "/result"
            argv = [command[0], "-c", bridge, report, framework, *command[3:]]
        env = dict(environment, HOME=abspath(home))
        if setup and command[1:2] == ["-m"]:
            python = abspath(venv) + "/bin/python"
            env["PATH"] = abspath(venv) + "/bin:" + env["PATH"]
            for step in [[command[0], "-m", "venv", abspath(venv)], *[[python, "-m", "pip", *args] for args in setup]]:
                if getattr(subprocess.Popen(step, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env), "wait")():
                    finish("INCOMPLETE", "dependency, service, secret or infrastructure unavailable")
            argv[0] = python
        child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=output, stderr=output, env=env)
        code = getattr(child, "wait")()
        length = getattr(output, "tell")()
        getattr(output, "seek")(max(0, length - 16000))
        text = getattr(output, "read")().decode("utf-8", "replace")
        if report:
            try:
                with open(report, encoding="utf-8") as retained:
                    results = json.load(retained)
            except (OSError, ValueError):
                pass
except OSError:
    finish("INCOMPLETE", "local tool unavailable")
if search(r"ModuleNotFoundError|ImportError|command not found|No module named|Cannot find module|Cannot find type definition|Cannot find implementation|Library stubs not installed|Could not find a declaration file|connection refused|could not resolve|network is unreachable|No such file or directory|PermissionError|ENOTFOUND|ECONNREFUSED|No space left", text, I):
    finish("INCOMPLETE", "dependency, service, secret or infrastructure unavailable")
if search(r"errors=[1-9]|(?m:^ERROR )|KeyError", text):
    finish("INCOMPLETE", "dependency, service, secret or infrastructure unavailable")
passed, failed = set(), set()
if kind == "test":
    if (not isinstance(results, dict) or set(results) != {"passed", "failed", "errors", "expected"}
            or any(not isinstance(rows, list) or any(not isinstance(value, str) for value in rows) for rows in results.values())):
        finish("INCOMPLETE", "structured test results unavailable")
    if results["expected"]:
        finish("INCOMPLETE", "expected failures are not fix proof")
    if results["errors"]:
        finish("INCOMPLETE", "test runner errors")
    passed = {hashlib.sha256(value.encode()).hexdigest() for value in results["passed"]}
    failed = {hashlib.sha256(value.encode()).hexdigest() for value in results["failed"]}
if code == 0:
    if kind == "test" and (failed or not passed):
        finish("INCOMPLETE", "no passing tests executed")
    if known_tests and not set(known_tests) <= passed:
        finish("INCOMPLETE", "failed test did not pass after fix")
    finish("PASS", "local command passed", tests=known_tests)
if code != 1 and not (command[0] == "tsc" and code == 2):
    finish("INCOMPLETE", "unexpected local exit")
if kind == "test" and (not failed or known_tests and set(known_tests) != failed):
    finish("INCOMPLETE", "failed test identity changed")
patterns = [r"(?m)^\s*(?:E\s+)?AssertionError[^\n]*", r"(?m)^.*error TS\d+:[^\n]*", r"(?m)^.*[:]\d+(?:[:]\d+)?[:] (?:error:|[A-Z]\d{3}\b)[^\n]*", r"(?m)^\s*(?:[EFW]\d{3}|ERR_ASSERTION)[^\n]*"]
for pattern in patterns:
    match = search(pattern, text)
    if match:
        diagnostic = hashlib.sha256(" ".join(match[0].split()).encode()).hexdigest()
        if known and diagnostic != known:
            finish("INCOMPLETE", "diagnostic differs from retained failure")
        finish("FAIL", "known local diagnostic", diagnostic, sorted(failed))
finish("INCOMPLETE", "unrecognized failure diagnostic")
'''


def check_script(plan, known=None, known_tests=None):
    command = list(plan["command"])
    if "runtime_name" in plan and len(command) > 1 and command[1] == "-m":
        command[0] = plan["runtime_name"]
    framework = command[2] if len(command) > 2 and command[1] == "-m" else ""
    values = dict(command=command, kind=plan["kind"], expected=EXPECTED, environment=clean_env("."),
                  known=known, known_tests=known_tests or [], framework=framework, bridge=TEST_BRIDGE,
                  setup=plan.get("setup", []))
    values["environment"]["PATH"] = os.defpath
    if replay_isolation.cached_tools() is not None:
        values["environment"]["PATH"] = replay_isolation.TOOLS + "/bin" + os.pathsep + os.defpath
    for name in ("COLDFIX_TSC", "COLDFIX_NODE"):
        values["environment"].pop(name)
    return "\n".join(key + " = " + repr(value) for key, value in values.items()) + "\n" + CHECK


def replay_pair(clone, before, after, plan, temporary, timeout, executor=None):
    script = check_script(plan)
    check = temporary / "check.py"
    c.atomic_create(check, script)
    command = "python3 -I -S {check}"
    replay_command = command
    if plan["command"][1:2] == ["-m"]:
        replay_command = shlex.quote(plan["command"][0]) + " -I -S {check}"
    rows, receipts = {}, {}
    shell = replay_command.replace("{check}", shlex.quote(str(check)))

    def run(side, revision, reverse_patch=None):
        if executor is not None:
            return executor(clone, revision, temporary / side, shell, EXPECTED, timeout)
        paths = []
        row = vm_replay_tree(clone, revision, temporary / side, shell, EXPECTED, timeout,
                             reverse_patch=reverse_patch, receipts=paths)
        receipts[side] = paths
        return row

    probe = run("probe", before)
    if not c.check_unchanged(check, script):
        raise Skipped("retained check changed during replay")
    if probe["status"] == "FAIL":
        script = check_script(plan, diagnostic(probe)["diagnostic"], diagnostic(probe)["tests"])
        c.atomic_create(check, script, replace=True)
    else:
        rows["before"] = probe
        if "probe" in receipts:
            receipts["before"] = receipts["probe"]
    for side, revision in (("before", before), ("after", after)):
        if side not in rows:
            rows[side] = run(side, revision)
        if not c.check_unchanged(check, script):
            raise Skipped("retained check changed during replay")
    if executor is not None:
        status, verification = c.replay_state(rows["before"], rows["after"])
        return dict(status=status, verification=verification, check_script=script,
                    check_command=command, check_sha256=c.digest(script), expected_failure=EXPECTED,
                    observed_at=c.new_record()["created"], **rows)
    if rows["before"]["status"] == "FAIL" and rows["after"]["status"] == "PASS":
        patch = temporary / "fix.patch"
        try:
            patch.write_bytes(c.repo_patch(clone, before, after, [], timeout))
        except (c.ColdfixError, OSError, subprocess.TimeoutExpired) as error:
            raise Skipped("fix patch unavailable for reverse replay") from error
        rows["reversed"] = run("reversed", after, patch)
        if not c.check_unchanged(check, script):
            raise Skipped("retained check changed during replay")
    else:
        rows["reversed"] = dict(commit="", tree="", status="INCOMPLETE", exit=None, stdout="", stderr="")
    status, verification = c.replay_state(rows["before"], rows["after"], rows["reversed"])
    return dict(status=status, verification=verification, receipts=receipts, check_script=script,
                check_command=command, check_sha256=c.digest(script), expected_failure=EXPECTED,
                observed_at=c.new_record()["created"], **rows)


def diagnostic(row):
    try:
        value = json.loads(row["stderr"])
        need(set(value) == {"reason", "diagnostic", "tests"})
        need(isinstance(value["tests"], list) and all(isinstance(item, str) and re.fullmatch(r"[0-9a-f]{64}", item) for item in value["tests"]))
        need(value["diagnostic"] == "" or re.fullmatch(r"[0-9a-f]{64}", value["diagnostic"]))
        need(value["reason"] in {"local tool unavailable", "dependency, service, secret or infrastructure unavailable",
             "local command passed", "structured test results unavailable", "expected failures are not fix proof", "test runner errors", "no passing tests executed", "failed test did not pass after fix", "failed test identity changed", "diagnostic differs from retained failure", "no tests executed", "unexpected local exit", "known local diagnostic", "unrecognized failure diagnostic"})
        return value
    except (ValueError, TypeError, c.ColdfixError):
        return dict(reason="replay timed out or checkout unavailable", diagnostic="", tests=[])


def promote(root, candidate, clone, temporary, timeout):
    proof = candidate["replay"]
    # These are two observations of one incident, not two historical CI failures.
    event = dict(kind="ci", repo=candidate["repo"], agent="unknown", thread="",
                 source_ref=candidate["links"][0], occurred=candidate["occurred"],
                 command=c.encoded([candidate["identity"], candidate["plan"]["command"]]).strip(),
                 error="failed check with reproduced local diagnostic " + diagnostic(proof["before"])["diagnostic"])
    with c.lesson_lock(root):
        _, store = c.load_store(root)
        identity = c.digest(c.encoded([candidate["repo"], candidate["identity"], proof["before"]["commit"]]))[:32]
        row = c.event_record(event, "ci-fix-learning")
        row["id"] = identity
        existing = store["failures"].get(identity)
        if existing and existing["group_id"] != row["group_id"]:
            raise Skipped("retained commit observation differs; existing evidence preserved")
        if not existing:
            c.save(root, "failures", row)
        local_id = c.digest(c.encoded(["ci-fix-local-reproduction", identity, proof["check_sha256"]]))[:32]
        if local_id not in store["failures"]:
            local = c.event_record(dict(event, kind="test", agent="local-replay",
                source_ref="coldfix:local-replay:" + c.digest(c.encoded([identity, proof["before"]["commit"], proof["check_sha256"]])),
                occurred=proof["observed_at"]), "ci-fix-learning-local-reproduction")
            local["id"] = local_id
            c.save(root, "failures", local)
        _, store = c.load_store(root)
        previous = [r for r in c.latest_lessons(store).values() if r["group_id"] == row["group_id"]]
        if (previous and previous[-1]["verification"] == "proven"
                and previous[-1]["status"] in c.LIVE
                and c.read_invalidations(root).get(previous[-1]["lesson_id"]) != previous[-1]["id"]
                and previous[-1]["check_script"] == proof["check_script"]):
            return dict(status="existing", lesson=previous[-1]["id"], verification="proven")
        check = temporary / "lesson-check.py"
        c.atomic_create(check, proof["check_script"])
        authored = c.lesson(argparse.Namespace(group=None if previous else row["group_id"], grouping="exact", from_proposal=None,
            revise=previous[-1]["id"] if previous else None, kind=candidate["plan"]["kind"], level=candidate["plan"]["kind"], status="proposed",
            title="Prevent CI check regression", failure=candidate["what_went_wrong"], fix=candidate["fix"],
            rule=candidate["prevention"], rule_file=None, check_command=proof["check_command"],
            check_file=str(check), expected_failure=EXPECTED), root, store)
        _, store = c.load_store(root)
        result = c.replay(argparse.Namespace(lesson=authored["id"], repo=str(clone), before=proof["before"]["commit"],
            after=proof["after"]["commit"], status="active", timeout=timeout,
            replay_executor=vm_replay_tree), root, store)
        _, store = c.load_store(root)
        need(store["lessons"][result["id"]]["verification"] == result["verification"])
        return result


def source_link(repo, failure, green=False):
    row, job = failure.green if green else (failure.run, failure.job)
    if failure.source == "workflow":
        link = f"https://github.com/{repo}/actions/runs/{integer(row['id'])}/attempts/{integer(row['run_attempt'])}"
        return link + (f"#job-{integer(job['id'])}" if job else "")
    return f"https://api.github.com/repos/{repo}/check-runs/{integer(row['id'])}"


def attempt(github, failure, root, output, timeout, clone_url=None):
    result = dict(repo=github.repo, source=failure.source, links=[source_link(github.repo, failure)],
                  status="SKIPPED", reason=failure.reason, red_reproduced=False, proven=False, lesson=None)
    if failure.green:
        result["links"].append(source_link(github.repo, failure, True))
    if failure.reason:
        return result
    before, after = failure.run["head_sha"], failure.green[0]["head_sha"]
    need(SHA.fullmatch(before) and SHA.fullmatch(after))
    if context(failure.run, github.repo)[0] != github.repo:
        result["reason"] = "fork checkout unsupported"
        return result
    work = c.ordinary_path(root / "ci-work")
    work.mkdir(mode=0o700, exist_ok=True)
    need(work.stat().st_mode & 0o077 == 0)
    with tempfile.TemporaryDirectory(prefix="clone-", dir=work) as name:
        temporary = Path(name)
        clone = temporary / ("checkout-" + uuid.uuid4().hex)
        try:
            git_env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
            prefix = ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "credential.helper=",
                      "-c", "credential.helper=!gh auth git-credential"]
            url = clone_url or f"https://github.com/{github.repo}.git"
            run([*prefix, "clone", "--quiet", "--depth=1", "--no-checkout", "--no-tags", "--", url, str(clone)], timeout, git_env)
            run([*prefix, "-C", str(clone), "fetch", "--quiet", "--depth=1", "--no-tags", "origin", before, after], timeout, git_env)
            path = failure.run["path"]
            try:
                c.public_text(failure.job["name"], [])
                need(not c.privacy_counts(failure.job["name"]))
            except c.ColdfixError:
                raise Skipped("job metadata privacy screen blocked") from None
            if not re.fullmatch(r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml", path):
                raise Skipped("unsupported workflow source path")
            content = run(["git", "-C", str(clone), "show", before + ":" + path], timeout, clean_env(temporary)).decode("utf-8")
            plan = workflow_plan(content, failure.job)
            if failure.run["event"] != "push":
                raise Skipped("workflow checkout event unsupported; PR merge source is not reproduced")
            proof = replay_pair(clone, before, after, plan, temporary, timeout)
            result.update(red_reproduced=proof["before"]["status"] == "FAIL", proven=proof["verification"] == "proven")
            result["reason"] = ("red-before, green-after, and reversed-fix failure proven" if result["proven"] else
                diagnostic(proof["reversed"])["reason"] if proof["before"]["status"] == "FAIL" and proof["after"]["status"] == "PASS" else
                diagnostic(proof["before"] if proof["before"]["status"] != "FAIL" else proof["after"])["reason"])
            result["status"] = "PROVEN" if result["proven"] else "SKIPPED"
            # Retain only protocol JSON and screened fixed reasons, never arbitrary subprocess output.
            for side in ("before", "after", "reversed"):
                info = diagnostic(proof[side])
                proof[side]["stderr"] = c.encoded(info).strip()
                if proof[side]["status"] == "INCOMPLETE":
                    proof[side]["stdout"] = '{"status":"INCOMPLETE"}\n'
            candidate = dict(result, plan=plan, replay=proof, occurred=instant(failure.job.get("completed_at") or failure.run.get("updated_at") or failure.run["created_at"]),
                occurred_basis="job_completed_at" if failure.job.get("completed_at") else "run_updated_at" if failure.run.get("updated_at") else "run_created_at",
                identity=[failure.run["workflow_id"], path, failure.job["name"]],
                what_went_wrong="The retained workflow check reports a known diagnostic on the failed CI commit.",
                fix=f"Later commit {after} on the same branch/PR passes the identical retained local check. The replay proves behavior, not a unique causal edit.",
                prevention="Run the retained regression check before accepting changes to this repository.")
            if result["proven"]:
                promotion = promote(root, candidate, clone, temporary, timeout)
                if promotion["verification"] != "proven":
                    raise Skipped("final lesson replay did not prove red-before and green-after")
                result["lesson"] = promotion
                candidate["lesson"] = promotion
            filename = "candidate-" + uuid.uuid4().hex + ".json"
            c.atomic_create(output / filename, c.encoded(candidate))
            result["artifact"] = filename
        except Skipped as error:
            result.update(status="SKIPPED", proven=False, lesson=None, reason=str(error))
    return result


def report_markdown(report):
    counts = report["counts"]
    lines = ["# CI fix learning", "", f"Failures found {counts['failures_found']}; red {counts['red_reproduced']}; proven {counts['proven']}; promoted {counts['lessons_promoted']}; skipped {counts['skipped']}.",
             f"Approval held (never executed): {report['approval_held']}.",
             f"Wall seconds {report['wall_seconds']}; model calls 0; infrastructure and total cost unknown.",
             f"Enumeration complete for selected history: {report['enumeration_complete']}. Historical/effectiveness coverage is not established.", ""]
    if report.get("abandoned_check_suites"):
        lines.append(f"{report['abandoned_check_suites']} queued check suites with no check runs, not created or updated within a day of the snapshot, were treated as abandoned; their earlier runs were read, but a check an app posts to one later is not counted.")
    if report.get("paused"):
        lines.append("Paused to preserve the 1000-request core API reserve. Repeat the command after the budget resets; completed repositories and cached reads are reused.")
    lines.append(f"Repositories visited {len(report['repositories'])} of {len(report['repositories_selected'])}; API GET requests {report['api_requests']}.")
    lines.extend(f"- {code}: {count}" for code, count in sorted(report["skipped_by_reason"].items()))
    lines.append(f"\nSee report.json for source URLs, per-repository results, and {len(report['incomplete_reasons'])} coverage limits.")
    return "\n".join(lines) + "\n"


def reason_code(reason):
    for pattern, code in (
        ("flaky failure", "FLAKY_FAILURE"),
        ("runner or startup", "RUNNER_STARTUP_UNAVAILABLE"),
        ("no later green", "NO_MATCHING_GREEN"),
        ("external check", "UNSUPPORTED_EXTERNAL_CHECK"),
        ("checkout", "UNSUPPORTED_CHECKOUT_CONTEXT"),
        ("workflow action", "UNSUPPORTED_ACTION_SETTINGS"),
        ("workflow preparation", "UNSUPPORTED_WORKFLOW_PREPARATION"),
        ("workflow needs", "UNSUPPORTED_WORKFLOW_FEATURES"),
        ("step requires", "UNSUPPORTED_STEP_SETTINGS"),
        ("OS or runner", "UNSUPPORTED_PLATFORM"),
        ("runtime unavailable", "RUNTIME_UNAVAILABLE"),
        ("timed out", "TIMEOUT_OR_TOOL_UNAVAILABLE"),
        ("local tool unavailable", "TIMEOUT_OR_TOOL_UNAVAILABLE"),
        ("infrastructure unavailable", "DEPENDENCY_OR_INFRASTRUCTURE_UNAVAILABLE"),
        ("no passing tests executed", "NO_TESTS_EXECUTED"),
        ("structured test results unavailable", "TEST_RESULTS_UNAVAILABLE"),
        ("expected failures", "EXPECTED_FAILURES_REMAIN"),
        ("failed test", "FAILED_TEST_NOT_PROVEN"),
        ("test runner errors", "TEST_RUNNER_ERROR"),
        ("read operation unavailable", "SOURCE_OR_CLONE_UNAVAILABLE"),
    ):
        if pattern in (reason or ""):
            return code
    return "UNSUPPORTED_OR_UNPROVEN"


def execute(args):
    started = time.monotonic()
    root = private_store(args.store)
    full_history = getattr(args, 'full_history', False)
    manifest_path = root / 'ci-checkpoints' / 'repositories.json'
    selection = dict(owner=args.owner, repo=sorted(set(repo_name(r) for r in args.repo)))
    if full_history and manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        need(manifest['selection'] == selection)
        repos = manifest['repositories']
    else:
        repos = list(dict.fromkeys([repo_name(r) for r in args.repo] + (discover(args.owner) if args.owner else [])))
        if full_history:
            repos.sort()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    base = c.ordinary_path(root / "ci-learning")
    base.mkdir(mode=0o700, exist_ok=True)
    need(base.stat().st_mode & 0o077 == 0)
    output = base / ("run-" + uuid.uuid4().hex)
    output.mkdir(mode=0o700)
    session = None
    checkpoint_dir = root / 'ci-checkpoints'
    previous_wall = 0
    requests_before = 0
    if full_history:
        cache = c.ordinary_path(getattr(args, 'history_cache', None) or root / 'ci-history-cache')
        need('.coldfix' in cache.parts)
        need(not cache.exists() or cache.is_dir() and cache.stat().st_mode & 0o077 == 0)
        cache.mkdir(mode=0o700, parents=True, exist_ok=True)
        checkpoint_dir.mkdir(mode=0o700, exist_ok=True)
        need(checkpoint_dir.stat().st_mode & 0o077 == 0)
        cache_snapshot = cache / 'snapshot.json'
        if not manifest_path.exists():
            snapshot = (json.loads(cache_snapshot.read_text())['snapshot'] if cache_snapshot.exists()
                        else time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
            c.atomic_create(manifest_path, c.encoded(dict(selection=selection, repositories=repos, snapshot=snapshot)))
        snapshot = json.loads(manifest_path.read_text())['snapshot']
        instant(snapshot)
        if cache_snapshot.exists():
            need(json.loads(cache_snapshot.read_text())['snapshot'] == snapshot)
        else:
            c.atomic_create(cache_snapshot, c.encoded(dict(snapshot=snapshot)))
        usage = json.loads(run(['gh', 'api', 'rate_limit']))['resources']['core']
        session = HistorySession(cache, usage['remaining'], snapshot)
        session.requests = 1
        progress = checkpoint_dir / 'progress.json'
        if progress.exists():
            previous = json.loads(progress.read_text())
            need(previous['repos'] == repos and previous['timeout'] == args.timeout)
            previous_wall, requests_before = previous['wall_seconds'], previous['api_requests']
    results, reasons, requests, repositories, approval_held = [], [], 0, [], 0
    paused = False
    ledger = checkpoint_dir / 'promoted.json'
    promoted_ids = set(json.loads(ledger.read_text())) if full_history and ledger.exists() else set()
    for repo in repos:
        checkpoint = checkpoint_dir / (c.digest(repo) + '.json')
        if full_history and checkpoint.exists():
            saved = json.loads(checkpoint.read_text())
            results.extend(saved['results'])
            reasons.extend(saved['reasons'])
            repositories.append(saved['repository'])
            approval_held += saved['approval_held']
            continue
        github = GitHub(repo, full_history=True, session=session, snapshot=snapshot) if full_history else GitHub(repo)
        first = len(results)
        source_status = "READ"
        print(f"ci-learning: repository {len(repositories) + 1}/{len(repos)}", file=sys.stderr, flush=True)
        try:
            failures = collect(github, None if full_history else args.max_failures)
            for failure in failures:
                try:
                    results.append(attempt(github, failure, root, output, args.timeout))
                    lesson = results[-1].get("lesson") or {}
                    if full_history and lesson.get("verification") == "proven" and "receipt_id" in lesson:
                        promoted_ids.add(lesson["id"])
                        c.atomic_create(ledger, c.encoded(sorted(promoted_ids)), replace=True)
                    if results[-1]["status"] == "SKIPPED" and reason_code(results[-1]["reason"]) in TRANSIENT:
                        github.incomplete.append("transient replay failure; rescan on resume")
                except SOURCE_ERRORS:
                    github.incomplete.append("replay source or proof storage unavailable; rescan on resume")
                    results.append(dict(repo=repo, source=failure.source, links=[source_link(repo, failure)],
                        status="SKIPPED", reason="invalid metadata or local proof storage unavailable", red_reproduced=False, proven=False, lesson=None))
        except BudgetPause:
            reasons.append(repo + ' paused to preserve 1000 core API requests')
            source_status = 'BUDGET_PAUSED'
            paused = True
        except SOURCE_ERRORS:
            reasons.append(repo + " source enumeration unavailable or incomplete")
            source_status = "SOURCE_UNAVAILABLE"
        reasons.extend(repo + ": " + reason for reason in github.incomplete)
        approval_held += len(github.held)
        requests += github.requests
        repositories.append(dict(repo=repo, status=source_status, failures_found=len(results) - first,
                                 api_requests=github.requests, limits=github.incomplete,
                                 abandoned_check_suites=github.abandoned))
        if full_history and source_status == 'READ' and not github.incomplete:
            retained = []
            for row in results[first:]:
                row = dict(row)
                if 'artifact' in row:
                    row['artifact'] = str(output / row['artifact'])
                retained.append(row)
            c.atomic_create(checkpoint, c.encoded(dict(results=retained, repository=repositories[-1],
                reasons=[repo + ': ' + reason for reason in github.incomplete], approval_held=len(github.held))))
        if paused:
            break
    for row in results:
        row["reason_code"] = reason_code(row["reason"]) if row["status"] == "SKIPPED" else "PROVEN"
    promoted = len({lesson.get("id") or lesson.get("lesson") for lesson in (r.get("lesson") for r in results)
                    if lesson and lesson.get("verification") == "proven"
                    and ("receipt_id" in lesson or lesson.get("lesson") in promoted_ids)})
    wall = round(previous_wall + time.monotonic() - started, 3)
    if full_history:
        requests = requests_before + session.requests
        c.atomic_create(checkpoint_dir / 'progress.json', c.encoded(dict(repos=repos, timeout=args.timeout,
            wall_seconds=wall, api_requests=requests)), replace=True)
    report = dict(version=1, results=results, counts=dict(failures_found=len(results),
        red_reproduced=sum(r["red_reproduced"] for r in results), proven=sum(r["proven"] for r in results),
        skipped=sum(r["status"] == "SKIPPED" for r in results), lessons_promoted=promoted),
        approval_held=approval_held, paused=paused, full_history=full_history,
        skipped_reasons=dict(Counter(r["reason"] for r in results if r["status"] == "SKIPPED")),
        skipped_by_reason=dict(Counter(r["reason_code"] for r in results if r["status"] == "SKIPPED")),
        wall_seconds=wall, api_requests=requests, tool_model_calls=0, model_cost_usd=0,
        infrastructure_cost_usd=None, total_cost_usd=None, approximate_cost_per_lesson_usd=None,
        wall_seconds_per_promoted_lesson=round(wall / promoted, 3) if promoted else None,
        enumeration_complete=not reasons, incomplete_reasons=reasons,
        abandoned_check_suites=sum(r.get('abandoned_check_suites', 0) for r in repositories),
        limits=dict(max_failures_per_repo=None if full_history else args.max_failures,
                    external_commits_per_repo=None if full_history else args.max_failures,
                    external_suites_per_commit=None if full_history else args.max_failures,
                    external_checks_per_suite=None if full_history else args.max_failures,
                    timeout_seconds=args.timeout, workflow_history_items_per_repo=None if full_history else 1000),
        repositories_selected=repos, repositories=repositories,
        coverage="completed workflow runs across branches and external checks on default-branch commits; bounded selected history, not complete historical or effectiveness coverage")
    c.atomic_create(output / "report.md", report_markdown(report))
    c.atomic_create(output / "report.json", c.encoded(report))
    return dict(report=str(output / "report.json"), markdown=str(output / "report.md"), **report["counts"], enumeration_complete=not reasons, paused=paused)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", action="append", default=[], help="OWNER/NAME; repeat to select repositories")
    parser.add_argument("--owner", help="discover all private repositories using gh repo list")
    parser.add_argument("--max-failures", type=int, default=20, help="per-repository failure and external-history selection cap, default 20")
    parser.add_argument('--full-history', action='store_true', help='remove run, commit and failure caps; cache metadata and resume paused repositories')
    parser.add_argument('--history-cache', help='private frozen metadata cache for repeatable corpus rounds; requires --full-history')
    parser.add_argument("--timeout", type=float, default=120, help="positive per-local-command timeout, default 120 seconds")
    parser.add_argument("--store", default=".coldfix")
    args = parser.parse_args(argv)
    try:
        need(args.repo or args.owner)
        need(not args.history_cache or args.full_history)
        need(args.max_failures > 0 and math.isfinite(args.timeout) and args.timeout > 0)
        summary = execute(args)
        print(json.dumps(summary, sort_keys=True))
        return 3 if summary['paused'] else 0
    except CleanupFailed as error:
        print(str(error), file=sys.stderr)
        return 2
    except SOURCE_ERRORS:
        print("ci_fix_learning: invalid input or unavailable source; no source output printed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
