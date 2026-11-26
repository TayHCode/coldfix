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
import tempfile
import time
import unicodedata
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
OSS_PR_CHECKS = """query($owner:String!,$name:String!,$cursor:String,$first:Int!,$commits:Int!,$contexts:Int!) {
  rateLimit { cost remaining resetAt }
  repository(owner:$owner,name:$name) {
    isPrivate
    licenseInfo { spdxId }
    pullRequests(first:$first,states:MERGED,orderBy:{field:UPDATED_AT,direction:DESC},after:$cursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title url mergedAt
        commits(first:$commits) {
          nodes {
            commit {
              oid message committedDate
              statusCheckRollup {
                state
                contexts(first:$contexts) {
                  nodes {
                    __typename
                    ... on CheckRun { name conclusion status title summary detailsUrl }
                    ... on StatusContext { context state description targetUrl }
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}"""
OSS_PERMISSIVE_LICENSES = frozenset({
    "MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "0BSD",
    "PSF-2.0", "Unlicense", "CC0-1.0", "Zlib", "BSL-1.0",
})
OSS_FAILED_CONCLUSIONS = frozenset({"FAILURE", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE", "ERROR"})
OSS_DOCS_CHORE_PATH = re.compile(
    r"(?i)(?:^|/)(?:docs?/|readme(?:\.[^/]+)?$|changelog(?:\.[^/]+)?$|license(?:\.[^/]+)?$|"
    r"authors(?:\.[^/]+)?$|codeowners$|\.gitignore$|\.gitattributes$|\.editorconfig$|"
    r"\.prettierrc(?:\.[^/]+)?$|\.prettierignore$|\.eslintignore$)|"
    r"\.(?:md|markdown|rst|txt|adoc)$")
OSS_DOCS_CHORE_SUBJECT = re.compile(
    r"(?i)^(?:(?:docs|chore|style|format|fmt|typo)(?:\([^)]*\))?!?:\s|"
    r"(?:re)?format(?:ting)?\b|run\s+(?:prettier|black|rustfmt|gofmt|ruff\s+format)\b|fix\s+typos?\b)")
OSS_EXTRA_SECRET = re.compile(
    r"\b(?:hf_|npm_|pypi-|sk-ant-)[A-Za-z0-9_-]{12,}|\bBearer\s+[A-Za-z0-9._~+/=-]{6,}", re.I)
OSS_MAX_DIFF_CHARS = 32768
OSS_MIN_REMAINING = 1000
OSS_SCHEMA_VERSION = 1
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
            need(payload["query"] in (PULL, PULLS, HISTORY, PR_COMMITS, CLOSING, OSS_PR_CHECKS))
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
    """Read-only GET/GraphQL client for public repositories that stops before the rate limit is spent."""

    def __init__(self, repo, *, min_remaining=0, sleep_on_limit=False, budget=None):
        super().__init__(repo)
        self.remaining = None
        self.reset = None
        self.min_remaining = min_remaining
        self.sleep_on_limit = sleep_on_limit
        self.budget = budget if budget is not None else dict(
            calls=0, total_cost=0, min_remaining=None, last_remaining=None, last_reset_at=None, stopped_early=False)

    def refresh_budget(self):
        core = super().api(RATE_LIMIT_ENDPOINT)["resources"]["core"]
        need(type(core["remaining"]) is int and core["remaining"] >= 0 and type(core["reset"]) is int)
        self.remaining, self.reset = core["remaining"], core["reset"]

    def _check_graphql_budget(self):
        remaining = self.budget.get("last_remaining")
        if remaining is None or remaining >= self.min_remaining:
            return
        reset_at = self.budget.get("last_reset_at")
        if self.sleep_on_limit and reset_at:
            delay = max(0.0, (timestamp(reset_at) - datetime.now(timezone.utc)).total_seconds()) + 2.0
            time.sleep(delay)
            self.budget["last_remaining"] = None
            return
        self.budget["stopped_early"] = True
        raise RateLimited(f"graphql rate limit remaining {remaining} < {self.min_remaining} until {reset_at}")

    def api(self, endpoint, payload=None):
        if payload is not None:
            need(endpoint == "graphql" and payload.get("query") == OSS_PR_CHECKS)
            self._check_graphql_budget()
            value = super().api(endpoint, payload)
            rl = (value.get("data") or {}).get("rateLimit")
            need(isinstance(rl, dict) and type(rl.get("cost")) is int and rl["cost"] >= 0
                 and type(rl.get("remaining")) is int and rl["remaining"] >= 0
                 and isinstance(rl.get("resetAt"), str) and rl["resetAt"])
            timestamp(rl["resetAt"])
            self.budget["calls"] += 1
            self.budget["total_cost"] += rl["cost"]
            self.budget["last_remaining"] = rl["remaining"]
            self.budget["last_reset_at"] = rl["resetAt"]
            prev_min = self.budget.get("min_remaining")
            self.budget["min_remaining"] = rl["remaining"] if prev_min is None else min(prev_min, rl["remaining"])
            return value
        if self.remaining is None:
            self.refresh_budget()
        if self.remaining <= 0:
            raise RateLimited(f"rate limit exhausted until {self.reset}")
        self.remaining -= 1
        return super().api(endpoint)


def oss_repos(path):
    text = coldfix.read_text(coldfix.ordinary_path(path))
    stripped = text.lstrip()
    names = []
    if stripped.startswith(("{", "[")):
        try:
            parsed = json.loads(text, object_pairs_hook=coldfix.unique_object)
        except (ValueError, RecursionError):
            raise coldfix.ColdfixError("invalid JSON repository allowlist") from None
        rows = parsed["repos"] if isinstance(parsed, dict) and isinstance(parsed.get("repos"), list) else parsed
        coldfix.require(isinstance(rows, list), "invalid JSON repository allowlist")
        for row in rows:
            name = row["repo"] if isinstance(row, dict) else row
            coldfix.require(isinstance(name, str) and REPO.fullmatch(name), "invalid repository in allowlist")
            if isinstance(row, dict) and "license" in row:
                lic = row["license"]
                if not isinstance(lic, str) or lic not in OSS_PERMISSIVE_LICENSES:
                    continue
            names.append(name)
    else:
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                coldfix.require(REPO.fullmatch(line), "invalid repository in allowlist")
                names.append(line)
    coldfix.require(names, "allowlist names no repositories")
    return list(dict.fromkeys(names))


def utc(value):
    return timestamp(value).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _commit_checks(commit_node):
    rollup = commit_node.get("statusCheckRollup")
    if not isinstance(rollup, dict):
        return None, [], [], False
    state = rollup.get("state")
    contexts = ((rollup.get("contexts") or {}).get("nodes")) or []
    failed, passed = [], set()
    for ctx in contexts:
        if not isinstance(ctx, dict):
            continue
        kind = ctx.get("__typename")
        if kind == "CheckRun" or ("name" in ctx and "conclusion" in ctx):
            name = (ctx.get("name") or "").strip()
            conclusion = (ctx.get("conclusion") or "").upper()
            if not name:
                continue
            if conclusion in OSS_FAILED_CONCLUSIONS:
                failed.append(dict(
                    name=name[:200],
                    conclusion=conclusion,
                    title=(ctx.get("title") or "")[:300],
                    summary=(ctx.get("summary") or "")[:2000],
                    url=ctx.get("detailsUrl") or "",
                ))
            elif conclusion == "SUCCESS":
                passed.add(name)
        elif kind == "StatusContext" or "context" in ctx:
            name = (ctx.get("context") or "").strip()
            ctx_state = (ctx.get("state") or "").upper()
            if not name:
                continue
            if ctx_state in OSS_FAILED_CONCLUSIONS:
                failed.append(dict(
                    name=name[:200],
                    conclusion=ctx_state,
                    title=(ctx.get("description") or "")[:300],
                    summary="",
                    url=ctx.get("targetUrl") or "",
                ))
            elif ctx_state == "SUCCESS":
                passed.add(name)
    same_commit_flaky = any(row["name"] in passed for row in failed)
    return state, failed, sorted(passed), same_commit_flaky


def _oss_collect_ci_pairs(github, since=None, *, max_prs=100, pr_page_size=25, max_commits=25, max_contexts=50,
                          prior=None):
    cutoff = timestamp(since) if since else None
    cursor = prior.get("end_cursor") if isinstance(prior, dict) else None
    cursors = {cursor} if cursor else set()
    prs_scanned = int(prior.get("prs_scanned", 0)) if isinstance(prior, dict) else 0
    candidates = list(prior.get("candidates") or []) if isinstance(prior, dict) else []
    seen_keys = {
        (c["pr_number"], c["before_sha"], c["after_sha"], c["stage1_kind"])
        for c in candidates if isinstance(c, dict)
    }
    spdx = prior.get("license") if isinstance(prior, dict) else None
    has_next_page = False
    status = "ok"
    page_size = pr_page_size
    while max_prs is None or prs_scanned < max_prs:
        if github.budget.get("last_remaining") is not None and github.budget["last_remaining"] < github.min_remaining and not github.sleep_on_limit:
            github.budget["stopped_early"] = True
            status = "rate_limited"
            has_next_page = True
            break
        batch = page_size if max_prs is None else min(page_size, max_prs - prs_scanned)
        value = None
        while True:
            variables = dict(owner=github.owner, name=github.name, cursor=cursor,
                             first=batch, commits=max_commits, contexts=max_contexts)
            try:
                value = github.api("graphql", dict(query=OSS_PR_CHECKS, variables=variables))
                break
            except RateLimited:
                raise
            except coldfix.ColdfixError:
                if batch <= 6:
                    raise
                batch = max(6, batch // 2)
                page_size = batch
        repo_data = (value.get("data") or {}).get("repository")
        need(isinstance(repo_data, dict))
        if repo_data.get("isPrivate") is not False:
            return dict(repo=github.repo, license=None, status="skipped_private",
                        prs_scanned=0, candidates=[], has_next_page=False, end_cursor=None,
                        rate_limit=dict(github.budget))
        lic_info = repo_data.get("licenseInfo")
        spdx = (lic_info.get("spdxId") if isinstance(lic_info, dict) else None) or ""
        if spdx not in OSS_PERMISSIVE_LICENSES:
            return dict(repo=github.repo, license=spdx or None, status="skipped_no_license",
                        prs_scanned=0, candidates=[], has_next_page=False, end_cursor=None,
                        rate_limit=dict(github.budget))
        pulls = repo_data.get("pullRequests")
        need(isinstance(pulls, dict) and isinstance(pulls.get("nodes"), list))
        nodes = pulls["nodes"]
        if not nodes:
            has_next_page = False
            break
        stop_window = False
        for pull in nodes:
            if not isinstance(pull, dict):
                continue
            prs_scanned += 1
            merged_at = pull.get("mergedAt")
            if not merged_at:
                continue
            if cutoff is not None and timestamp(merged_at) < cutoff:
                stop_window = True
                continue
            number = pull.get("number")
            title = pull.get("title") or ""
            pr_url = pull.get("url") or ""
            commit_nodes = [row["commit"] for row in ((pull.get("commits") or {}).get("nodes") or [])
                            if isinstance(row, dict) and isinstance(row.get("commit"), dict)]
            parsed = []
            for commit_node in commit_nodes:
                oid = commit_node.get("oid") or ""
                if not SHA.fullmatch(oid):
                    continue
                state, failed, passed, same_flaky = _commit_checks(commit_node)
                parsed.append(dict(
                    oid=oid,
                    message=commit_node.get("message") or "",
                    committed_date=commit_node.get("committedDate") or "",
                    rollup_state=state,
                    failed=failed,
                    passed=passed,
                    same_flaky=same_flaky,
                ))
            for idx, cur in enumerate(parsed):
                if cur["same_flaky"]:
                    key = (number, cur["oid"], cur["oid"], "flaky_same_commit")
                    if key not in seen_keys:
                        seen_keys.add(key)
                        flaky_failed = [row for row in cur["failed"] if row["name"] in cur["passed"]]
                        candidates.append(dict(
                            stage1_kind="flaky_same_commit",
                            pr_number=number, pr_url=pr_url, pr_title=title, merged_at=merged_at,
                            before_sha=cur["oid"], after_sha=cur["oid"],
                            before_message=cur["message"], after_message=cur["message"],
                            failed_checks=flaky_failed, after_rollup_state="SUCCESS", after_failed_checks=[],
                        ))
                if idx + 1 >= len(parsed) or not cur["failed"]:
                    continue
                nxt = parsed[idx + 1]
                nxt_has_failure = bool(nxt["failed"]) or nxt["rollup_state"] in ("FAILURE", "ERROR")
                nxt_green = (not nxt_has_failure) and (nxt["rollup_state"] == "SUCCESS" or bool(nxt["passed"]))
                if nxt_green:
                    key = (number, cur["oid"], nxt["oid"], "red_to_green")
                    if key not in seen_keys:
                        seen_keys.add(key)
                        candidates.append(dict(
                            stage1_kind="red_to_green",
                            pr_number=number, pr_url=pr_url, pr_title=title, merged_at=merged_at,
                            before_sha=cur["oid"], after_sha=nxt["oid"],
                            before_message=cur["message"], after_message=nxt["message"],
                            failed_checks=cur["failed"], after_rollup_state="SUCCESS", after_failed_checks=[],
                        ))
                elif nxt["failed"]:
                    key = (number, cur["oid"], nxt["oid"], "still_red")
                    if key not in seen_keys:
                        seen_keys.add(key)
                        candidates.append(dict(
                            stage1_kind="still_red",
                            pr_number=number, pr_url=pr_url, pr_title=title, merged_at=merged_at,
                            before_sha=cur["oid"], after_sha=nxt["oid"],
                            before_message=cur["message"], after_message=nxt["message"],
                            failed_checks=cur["failed"],
                            after_rollup_state=nxt["rollup_state"] or "FAILURE",
                            after_failed_checks=nxt["failed"],
                        ))
        info = pulls.get("pageInfo") or {}
        has_next_page = bool(info.get("hasNextPage")) and not stop_window
        next_cursor = info.get("endCursor")
        if not has_next_page or not isinstance(next_cursor, str) or not next_cursor or next_cursor in cursors:
            has_next_page = False
            break
        cursors.add(next_cursor)
        cursor = next_cursor
    return dict(repo=github.repo, license=spdx, status=status, prs_scanned=prs_scanned,
                candidates=candidates, has_next_page=has_next_page, end_cursor=cursor,
                rate_limit=dict(github.budget))


def _load_privacy_policy(path):
    if path is None:
        return ([], [])
    try:
        from training import dataset as training_dataset
        return training_dataset.load_policy(Path(path))
    except ImportError:
        policy = json.loads(Path(path).read_bytes(), object_pairs_hook=coldfix.unique_object)
        terms = [unicodedata.normalize("NFKC", term.strip())
                 for field in ("owner_names", "private_repos", "private_hosts")
                 for term in policy.get(field, []) if isinstance(term, str) and term.strip()]
        patterns = [re.compile(pattern, re.I) for pattern in policy.get("patterns", [])
                    if isinstance(pattern, str) and pattern]
        return terms, patterns


def _privacy_blocked(text, policy=None):
    terms, patterns = policy or ([], [])
    normalized = unicodedata.normalize("NFKC", text)
    if (coldfix.privacy_counts(normalized, {"private": terms} if terms else None, filename_exempt=True)
            or OSS_EXTRA_SECRET.search(normalized)
            or re.search(r"(?i)\b[\w.-]+-private\b|<\|[^>]+\|>", normalized)
            or any(pattern.search(normalized) for pattern in patterns)):
        return True
    try:
        from training import dataset as training_dataset
        import regression_adapter
        return bool(training_dataset.screened(regression_adapter.screenable(normalized), (terms, patterns)))
    except ImportError:
        return False


def _is_docs_format_chore(changed_files, diff_w_empty, after_message):
    if diff_w_empty:
        return True
    if changed_files and all(OSS_DOCS_CHORE_PATH.search(path) for path in changed_files):
        return True
    subject = (after_message or "").strip().splitlines()[0] if (after_message or "").strip() else ""
    return bool(OSS_DOCS_CHORE_SUBJECT.search(subject))


def _git_anonymous(clone, args, env, timeout=60):
    command = ["git", "-C", str(clone), "-c", "core.hooksPath=" + os.devnull, "-c", "credential.helper=", *args]
    return subprocess.run(command, capture_output=True, text=True, errors="replace", env=env, timeout=timeout, check=False)


def _fetch_shas_blobless(url, shas, clone, timeout=180):
    import oss_replay
    import ci_fix_learning as ci
    unique = list(dict.fromkeys(shas))
    if not unique:
        return None, set()
    env = None
    fetched = set()
    chunk_size = 50
    for start in range(0, len(unique), chunk_size):
        chunk = unique[start:start + chunk_size]
        if env is None:
            try:
                env = oss_replay.anonymous_clone(url, chunk, clone, timeout, blobless=True)
                fetched.update(chunk)
                continue
            except ci.Skipped:
                env = dict(PATH=os.defpath, HOME=str(clone.parent), LANG="C.UTF-8", LC_ALL="C.UTF-8",
                           GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                           GIT_ASKPASS="/bin/false", SSH_ASKPASS="/bin/false")
                if not clone.exists():
                    subprocess.run(["git", "-c", "core.hooksPath=" + os.devnull, "-c", "credential.helper=",
                                    "init", "--quiet", "--bare", str(clone)],
                                   capture_output=True, env=env, timeout=timeout, check=False)
                    _git_anonymous(clone, ["remote", "add", "origin", url], env, timeout)
                for sha in chunk:
                    res = _git_anonymous(clone, ["fetch", "--quiet", "--filter=blob:none", "--depth=1",
                                                 "--no-tags", "origin", sha], env, timeout)
                    if res.returncode == 0:
                        fetched.add(sha)
        else:
            res = _git_anonymous(clone, ["fetch", "--quiet", "--filter=blob:none", "--depth=1",
                                         "--no-tags", "origin", *chunk], env, timeout)
            if res.returncode == 0:
                fetched.update(chunk)
            else:
                for sha in chunk:
                    one = _git_anonymous(clone, ["fetch", "--quiet", "--filter=blob:none", "--depth=1",
                                                 "--no-tags", "origin", sha], env, timeout)
                    if one.returncode == 0:
                        fetched.add(sha)
    return env, fetched


def _materialize_repo_pairs(stage1, clone_work, *, repo_url=None, privacy_policy=None, timeout=180):
    repo = stage1["repo"]
    spdx = stage1.get("license")
    candidates = stage1.get("candidates") or []
    if not spdx or spdx not in OSS_PERMISSIVE_LICENSES or not candidates:
        return [], dict(privacy_dropped=0, clone_errors=0)
    url = repo_url or f"https://github.com/{repo}.git"
    needed_shas = []
    for cand in candidates:
        if cand["stage1_kind"] != "flaky_same_commit":
            needed_shas.extend((cand["before_sha"], cand["after_sha"]))
    pairs = []
    privacy_dropped = 0
    clone_errors = 0
    clone_root = Path(clone_work) if clone_work else Path(tempfile.gettempdir())
    clone_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="coldfix-oss-s2-", dir=str(clone_root)) as tmp:
        clone = Path(tmp) / "repo.git"
        env, fetched = _fetch_shas_blobless(url, needed_shas, clone, timeout=timeout)
        for cand in candidates:
            kind = cand["stage1_kind"]
            before_sha, after_sha = cand["before_sha"], cand["after_sha"]
            before_msg, after_msg = cand.get("before_message") or "", cand.get("after_message") or ""
            if kind == "flaky_same_commit":
                label, negative_kind = "negative", "flaky"
                changed_files, diff_text, diff_truncated = [], "", False
            else:
                if env is None or before_sha not in fetched or after_sha not in fetched:
                    clone_errors += 1
                    continue
                if not before_msg:
                    res_msg = _git_anonymous(clone, ["log", "-n", "1", "--format=%B", before_sha], env, timeout=30)
                    if res_msg.returncode == 0:
                        before_msg = res_msg.stdout.strip()
                if not after_msg:
                    res_msg = _git_anonymous(clone, ["log", "-n", "1", "--format=%B", after_sha], env, timeout=30)
                    if res_msg.returncode == 0:
                        after_msg = res_msg.stdout.strip()
                names_res = _git_anonymous(clone, ["diff", "--name-only", before_sha, after_sha], env, timeout=60)
                if names_res.returncode != 0:
                    clone_errors += 1
                    continue
                changed_files = [line.strip() for line in names_res.stdout.splitlines() if line.strip()]
                if not changed_files:
                    if kind == "red_to_green":
                        label, negative_kind = "negative", "flaky"
                        diff_text, diff_truncated = "", False
                    else:
                        continue
                else:
                    diff_res = _git_anonymous(clone, ["diff", "--no-color", "--no-ext-diff", before_sha, after_sha], env, timeout=90)
                    if diff_res.returncode != 0:
                        clone_errors += 1
                        continue
                    raw_diff = diff_res.stdout
                    diff_truncated = len(raw_diff) > OSS_MAX_DIFF_CHARS
                    diff_text = raw_diff[:OSS_MAX_DIFF_CHARS]
                    if kind == "still_red":
                        label, negative_kind = "negative", "still_red"
                    else:
                        w_res = _git_anonymous(clone, ["diff", "-w", "--no-color", "--no-ext-diff", before_sha, after_sha], env, timeout=90)
                        diff_w_empty = (w_res.returncode == 0 and not w_res.stdout.strip())
                        if _is_docs_format_chore(changed_files, diff_w_empty, after_msg):
                            label, negative_kind = "negative", "docs_format_chore"
                        else:
                            label, negative_kind = "positive", None
            check_key = cand["failed_checks"][0]["name"] if cand.get("failed_checks") else ""
            pair_id = coldfix.digest(f"{repo}|{cand['pr_number']}|{before_sha}|{after_sha}|{label}|{negative_kind or ''}|{check_key}")[:32]
            row = dict(
                schema_version=OSS_SCHEMA_VERSION,
                pair_id=pair_id,
                repo=repo,
                license=spdx,
                pr_number=cand["pr_number"],
                pr_url=cand["pr_url"],
                pr_title=cand["pr_title"],
                merged_at=cand["merged_at"],
                label=label,
                negative_kind=negative_kind,
                before_sha=before_sha,
                after_sha=after_sha,
                before_message=before_msg[:2000],
                after_message=after_msg[:2000],
                failed_checks=cand["failed_checks"],
                after_rollup_state=cand["after_rollup_state"],
                after_failed_checks=cand["after_failed_checks"],
                changed_files=changed_files[:200],
                diff=diff_text,
                diff_truncated=diff_truncated,
                rule=None,
            )
            screen_blob = "\n".join([
                row["repo"], row["pr_url"], row["pr_title"], row["before_message"], row["after_message"],
                "\n".join(row["changed_files"]),
                "\n".join(f"{c['name']}\n{c['title']}\n{c['summary']}" for c in row["failed_checks"]),
                "\n".join(f"{c['name']}\n{c['title']}\n{c['summary']}" for c in row["after_failed_checks"]),
                row["diff"],
            ])
            if _privacy_blocked(screen_blob, privacy_policy):
                privacy_dropped += 1
                continue
            pairs.append(row)
    return pairs, dict(privacy_dropped=privacy_dropped, clone_errors=clone_errors)


def _repo_slug_file(repo):
    return repo.replace("/", "__")


def _write_oss_outputs(out_dir, batch_files, stats_by_repo, receipt, *, write_merged=True):
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    all_rows = []
    seen_ids = set()
    pairs_per_repo = {}
    licenses = {}
    positive = 0
    neg_breakdown = Counter()
    for path in sorted(batch_files):
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").split("\n"):
            if not line.strip():
                continue
            row = json.loads(line)
            if row["pair_id"] in seen_ids:
                continue
            seen_ids.add(row["pair_id"])
            if write_merged:
                all_rows.append(row)
            pairs_per_repo[row["repo"]] = pairs_per_repo.get(row["repo"], 0) + 1
            licenses[row["repo"]] = row["license"]
            if row["label"] == "positive":
                positive += 1
            else:
                neg_breakdown[row.get("negative_kind") or "other"] += 1
    if write_merged:
        coldfix.atomic_create(
            out_dir / "pairs.jsonl",
            "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in all_rows),
            replace=True, mode=0o644)
    total = len(seen_ids)
    summary = dict(
        schema_version=OSS_SCHEMA_VERSION,
        updated_at=now_iso,
        total_pairs=total,
        positive=positive,
        negative=total - positive,
        negative_breakdown=dict(sorted(neg_breakdown.items())),
        repos_with_pairs=len(pairs_per_repo),
        pairs_per_repo=dict(sorted(pairs_per_repo.items(), key=lambda kv: (-kv[1], kv[0]))),
        licenses=dict(sorted(licenses.items())),
        repo_status=stats_by_repo,
        privacy_dropped=sum(v.get("privacy_dropped", 0) for v in stats_by_repo.values()),
        clone_errors=sum(v.get("clone_errors", 0) for v in stats_by_repo.values()),
    )
    coldfix.atomic_create(out_dir / "summary.json", coldfix.encoded(summary), replace=True, mode=0o644)
    receipt_out = dict(receipt, updated_at=now_iso)
    coldfix.atomic_create(out_dir / "rate_limit_receipt.json", coldfix.encoded(receipt_out), replace=True, mode=0o644)
    return summary, receipt_out


def oss_collect(github, since=None, *, mode="events", max_prs=100, pr_page_size=25, max_commits=25, max_contexts=50,
                prior=None):
    """Collect OSS events (default) or Stage-1 GraphQL CI-pair candidates (`mode='ci_pairs'`).

    In default `'events'` mode, returns failed runs and fix or revert commits since `since` (UTC) as `(events, complete)`.
    In `'ci_pairs'` mode, runs batched GraphQL queries over merged PR commits and statusCheckRollup/CheckRun."""
    if mode == "ci_pairs":
        return _oss_collect_ci_pairs(github, since, max_prs=max_prs, pr_page_size=pr_page_size,
                                     max_commits=max_commits, max_contexts=max_contexts, prior=prior)
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


def oss_gather(repos, since=None, *, mode="events", output_dir=None, stage="both", max_prs=100,
               pr_page_size=25, max_commits=25, max_contexts=50, min_remaining=OSS_MIN_REMAINING,
               sleep_on_limit=False, clone_dir=None, privacy_policy=None, target_pairs=0,
               repo_url_fn=None, timeout=180):
    """Gather each repository independently; one failing repository never hides the others."""
    if mode == "ci_pairs":
        coldfix.require(output_dir is not None, "output_dir is required for ci_pairs mode")
        coldfix.require(stage in ("stage1", "stage2", "both"), "invalid stage")
        out = Path(output_dir)
        ckpt_dir = out / "checkpoints"
        batch_dir = out / "batches"
        ckpt_dir.mkdir(parents=True, exist_ok=True)
        batch_dir.mkdir(parents=True, exist_ok=True)
        receipt_path = out / "rate_limit_receipt.json"
        if receipt_path.is_file():
            prior = json.loads(receipt_path.read_text(encoding="utf-8"))
            budget = dict(
                calls=prior.get("calls", 0),
                total_cost=prior.get("total_cost", 0),
                min_remaining=prior.get("min_remaining"),
                last_remaining=prior.get("last_remaining"),
                last_reset_at=prior.get("last_reset_at"),
                stopped_early=False,
            )
            if budget["last_reset_at"] and timestamp(budget["last_reset_at"]) <= datetime.now(timezone.utc):
                budget["last_remaining"] = None
        else:
            budget = dict(calls=0, total_cost=0, min_remaining=None, last_remaining=None,
                          last_reset_at=None, stopped_early=False)
        stats_path = out / "summary.json"
        stats_by_repo = (json.loads(stats_path.read_text(encoding="utf-8")).get("repo_status") or {}) if stats_path.is_file() else {}
        policy = _load_privacy_policy(privacy_policy) if isinstance(privacy_policy, (str, Path)) else privacy_policy
        running_pairs = sum(v.get("pairs", 0) for v in stats_by_repo.values())
        for idx, repo in enumerate(repos, 1):
            if target_pairs and running_pairs >= target_pairs:
                break
            slug = _repo_slug_file(repo)
            ckpt_file = ckpt_dir / f"{slug}.stage1.json"
            batch_file = batch_dir / f"{slug}.pairs.jsonl"
            stage1 = json.loads(ckpt_file.read_text(encoding="utf-8")) if ckpt_file.is_file() else None
            needs_deeper = (
                stage1 is not None
                and stage1.get("status") == "ok"
                and stage1.get("has_next_page")
                and stage1.get("end_cursor")
                and max_prs is not None
                and int(stage1.get("prs_scanned", 0)) < max_prs
            )
            stage1_updated = False
            if stage in ("stage1", "both") and (
                stage1 is None
                or stage1.get("status") not in ("ok", "skipped_no_license", "skipped_private")
                or needs_deeper
            ):
                if budget.get("stopped_early"):
                    stats_by_repo[repo] = dict(status="skipped_rate_limited", pairs=0)
                    continue
                github = PublicGitHub(repo, min_remaining=min_remaining, sleep_on_limit=sleep_on_limit, budget=budget)
                try:
                    stage1 = oss_collect(github, since, mode="ci_pairs", max_prs=max_prs,
                                         pr_page_size=pr_page_size, max_commits=max_commits, max_contexts=max_contexts,
                                         prior=stage1 if needs_deeper else None)
                except RateLimited:
                    budget["stopped_early"] = True
                    stats_by_repo[repo] = dict(status="rate_limited", pairs=0)
                    _write_oss_outputs(out, list(batch_dir.glob("*.pairs.jsonl")), stats_by_repo, budget, write_merged=False)
                    continue
                except (coldfix.ColdfixError, KeyError, TypeError, ValueError):
                    stats_by_repo[repo] = dict(status="failed", pairs=0)
                    continue
                coldfix.atomic_create(ckpt_file, coldfix.encoded(stage1), replace=True, mode=0o644)
                stage1_updated = True
                stats_by_repo[repo] = dict(
                    status=stage1["status"], license=stage1.get("license"),
                    prs_scanned=stage1.get("prs_scanned", 0), candidates=len(stage1.get("candidates") or []),
                    pairs=stats_by_repo.get(repo, {}).get("pairs", 0),
                )
            if stage in ("stage2", "both") and stage1 is not None and (not batch_file.is_file() or stage1_updated):
                if stage1.get("status") in ("skipped_no_license", "skipped_private"):
                    stats_by_repo[repo] = dict(status=stage1["status"], license=stage1.get("license"), pairs=0)
                else:
                    url = repo_url_fn(repo) if repo_url_fn else None
                    pairs, meta = _materialize_repo_pairs(stage1, clone_dir, repo_url=url,
                                                          privacy_policy=policy, timeout=timeout)
                    coldfix.atomic_create(
                        batch_file,
                        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in pairs),
                        replace=True, mode=0o644)
                    stats_by_repo[repo] = dict(
                        status=stage1["status"], license=stage1.get("license"),
                        prs_scanned=stage1.get("prs_scanned", 0),
                        candidates=len(stage1.get("candidates") or []),
                        pairs=len(pairs),
                        positive=sum(row["label"] == "positive" for row in pairs),
                        negative=sum(row["label"] == "negative" for row in pairs),
                        **meta,
                    )
                    running_pairs = sum(v.get("pairs", 0) for v in stats_by_repo.values())
            _write_oss_outputs(out, list(batch_dir.glob("*.pairs.jsonl")), stats_by_repo, budget,
                               write_merged=(idx % 5 == 0))
            st = stats_by_repo.get(repo, {})
            print(f"oss-ci-pairs: [{idx}/{len(repos)}] {repo} status={st.get('status')} "
                  f"pairs={st.get('pairs', 0)} total={running_pairs} "
                  f"remaining={budget.get('last_remaining')}", file=sys.stderr, flush=True)
        return _write_oss_outputs(out, list(batch_dir.glob("*.pairs.jsonl")), stats_by_repo, budget, write_merged=True)
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
    parser.add_argument("--oss-allowlist", help="file of public OWNER/REPO names (one per line or JSON catalog); "
                        "gathers failed CI runs/commits or OSS CI pairs read-only (default window: last %d days)" % OSS_DEFAULT_DAYS)
    parser.add_argument("--oss-ci-pairs", action="store_true",
                        help="with --oss-allowlist: mine two-stage OSS CI pairs (Stage 1 GraphQL, Stage 2 blobless local git)")
    parser.add_argument("--output-dir", help="with --oss-ci-pairs: directory for checkpoints, batches, summary and rate-limit receipt")
    parser.add_argument("--stage", choices=("stage1", "stage2", "both"), default="both",
                        help="with --oss-ci-pairs: run stage1 (GraphQL only), stage2 (local git only) or both")
    parser.add_argument("--max-prs", type=int, default=100,
                        help="with --oss-ci-pairs: max merged PRs scanned per repo (default 100)")
    parser.add_argument("--min-remaining", type=int, default=OSS_MIN_REMAINING,
                        help="with --oss-ci-pairs: stop or sleep when GraphQL remaining drops below this threshold (default %d)" % OSS_MIN_REMAINING)
    parser.add_argument("--sleep-on-limit", action="store_true",
                        help="with --oss-ci-pairs: sleep until rateLimit.resetAt instead of stopping")
    parser.add_argument("--privacy-policy", help="with --oss-ci-pairs: JSON privacy policy for dataset screening")
    parser.add_argument("--clone-dir", help="with --oss-ci-pairs: scratch directory for Stage 2 blobless git clones")
    parser.add_argument("--target-pairs", type=int, default=0,
                        help="with --oss-ci-pairs: stop once at least this many total pairs are materialized (0 = all repos)")
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
        coldfix.require(args.oss_allowlist or not args.oss_ci_pairs, "--oss-ci-pairs needs --oss-allowlist")
        if args.oss_allowlist:
            coldfix.require(not (args.repo or args.pr or args.import_existing or args.migrate_codex_labels),
                            "--oss-allowlist does not combine with --repo, --pr, --import-existing or --migrate-codex-labels")
            if args.oss_ci_pairs:
                coldfix.require(args.output_dir and args.max_prs > 0 and args.min_remaining >= 0 and args.target_pairs >= 0,
                                "--oss-ci-pairs requires --output-dir and valid numeric limits")
                since = utc(args.since) if args.since else None
                summary, receipt = oss_gather(
                    oss_repos(args.oss_allowlist), since, mode="ci_pairs", output_dir=args.output_dir,
                    stage=args.stage, max_prs=args.max_prs, min_remaining=args.min_remaining,
                    sleep_on_limit=args.sleep_on_limit, clone_dir=args.clone_dir,
                    privacy_policy=args.privacy_policy, target_pairs=args.target_pairs)
                print(json.dumps(dict(summary=summary, rate_limit_receipt=receipt), sort_keys=True))
                return 0 if not receipt.get("stopped_early") else 3
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
