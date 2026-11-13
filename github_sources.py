#!/usr/bin/env python3
"""Read GitHub history into a private Coldfix JSONL batch. Stdlib and gh only."""

from collections import Counter
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import uuid

import coldfix


COMMIT = "oid url message committedDate author { user { login } }"
PAGE = "pageInfo { hasNextPage endCursor } totalCount"
PULL = """query($owner:String!,$name:String!,$number:Int!) {
  repository(owner:$owner,name:$name) {
    pullRequest(number:$number) {
      number title body url mergedAt headRefOid mergeCommit { %s } author { login }
    }
  }
}""" % COMMIT
PULLS = """query($owner:String!,$name:String!,$cursor:String) {
  repository(owner:$owner,name:$name) {
    pullRequests(first:100,after:$cursor,orderBy:{field:CREATED_AT,direction:ASC}) {
      nodes { number title body url mergedAt headRefOid mergeCommit { %s }
              author { login } }
      pageInfo { hasNextPage endCursor } totalCount
    }
  }
}""" % COMMIT
HISTORY = """query($owner:String!,$name:String!,$cursor:String) {
  repository(owner:$owner,name:$name) {
    defaultBranchRef { target { ... on Commit {
      history(first:100,after:$cursor) { nodes { %s } %s }
    } } }
  }
}""" % (COMMIT, PAGE)
PR_COMMITS = """query($owner:String!,$name:String!,$number:Int!,$cursor:String) {
  repository(owner:$owner,name:$name) {
    pullRequest(number:$number) {
      commits(first:100,after:$cursor) { nodes { commit { %s } } %s }
    }
  }
}""" % (COMMIT, PAGE)
CLOSING = """query($owner:String!,$name:String!,$number:Int!,$cursor:String) {
  repository(owner:$owner,name:$name) {
    pullRequest(number:$number) {
      closingIssuesReferences(first:100,after:$cursor) {
        nodes { title body url closed closedAt author { login } repository { nameWithOwner } }
        pageInfo { hasNextPage endCursor } totalCount
      }
    }
  }
}"""
REVERT = re.compile(r'(?im)^(?:revert(?:\([^\n]*\))?[:\s]|this reverts commit [0-9a-f]{7,40}\b)')
FIX = re.compile(r'(?i)^fix(?:\([^\n)]*\))?!?:\s|^fix(?:es|ed)?\s+\S')
FINDING = re.compile(r'(?i)\[P[0-3](?: Badge)?\]|badge/P[0-3]-|\b(?:changes requested|requesting changes)\b')
IMPERATIVE = re.compile(
    r'(?i)^(?:please\s+|(?:can|could|would) you\s+(?:please\s+)?|(?:we|you) need to\s+)?'
    r'(?:fix|change|replace|remove|add|update|use|avoid|rename|move|handle|validate|check|return|ensure|split|include|make)\b\s+\S')
NEGATED = re.compile(r'(?i)\b(?:no|not|without)\s+(?:further\s+)?(?:changes requested|requesting changes)\b')
FAILED = {"failure", "timed_out", "action_required", "startup_failure"}
SHA = re.compile(r"[0-9a-f]{40}\Z")
REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*")


def need(value):
    coldfix.require(value, "invalid or incomplete GitHub response; no batch published")


def string(value, empty=False):
    return coldfix.text(value, "GitHub field", optional=empty)


def timestamp(value):
    coldfix.occurred(value)
    return coldfix.time_key(value)


def actor(value):
    if value is None:
        return "unknown"
    need(isinstance(value, dict))
    return string(value["login"])


def change_finding(body):
    fenced = False
    for line in body.splitlines():
        line = line.strip()
        if line.startswith(">"):
            continue
        if line.startswith(("```", "~~~")):
            if not fenced and re.match(r"```suggestion(?:\s|$)", line):
                return True
            fenced = not fenced
            continue
        if fenced or NEGATED.search(line):
            continue
        if FINDING.search(line):
            return True
        line = re.sub(r"^(?:[-*+] |\d+[.)] )", "", line)
        if any(IMPERATIVE.search(sentence) for sentence in re.split(r"[.!?;]\s+", line)):
            return True
    return False


class GitHub:
    def __init__(self, repo):
        self.repo = repo
        self.owner, self.name = repo.split("/")
        self.requests = 0

    def api(self, endpoint, payload=None):
        command = ["gh", "api", "--hostname", "github.com", "--method", "POST" if payload else "GET", endpoint]
        if payload:
            # Only fixed query documents are posted. Source text never becomes a query or shell command.
            need(payload["query"] in (PULL, PULLS, HISTORY, PR_COMMITS, CLOSING))
            command += ["--input", "-"]
        self.requests += 1
        try:
            result = subprocess.run(command, input=json.dumps(payload) if payload else None,
                                    capture_output=True, text=True, check=False)
        except (OSError, UnicodeError):
            raise coldfix.ColdfixError("GitHub CLI unavailable; no batch published") from None
        coldfix.require(result.returncode == 0, "GitHub API request failed; no batch published")
        try:
            value = json.loads(result.stdout, object_pairs_hook=coldfix.unique_object)
        except (ValueError, RecursionError):
            raise coldfix.ColdfixError("invalid GitHub JSON; no batch published") from None
        need(not isinstance(value, dict) or not value.get("errors"))
        return value

    def connection(self, query, path, number=None):
        cursor, cursors, count, total = None, set(), 0, None
        while True:
            variables = dict(owner=self.owner, name=self.name, cursor=cursor)
            if number is not None:
                variables["number"] = number
            value = self.api("graphql", dict(query=query, variables=variables))
            for key in ("data", "repository", *path):
                value = value[key]
                if value is None and query == HISTORY and key == "defaultBranchRef":
                    return
            need(isinstance(value, dict) and isinstance(value["nodes"], list))
            size = value["totalCount"]
            need(type(size) is int and size >= 0 and (total is None or total == size))
            total = size
            count += len(value["nodes"])
            info = value["pageInfo"]
            need(isinstance(info, dict) and type(info["hasNextPage"]) is bool and count <= total)
            for node in value["nodes"]:
                need(isinstance(node, dict))
                yield node
            if not info["hasNextPage"]:
                need(count == total)
                return
            cursor = info["endCursor"]
            need(isinstance(cursor, str) and cursor and cursor not in cursors and value["nodes"])
            cursors.add(cursor)

    def pages(self, endpoint, key=None, query=""):
        page, count, total = 1, 0, None
        seen = set()
        seen = set()
        while True:
            value = self.api(f"repos/{self.repo}/{endpoint}?per_page=100&page={page}{query}")
            if key:
                need(isinstance(value, dict) and type(value["total_count"]) is int and value["total_count"] >= 0)
                need(total is None or total == value["total_count"])
                total = value["total_count"]
                value = value[key]
            need(isinstance(value, list) and len(value) <= 100)
            count += len(value)
            need(total is None or count <= total)
            digest = coldfix.digest(json.dumps(value, sort_keys=True))
            need(not value or digest not in seen)
            seen.add(digest)
            for row in value:
                need(isinstance(row, dict))
                identity = row["id"]
                need(type(identity) is int and identity > 0 and identity not in seen)
                seen.add(identity)
                yield row
            if len(value) < 100:
                need(total is None or count == total)
                return
            page += 1

    def pull(self, number):
        value = self.api("graphql", dict(query=PULL, variables=dict(owner=self.owner, name=self.name, number=number)))
        value = value["data"]["repository"]["pullRequest"]
        need(isinstance(value, dict) and value["number"] == number)
        return value


def collect(github, since=None, prs=None):
    events, seen_commits, seen_checks, seen_shas = [], set(), set(), set()
    cutoff = timestamp(since) if since else None

    def emit(kind, command, error, url, when, agent, repo=None, thread=""):
        event = dict(kind=kind, command=command, error=error, repo=repo or github.repo,
                     agent=agent, source_ref=url, occurred=when, thread=thread)
        coldfix.event_record(event, "github")
        if cutoff is None or timestamp(when) >= cutoff:
            events.append(event)

    def commit(row):
        oid = string(row["oid"])
        need(SHA.fullmatch(oid))
        message, url, when = string(row["message"], empty=True), string(row["url"]), row["committedDate"]
        timestamp(when)
        author = row["author"]
        need(author is None or isinstance(author, dict))
        who = actor(author["user"]) if author else "unknown"
        if oid not in seen_commits:
            seen_commits.add(oid)
            if REVERT.search(message):
                emit("revert", "git revert " + oid, message, url, when, who)
            elif FIX.search(message):
                emit("issue", "fix commit " + oid, message, url, when, who)
        return oid

    if not prs:
        for row in github.connection(HISTORY, ("defaultBranchRef", "target", "history")):
            commit(row)
    pulls = (github.pull(number) for number in dict.fromkeys(prs)) if prs else github.connection(PULLS, ("pullRequests",))
    for pull in pulls:
        number = pull["number"]
        need(type(number) is int and number > 0)
        title, body, url = string(pull["title"]), string(pull["body"], empty=True), string(pull["url"])
        merged = pull["mergedAt"]
        who = actor(pull["author"])
        if merged is not None:
            timestamp(merged)
            if REVERT.search(title + "\n" + body):
                emit("revert", f"revert PR #{number}", title + "\n" + body, url, merged, who, thread=url)
            for issue in github.connection(CLOSING, ("pullRequest", "closingIssuesReferences"), number):
                need(type(issue["closed"]) is bool)
                title_issue, body_issue = string(issue["title"]), string(issue["body"], empty=True)
                if issue["closed"]:
                    timestamp(issue["closedAt"])
                    emit("issue", f"closed by merged PR {url}", title_issue + "\n" + body_issue,
                         string(issue["url"]), merged, actor(issue["author"]),
                         repo=string(issue["repository"]["nameWithOwner"]), thread=url)
        pr_commits = [row["commit"] for row in github.connection(PR_COMMITS, ("pullRequest", "commits"), number)]
        shas = {commit(row) for row in pr_commits}
        commit_files = {}
        if pull["mergeCommit"] is not None:
            shas.add(commit(pull["mergeCommit"]))
        oid = pull["headRefOid"]
        if oid is not None:
            need(isinstance(oid, str) and SHA.fullmatch(oid))
            shas.add(oid)
        requested_reviews = set()
        for endpoint, formal in ((f"pulls/{number}/reviews", True),
                                 (f"pulls/{number}/comments", False),
                                 (f"issues/{number}/comments", False)):
            for review in github.pages(endpoint):
                state = review["state"] if formal else "COMMENTED"
                need(state in ("APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED", "PENDING"))
                when = review.get("submitted_at") if formal else review["created_at"]
                if state == "PENDING":
                    need(when is None)
                    continue
                body = review.get("body")
                content = string("" if body is None else body, empty=True)
                who = actor(review["user"])
                timestamp(when)
                if who == "chatgpt-codex-connector[bot]" and any(
                        marker in content for marker in ("codex-pull-request-review-summary", "codex-review-receipt")):
                    continue
                if (endpoint == f"pulls/{number}/comments" and who == "chatgpt-codex-connector[bot]"
                        and "<!-- codex-code-review-finding:v1 -->" in content):
                    badge = re.search(r"(?i)(?:\b(P[0-3]) Badge\b|badge/(P[0-3])-)", content)
                    severity = next(filter(None, badge.groups())).upper() if badge else "unknown"
                    path = string(review["path"])
                    original = string(review["original_commit_id"])
                    commit_ids = [row["oid"] for row in pr_commits]
                    later_change = False
                    later_commits = pr_commits[commit_ids.index(original) + 1:] if original in commit_ids else []
                    for row in later_commits:
                        if timestamp(row["committedDate"]) <= timestamp(when) or row["oid"] == original:
                            continue
                        sha = row["oid"]
                        if sha not in commit_files:
                            # Commit files paginate independently of the PR commit list.
                            files = []
                            page = 1
                            while True:
                                detail = github.api(f"repos/{github.repo}/commits/{sha}?per_page=100&page={page}")
                                batch = detail["files"]
                                need(isinstance(batch, list) and len(batch) <= 100)
                                files.extend(batch)
                                if len(batch) < 100:
                                    break
                                need(page < 30)  # GitHub caps commit file responses at 3,000 files.
                                page += 1
                            commit_files[sha] = files
                        if any(string(file["filename"]) == path or file.get("previous_filename") == path
                               for file in commit_files[sha]):
                            later_change = True
                            break
                    reactions = {reaction["content"] for reaction in
                                 github.pages(f"pulls/comments/{review['id']}/reactions")}
                    if severity in ("P0", "P1") or later_change or "+1" in reactions:
                        classification = "real bug"
                    elif "-1" in reactions:
                        classification = "false alarm"
                    elif merged is not None:
                        classification = "unresolved"
                    else:
                        classification = "style"
                    emit("review", f"PR review finding {severity}: {classification}", content,
                         string(review["html_url"]), when, "codex-review", thread=url)
                    continue
                requested = state == "CHANGES_REQUESTED" or review.get("pull_request_review_id") in requested_reviews
                if formal and requested:
                    need(type(review["id"]) is int and review["id"] > 0)
                    requested_reviews.add(review["id"])
                if requested or change_finding(content):
                    emit("review", "PR review " + ("CHANGES_REQUESTED" if requested else "finding"), content or "Changes requested",
                         string(review["html_url"]), when, who, thread=url)
        for oid in sorted(shas):
            if oid in seen_shas:
                continue
            seen_shas.add(oid)
            # Per-suite traversal avoids the commit check-runs endpoint's 1,000-run ceiling.
            for suite in github.pages(f"commits/{oid}/check-suites", "check_suites"):
                suite_id = suite["id"]
                need(type(suite_id) is int and suite_id > 0)
                for check in github.pages(f"check-suites/{suite_id}/check-runs", "check_runs", "&filter=all"):
                    check_id = check["id"]
                    need(type(check_id) is int and check_id > 0)
                    conclusion = check["conclusion"]
                    need(conclusion is None or conclusion in FAILED | {"success", "neutral", "cancelled", "skipped", "stale"})
                    if check_id in seen_checks:
                        continue
                    seen_checks.add(check_id)
                    if conclusion in FAILED:
                        output = check["output"]
                        need(isinstance(output, dict))
                        details = [string(output[key], empty=True) if output[key] is not None else ""
                                   for key in ("title", "summary", "text")]
                        app = check["app"]
                        need(app is None or isinstance(app, dict))
                        agent = "unknown" if app is None else string(app.get("slug") if app.get("slug") is not None else app["name"])
                        reference = check["html_url"] if check["html_url"] is not None else check["url"]
                        when = check["completed_at"] if check["completed_at"] is not None else check["started_at"]
                        emit("ci", string(check["name"]), coldfix.encoded(dict(
                            head_sha=oid, details="\n".join([conclusion, *filter(None, details)]))).strip(),
                             string(reference), when, agent, thread=url)
    return events


RATE_LIMIT_ENDPOINT = "rate_limit"
OSS_DEFAULT_DAYS = 30
OSS_RUN_PAGES = 10
RERUN_WINDOW = timedelta(days=30)


class RateLimited(coldfix.ColdfixError):
    pass


class PublicGitHub(GitHub):
    """Read-only GET client for public repositories that stops before the rate limit is spent."""

    def __init__(self, repo):
        super().__init__(repo)
        self.remaining = None
        self.reset = None

    def refresh_budget(self):
        core = super().api(RATE_LIMIT_ENDPOINT)["resources"]["core"]
        need(type(core["remaining"]) is int and core["remaining"] >= 0 and type(core["reset"]) is int)
        self.remaining, self.reset = core["remaining"], core["reset"]

    def api(self, endpoint, payload=None):
        need(payload is None)
        if self.remaining is None:
            self.refresh_budget()
        if self.remaining <= 0:
            raise RateLimited(f"rate limit exhausted until {self.reset}")
        self.remaining -= 1
        return super().api(endpoint)


def oss_repos(path):
    names = []
    for line in coldfix.read_text(coldfix.ordinary_path(path)).splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            coldfix.require(REPO.fullmatch(line), "invalid repository in allowlist")
            names.append(line)
    coldfix.require(names, "allowlist names no repositories")
    return list(dict.fromkeys(names))


def utc(value):
    return timestamp(value).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def oss_collect(github, since):
    """Failed runs and fix or revert commits since `since` (UTC) as (events, complete).

    Completed runs are listed and failure conclusions kept locally, since the API has no startup_failure status filter; GitHub exposes at most OSS_RUN_PAGES pages per listing, so a full last page means truncation.
    Runs are listed from RERUN_WINDOW before `since` because a rerun can fail long after creation."""
    events = []

    def emit(kind, command, error, url, when, agent):
        event = dict(kind=kind, command=command, error=error, repo=github.repo,
                     agent=agent, source_ref=url, occurred=when, thread="")
        coldfix.event_record(event, "github")
        events.append(event)

    repo = github.api(f"repos/{github.repo}")
    need(isinstance(repo, dict) and repo["private"] is False)
    window = timestamp(since)
    created = (window - RERUN_WINDOW).strftime("%Y-%m-%dT%H:%M:%SZ")
    complete = True
    for page in range(1, OSS_RUN_PAGES + 1):
        value = github.api(f"repos/{github.repo}/actions/runs?status=completed&per_page=100&page={page}"
                           f"&created=%3E%3D{created}")
        rows = value["workflow_runs"]
        need(isinstance(rows, list) and len(rows) <= 100)
        for run in rows:
            need(isinstance(run, dict))
            if run["conclusion"] in FAILED:
                when = string(run["updated_at"])
                if timestamp(when) >= window:
                    emit("ci", string(run["name"]), "\n".join([run["conclusion"], string(run["display_title"], empty=True)]),
                         string(run["html_url"]), when, "github-actions")
        if len(rows) < 100:
            break
    else:
        complete = False
    page = 1
    while True:
        rows = github.api(f"repos/{github.repo}/commits?since={since}&per_page=100&page={page}")
        need(isinstance(rows, list) and len(rows) <= 100)
        for row in rows:
            oid, message = string(row["sha"]), string(row["commit"]["message"], empty=True)
            need(SHA.fullmatch(oid))
            when = string(row["commit"]["committer"]["date"])
            who = actor(row["author"]) if row.get("author") else "unknown"
            if REVERT.search(message):
                emit("revert", "git revert " + oid, message, string(row["html_url"]), when, who)
            elif FIX.search(message):
                emit("issue", "fix commit " + oid, message, string(row["html_url"]), when, who)
        if len(rows) < 100:
            return events, complete
        page += 1


def oss_gather(repos, since):
    """Gather each repository independently; one failing repository never hides the others."""
    results, events = [], []
    for repo in repos:
        github = PublicGitHub(repo)
        if results and results[-1]["status"] == "rate_limited":
            results.append(dict(repo=repo, status="skipped_rate_limited", records=0))
            continue
        try:
            found, complete = oss_collect(github, since)
        except RateLimited:
            results.append(dict(repo=repo, status="rate_limited", records=0))
            continue
        except (coldfix.ColdfixError, KeyError, TypeError, ValueError):
            results.append(dict(repo=repo, status="failed", records=0))
            continue
        counts = Counter(row["kind"] for row in found)
        events.extend(found)
        results.append(dict(repo=repo, status="ok" if complete else "incomplete", records=len(found),
                            requests=github.requests,
                            **{kind: counts[kind] for kind in ("revert", "issue", "ci")}))
    return results, events


def private_store(path):
    root = coldfix.ordinary_path(path)
    coldfix.require(root.name == ".coldfix", "store must be a private .coldfix directory")
    coldfix.require(not root.exists() or root.is_dir() and root.stat().st_mode & 0o077 == 0,
                    "store directory must have private permissions")
    return root


def import_batch(root, path):
    with coldfix.lesson_lock(root):
        _, store = coldfix.load_store(root)
        return coldfix.ingest(argparse.Namespace(format="jsonl", input=str(path)), root, store)


def import_existing(root):
    files = [*sorted(set(root.glob("github-*-pr*.jsonl")) | set(root.glob("github-events-*.jsonl"))
                        | set(root.glob("github-oss-*.jsonl"))),
             root / "github-audit" / "verified-events.jsonl"]
    total = Counter()
    for path in filter(Path.is_file, files):
        try:
            result = import_batch(root, path)
        except coldfix.ColdfixError:
            print(f"github_sources: cannot import {path.name}; earlier files stay imported", file=sys.stderr)
            return None
        total.update(files=1, imported_records=result["imported_records"],
                     skipped_duplicates=result["skipped_duplicates"])
    return dict(total, files=total["files"])


def migrate_codex_labels(root, github):
    with coldfix.lesson_lock(root):
        _, store = coldfix.load_store(root)
        candidates = []
        for record in store["failures"].values():
            match = re.fullmatch(r"https://github\.com/" + re.escape(github.repo) + r"/pull/([1-9][0-9]*)",
                                 record["thread"])
            if (match and record.get("version") == 2 and record["kind"] == "review"
                    and record["repo"] == github.repo and record["agent"] == "codex-review"
                    and record["command"] in ("PR review finding P2: false alarm", "PR review finding P3: false alarm")):
                candidates.append((record, int(match[1])))
        events = collect(github, prs=sorted({number for _, number in candidates})) if candidates else []
        unresolved = {(row["source_ref"], row["error"], row["command"]) for row in events
                      if row["command"] in ("PR review finding P2: unresolved", "PR review finding P3: unresolved")}
        changes = []
        for record, _ in candidates:
            command = record["command"].replace("false alarm", "unresolved")
            if (record["source_ref"], record["error"], command) in unresolved:
                updated = dict(record, command=command)
                updated["group_id"] = updated["signature"] = coldfix.group_id(updated)
                changes.append((record, updated))
        changed_ids = {record["id"] for record, _ in changes}
        referenced = any(changed_ids.intersection(row["evidence"])
                         for collection in ("proposals", "lessons") for row in store[collection].values())
        measured = any(changed_ids.intersection(row["snapshot"]["failures"])
                       for row in store["measurements"].values())
        if referenced or measured:
            print("github_sources: migration would invalidate proposal, lesson or measurement evidence; left unchanged",
                  file=sys.stderr)
            raise coldfix.ColdfixError("referenced migration records")
        identities = {(row["source_ref"], row["group_id"]) for row in store["failures"].values()
                      if row["version"] == 2}
        archived = 0
        for record, updated in changes:
            backup = root / "codex-label-backup" / (record["id"] + ".json")
            if backup.exists():
                coldfix.require(coldfix.read_text(backup) == coldfix.encoded(record),
                                "migration backup differs; left unchanged")
            else:
                coldfix.atomic_create(backup, coldfix.encoded(record))
            path = root / "failures" / (updated["id"] + ".json")
            identity = (updated["source_ref"], updated["group_id"])
            if identity in identities:
                os.replace(path, backup)
                archived += 1
            else:
                coldfix.validate(updated, "failures", updated["id"])
                coldfix.atomic_create(path, coldfix.encoded(updated), replace=True)
                identities.add(identity)
        coldfix.load_store(root)
        return dict(relabeled_records=len(changes) - archived, archived_duplicates=archived, requests=github.requests)


def main(argv=None):
    parser = coldfix.Parser(description=__doc__)
    parser.add_argument("--repo", help="GitHub OWNER/REPO")
    parser.add_argument("--oss-allowlist", help="file of public OWNER/REPO names, one per line; gathers failed CI "
                        "runs and fix commits read-only (default window: last %d days)" % OSS_DEFAULT_DAYS)
    parser.add_argument("--dry-run", action="store_true",
                        help="with --oss-allowlist: print per-repository record counts and write nothing")
    parser.add_argument("--store", default=".coldfix", help="private .coldfix directory")
    parser.add_argument("--output", help="new JSONL filename inside the store; default is unique per run")
    parser.add_argument("--migrate-codex-labels", action="store_true",
                        help="recheck stored Codex P2/P3 false alarms and relabel unresolved merged-PR findings")
    parser.add_argument("--since", help="inclusive event date or timestamp with timezone")
    parser.add_argument("--pr", type=int, action="append", help="read only this PR; repeat for multiple PRs")
    parser.add_argument("--import-existing", action="store_true",
                        help="import GitHub PR batches, default UUID batches and github-audit/verified-events.jsonl; no GitHub calls")
    args = parser.parse_args(argv)
    try:
        coldfix.require(not (args.import_existing and args.migrate_codex_labels), "choose one import or migration mode")
        coldfix.require(args.oss_allowlist or not args.dry_run, "--dry-run needs --oss-allowlist")
        if args.oss_allowlist:
            coldfix.require(not (args.repo or args.pr or args.import_existing or args.migrate_codex_labels),
                            "--oss-allowlist does not combine with --repo, --pr, --import-existing or --migrate-codex-labels")
            since = utc(args.since) if args.since else (
                datetime.now(timezone.utc) - timedelta(days=OSS_DEFAULT_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
            results, events = oss_gather(oss_repos(args.oss_allowlist), since)
            summary = dict(since=since, dry_run=args.dry_run, repos=results, records=len(events))
            if not args.dry_run:
                root = private_store(args.store)
                output = args.output or f"github-oss-{uuid.uuid4().hex}.jsonl"
                coldfix.require(re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*\.jsonl", output), "invalid JSONL filename")
                output = coldfix.ordinary_path(root / output)
                coldfix.require(not output.exists(), "output file already exists; choose a new --output name")
                coldfix.load_store(root)
                root.mkdir(parents=True, mode=0o700, exist_ok=True)
                coldfix.atomic_create(output, "".join(json.dumps(row, ensure_ascii=True, sort_keys=True) + "\n"
                                                      for row in events))
                summary.update(import_batch(root, output))
            print(json.dumps(summary, sort_keys=True))
            return 0 if all(row["status"] == "ok" for row in results) else 3
        if args.import_existing:
            result = import_existing(private_store(args.store))
            if result is None:
                return 2
            print(json.dumps(result, sort_keys=True))
            return 0
        coldfix.require(args.repo and REPO.fullmatch(args.repo), "invalid repository")
        if args.migrate_codex_labels:
            coldfix.require(args.pr is None and args.since is None and args.output is None,
                            "migration does not accept --pr, --since or --output")
            result = migrate_codex_labels(private_store(args.store), GitHub(args.repo))
            print(json.dumps(result, sort_keys=True))
            return 0
        if args.output is None:
            args.output = f"github-events-{uuid.uuid4().hex}.jsonl"
        if args.since is not None:
            timestamp(args.since)
        coldfix.require(args.pr is None or all(number > 0 for number in args.pr), "invalid PR number")
        root = private_store(args.store)
        coldfix.require(re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*\.jsonl", args.output), "invalid JSONL filename")
        output = coldfix.ordinary_path(root / args.output)
        replace = output.is_file() and output.stat().st_size == 0
        if output.exists() and not replace:
            print("github_sources: output file already exists; choose a new --output name; nothing changed", file=sys.stderr)
            return 2
        github = GitHub(args.repo)
        events = collect(github, args.since, args.pr)
        content = "".join(json.dumps(row, ensure_ascii=True, sort_keys=True) + "\n" for row in events)
        coldfix.load_store(root)
        root.mkdir(parents=True, mode=0o700, exist_ok=True)
        coldfix.atomic_create(output, content, replace=replace)
        imported = import_batch(root, output)
        counts = Counter(row["kind"] for row in events)
        print(json.dumps(dict(events=len(events), requests=github.requests,
                              imported_records=imported["imported_records"],
                              skipped_duplicates=imported["skipped_duplicates"],
                              **{kind: counts[kind] for kind in ("revert", "issue", "review", "ci")}), sort_keys=True))
        return 0
    except (coldfix.ColdfixError, KeyError, TypeError, ValueError, OverflowError, RecursionError):
        print("github_sources: invalid input or incomplete GitHub data; no batch published", file=sys.stderr)
    except (OSError, UnicodeError):
        print("github_sources: local I/O failed; no batch published", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
