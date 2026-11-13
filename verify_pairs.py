#!/usr/bin/env python3
"""Import verify FAIL-then-PASS fix pairs as evidence-backed Coldfix failure records. Rerunnable."""

import argparse
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit

import coldfix as c
from failure_ingest import import_records, record, record_id

VERDICT = re.compile(r"(?m)^(PASS|FAIL): (?:tests|checks)=\d+ head=([0-9a-f]{12})")
REAL_PASS = re.compile(r"(?m)^PASS: real Linux verification")
QUEUE = re.compile(r"(?m)^QUEUED/INCOMPLETE\b")
INFRA = re.compile(r"(?m)Terminated {4,}\S|^(?:Terminated|Killed)$|ssh: connect to host|Connection (?:reset|timed out)")
RESULT = re.compile(r"(?m)^(OK|FAILED)\b")
RAN = re.compile(r"(?m)^Ran (\d+) tests? in")
FLEET = re.compile(r"\.fleet/verify\.sh")
QUEUE_NOISE = re.compile(r"^(queued behind \d+; waiting for verify lock|queued, retry \d+|main queued, retry \d+)$")
FAIL_LINES = re.compile(r"^(?:FAIL|ERROR|FAILED|Traceback|AssertionError|Terminated)|Terminated {4,}")
LOOP_NAME = re.compile(r"([0-9a-f]{12})-[0-9a-f]{12}-[0-9a-f]{8}\.log\Z")
PR_LINE = re.compile(r"(?m)^pr: \w+ ([0-9a-f]{40})")
START = re.compile(r"(?m)^start (\S+)")
START_HEAD = re.compile(r"(?m)^start \S+ \S+ \S+#\d+ ([0-9a-f]{40})\b")
SHA12 = re.compile(r"[0-9a-f]{12}\Z")
EXCERPT_CHARS = 3500
DIFF_CHARS = 8000
REASONS = ("queue-timeout", "infra-terminated", "no-verdict", "no-head", "missing-commits", "no-pr-join",
           "same-head", "not-ancestor", "no-later-pass")


@dataclass
class Attempt:
    source: str
    repo: str
    head: str
    status: str
    reason: str
    excerpt: str
    occurred: str


def clip(lines, limit=EXCERPT_CHARS):
    out, size = [], 0
    for line in lines:
        line = line.rstrip()[:240]
        if size + len(line) + 1 > limit:
            break
        out.append(line)
        size += len(line) + 1
    return "\n".join(out)


def classify(content, head=None):
    verdicts = [m for m in VERDICT.finditer(content) if head is None or head.startswith(m.group(2))]
    verdict = verdicts[-1].group(1) if verdicts else None
    if REAL_PASS.search(content) and verdict in (None, "PASS"):
        verdict = "PASS"
    lines = [line for line in content.split("\n") if line.strip() and not QUEUE_NOISE.match(line.strip())]
    if verdict != "PASS" and QUEUE.search(content):
        return "REJECT", "queue-timeout", ""
    if verdict != "PASS" and INFRA.search(content):
        return "REJECT", "infra-terminated", ""
    results = RESULT.findall(content)
    ran = [int(n) for n in RAN.findall(content)]
    if verdict is None and not FLEET.search(content) and results and ran and max(ran) > 0:
        verdict = "PASS" if results[-1] == "OK" else "FAIL"
    if verdict == "FAIL":
        marked = [line for line in lines if FAIL_LINES.search(line)]
        picked = [m.group(0) for m in verdicts[-1:]] + marked[:30] + lines[-10:]
        return "FAIL", "", clip(dict.fromkeys(picked))
    if verdict == "PASS":
        keep = [line for line in lines if VERDICT.match(line) or REAL_PASS.match(line) or RAN.match(line)
                or line.startswith(("OK", "FLEET_"))]
        return "PASS", "", clip(keep[-12:])
    if not lines:
        return "REJECT", "queue-timeout", ""
    return "REJECT", "no-verdict", ""


def stamp(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read(path):
    return Path(path).read_text(encoding="utf-8", errors="replace")


def loop_attempt(path, repo):
    match = LOOP_NAME.fullmatch(Path(path).name)
    if not match:
        return None
    head = match.group(1)
    return Attempt(str(path), repo, head, *classify(read(path), head), stamp(Path(path).stat().st_mtime))


def settle_attempt(out):
    out = Path(out)
    content = read(out)
    sibling = out.with_name(out.stem + "-verify.log")
    underlying = read(sibling) if sibling.exists() else ""
    verdict = VERDICT.search(content)
    full = PR_LINE.search(content)
    named = START_HEAD.search(content)
    head = verdict.group(2) if verdict else full.group(1)[:12] if full else named.group(1)[:12] if named else None
    start = START.search(content)
    occurred = stamp(out.stat().st_mtime)
    if start:
        try:
            occurred = datetime.fromisoformat(start.group(1)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            pass
    if head is None:
        return Attempt(str(out), "", "", "REJECT", "no-head", "", occurred)
    status, reason, excerpt = classify(content + "\n" + underlying, head)
    failed = re.search(rf"(?m)^{re.escape(c.fleet_label('verify'))}(?: at head)? exit [1-9]\d*$", content)
    if failed and reason == "no-verdict":
        marked = [line for line in content.split("\n") if FAIL_LINES.search(line)]
        status, reason, excerpt = "FAIL", "", clip(dict.fromkeys(marked[:30]))
    elif status != "REJECT" and not verdict and not REAL_PASS.search(content) and not (status == "FAIL" and failed):
        status, reason, excerpt = "REJECT", "no-verdict", ""
    return Attempt(str(out), "", head, status, reason, excerpt, occurred)


def collect(projects, settle):
    attempts = []
    for log_dir in sorted(Path(projects).glob("*/.git/verify-loop")) + sorted(Path(projects).glob("*/.worktrees/*/.git/verify-loop")):
        repo = str(log_dir.parent.parent)
        for log in sorted(log_dir.glob("*.log")):
            attempt = loop_attempt(log, repo)
            if attempt:
                attempts.append(attempt)
    for out in sorted(Path(settle).glob("settle-*.out")):
        attempts.append(settle_attempt(out))
    return attempts


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, errors="replace")


def github_slug(url):
    scp = re.fullmatch(r"git@github\.com:(.+)", url)
    if scp:
        path = "/" + scp.group(1)
    else:
        parts = urlsplit(url)
        if parts.scheme not in ("https", "http", "ssh", "git", "git+ssh") or parts.hostname != "github.com":
            return None
        path = parts.path
    match = re.fullmatch(r"/([^/\s]+/[^/\s]+?)(?:\.git)?/?", path)
    return match.group(1) if match else None


class Repos:
    def __init__(self, dirs, prs=None):
        self.dirs = list({self.common(str(Path(d))): str(Path(d)) for d in dirs}.values())
        self.prs = dict(prs or {})
        self.slugs = {}
        self.associated = {}

    @lru_cache(maxsize=None)
    def common(self, repo):
        result = git(repo, "rev-parse", "--git-common-dir")
        return os.path.realpath(os.path.join(repo, result.stdout.strip())) if result.returncode == 0 else None

    @lru_cache(maxsize=None)
    def full(self, repo, head):
        result = git(repo, "rev-parse", "--verify", "-q", head + "^{commit}")
        return result.stdout.strip() if result.returncode == 0 else None

    def locate(self, head):
        found = {}
        for repo in self.dirs:
            common = self.common(repo)
            if common and common not in found and self.full(repo, head):
                found[common] = repo
        return list(found.values())

    def slug(self, repo):
        if repo not in self.slugs:
            url = git(repo, "remote", "get-url", "origin").stdout.strip()
            self.slugs[repo] = github_slug(url)
        return self.slugs[repo]

    def pr_for(self, repo, full):
        key = self.common(repo) or repo
        if key in self.prs:
            numbers = {row["number"] for row in self.prs[key]
                       if full == row.get("headRefOid") or any(full == commit.get("oid") for commit in row.get("commits", []))}
        else:
            slug = self.slug(repo)
            if not slug:
                return None
            if (slug, full) not in self.associated:
                result = subprocess.run(["gh", "api", f"repos/{slug}/commits/{full}/pulls?per_page=100"], capture_output=True, text=True)
                try:
                    self.associated[(slug, full)] = {row["number"] for row in json.loads(result.stdout)} if result.returncode == 0 else set()
                except (ValueError, TypeError, KeyError):
                    self.associated[(slug, full)] = set()
            numbers = self.associated[(slug, full)]
        return next(iter(numbers)) if len(numbers) == 1 else None

    def ancestor(self, repo, older, newer):
        return git(repo, "merge-base", "--is-ancestor", older, newer).returncode == 0

    def distance(self, repo, older, newer):
        return int(git(repo, "rev-list", "--count", f"{older}..{newer}").stdout.strip() or 0)

    def evidence_diff(self, repo, older, newer):
        stat = git(repo, "diff", "--stat", older, newer).stdout
        patch = git(repo, "diff", "--unified=3", older, newer).stdout
        log = git(repo, "log", "--format=%h %s", f"{older}..{newer}").stdout
        return dict(commits=log.strip().split("\n"), stat=clip(stat.split("\n"), 2000),
                    patch=patch[:DIFF_CHARS], patch_truncated=len(patch) > DIFF_CHARS)


def join(attempts, repos, rejected):
    resolved = []
    for attempt in attempts:
        if attempt.status == "REJECT":
            rejected[attempt.reason] += 1
            continue
        candidates = [attempt.repo] if attempt.repo else repos.locate(attempt.head)
        if len(candidates) != 1:
            rejected["missing-commits" if not candidates else "no-pr-join"] += 1
            continue
        repo = candidates[0]
        full = repos.full(repo, attempt.head)
        if not full:
            rejected["missing-commits"] += 1
            continue
        resolved.append((attempt, repo, full))
    failing = {repos.common(repo) for attempt, repo, _ in resolved if attempt.status == "FAIL"}
    joined = {}
    for attempt, repo, full in resolved:
        if repos.common(repo) not in failing:
            continue
        number = repos.pr_for(repo, full)
        if number is None:
            rejected["no-pr-join"] += 1
            continue
        joined.setdefault((repos.common(repo), number), dict(repo=repo, heads={}))["heads"].setdefault(full, []).append(attempt)
    return joined


def when(attempt):
    return attempt.occurred, attempt.source


def find_pairs(attempts, repos):
    rejected = Counter()
    pairs = []
    for (_, number), group in sorted(join(attempts, repos, rejected).items(), key=lambda item: item[0][1]):
        repo, heads = group["repo"], group["heads"]
        failing = {h for h, rows in heads.items() if any(a.status == "FAIL" for a in rows)}
        passing = {h for h, rows in heads.items() if any(a.status == "PASS" for a in rows)}
        rejected["same-head"] += len(failing & passing)
        for x in sorted(failing - passing):
            fail = min((a for a in heads[x] if a.status == "FAIL"), key=when)
            passes = {y: [a for a in heads[y] if a.status == "PASS" and a.occurred >= fail.occurred] for y in passing - failing}
            later = [y for y, rows in passes.items() if rows]
            fixes = [y for y in later if repos.ancestor(repo, x, y)]
            if not fixes:
                rejected["not-ancestor" if later else "no-later-pass"] += 1
                continue
            y = min(fixes, key=lambda candidate: (repos.distance(repo, x, candidate), candidate))
            pairs.append(dict(repo=repo, slug=repos.slug(repo) or Path(repo).name, pr=number, fail=fail,
                              ok=min(passes[y], key=when), x=x, y=y))
    return pairs, rejected


def evidence(pair, repos):
    side = lambda attempt, head: dict(head=head, log=Path(attempt.source).name, occurred=attempt.occurred, excerpt=attempt.excerpt)
    return dict(type="verify-fix-pair", repo=pair["slug"], pr=pair["pr"],
                failing_check=side(pair["fail"], pair["x"]), passing_check=side(pair["ok"], pair["y"]),
                diff=repos.evidence_diff(pair["repo"], pair["x"], pair["y"]))


def source_ref(pair):
    return f"verify-pair:{pair['slug']}:{pair['pr']}:{pair['x'][:12]}:{pair['y'][:12]}"


def event(pair, repos):
    return dict(kind="test", repo=pair["slug"], command=c.fleet_label("verify"), error=c.encoded(evidence(pair, repos)).strip(),
                agent="unknown", thread="", occurred=pair["fail"].occurred, source_ref=source_ref(pair))


def run(root, projects, settle, dry_run=False, repos=None):
    c.require(c.fleet_cli(), "verify pair import is off: set COLDFIX_FLEET_CLI")
    attempts = collect(projects, settle)
    if repos is None:
        dirs = {str(p.parent) for p in Path(projects).glob("*/.git")} | {str(p.parent) for p in Path(projects).glob("*/.worktrees/*/.git")}
        repos = Repos(sorted(dirs))
    pairs, rejected = find_pairs(attempts, repos)
    ingested = dict(imported=0, skipped=0)
    if pairs and not dry_run:
        path = c.ordinary_path(root)
        known = c.load_store(path)[1]["failures"]
        fresh = [pair for pair in pairs if record_id(source_ref(pair)) not in known]
        ingested = import_records(path, [record(event(pair, repos)) for pair in fresh])
        ingested["skipped"] += len(pairs) - len(fresh)
    return dict(attempts=len(attempts), pairs=len(pairs), ingested=ingested["imported"], already_present=ingested["skipped"],
                rejected={reason: rejected[reason] for reason in REASONS if rejected[reason]},
                sources=[source_ref(pair) for pair in pairs])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", default=".coldfix")
    parser.add_argument("--projects", required=True)
    parser.add_argument("--settle-dir", default="/tmp")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run(args.store, args.projects, args.settle_dir, args.dry_run)
    except c.ColdfixError as error:
        print("verify_pairs: " + str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
