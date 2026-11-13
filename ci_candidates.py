#!/usr/bin/env python3
"""List public repositories whose CI workflow ci_fix_learning can replay. Read-only gh GETs."""

import argparse
import base64
from collections import Counter
import json
import re
import sys
import time

import ci_fix_learning as ci
import coldfix as c
import failure_ingest
from failure_ingest import SOURCE_ERRORS

QUERIES = ('path:.github/workflows extension:yml size:<500 "unittest"',
           'path:.github/workflows extension:yml size:<500 "pytest"',
           'path:.github/workflows extension:yml size:<500 "mypy"')
WORKFLOW = re.compile(r"[A-Za-z0-9_.-]+\.ya?ml")


def fitting_job(content):
    try:
        for names, block in ci.workflow_jobs(content):
            expanded = "\n".join(block)
            steps = [ci.scalar(m) for m in re.findall(r"^      (?:- |  )name:\s*(.+)$", expanded, re.M)]
            steps += ["Run " + ci.scalar(m) for m in re.findall(r"^      (?:- |  )run:\s*([^|>\s].*)$", expanded, re.M)]
            for body in re.findall(r"^      (?:- |  )run:\s*\|-?[ ]*\n((?:[ ]{10}.*\n|[ ]*\n)+)", expanded + "\n", re.M):
                steps.append("Run " + "\n".join(line[10:] for line in body.splitlines() if line.strip()))
            for name in names:
                for step in steps:
                    try:
                        ci.workflow_plan(content, dict(name=name, steps=[dict(name=step, conclusion="failure")]))
                        return name
                    except (ci.Skipped, *SOURCE_ERRORS):
                        continue
    except (ci.Skipped, *SOURCE_ERRORS):
        return None
    return None


def total(github, workflow, conclusion):
    value = github.api(f"actions/workflows/{workflow}/runs?status={conclusion}&event=push&per_page=1")
    ci.need(type(value["total_count"]) is int)
    return value["total_count"]


def inspect(repo):
    github = ci.GitHub(repo)
    reason = "no_fitting_workflow"
    try:
        listing = github.api("contents/.github/workflows")
        ci.need(isinstance(listing, list))
        for entry in listing:
            if entry["type"] != "file" or not WORKFLOW.fullmatch(entry["name"]):
                continue
            file = github.api("contents/.github/workflows/" + entry["name"])
            ci.need(file["encoding"] == "base64")
            job = fitting_job(base64.b64decode(file["content"]).decode("utf-8"))
            if not job:
                continue
            if total(github, entry["name"], "failure") and total(github, entry["name"], "success"):
                return dict(repo=repo, workflow=entry["name"], job=job), "fit", github.requests
            reason = "no_red_and_green_push_runs"
    except (ci.Skipped, *SOURCE_ERRORS):
        reason = "workflows_unreadable"
    return None, reason, github.requests


def search(query, page):
    raw = ci.run(["gh", "api", "--hostname", "github.com", "--method", "GET", "search/code",
                  "-f", "q=" + query, "-f", "per_page=100", "-f", f"page={page}"])
    value = json.loads(raw, object_pairs_hook=c.unique_object)
    return [row["repository"]["full_name"].lower() for row in value["items"]
            if row["repository"]["private"] is False and failure_ingest.REPO.fullmatch(row["repository"]["full_name"])]


def candidates(queries, wanted, scan):
    started = time.monotonic()
    found, seen, outcomes, requests = [], set(), Counter(), 0
    for query in queries:
        for page in range(1, 11):
            requests += 1
            try:
                hits = search(query, page)
            except (ci.Skipped, *SOURCE_ERRORS):
                break
            if not hits:
                break
            for repo in [repo for repo in dict.fromkeys(hits) if repo not in seen][:scan - len(seen)]:
                seen.add(repo)
                print(f"ci-candidates: scanned {len(seen)}, fit {len(found)}", file=sys.stderr, flush=True)
                row, reason, used = inspect(repo)
                outcomes[reason] += 1
                requests += used
                if row:
                    found.append(row)
                if len(found) >= wanted or len(seen) >= scan:
                    return report(queries, started, seen, outcomes, requests, found)
    return report(queries, started, seen, outcomes, requests, found)


def report(queries, started, seen, outcomes, requests, found):
    return dict(queries=queries, scanned=len(seen), outcomes=dict(outcomes), api_requests=requests,
                wall_seconds=round(time.monotonic() - started, 3), candidates=found)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", action="append", help="GitHub code search query; repeat for more, default: " + "; ".join(QUERIES))
    parser.add_argument("--wanted", type=int, default=8, help="stop after this many fitting repositories, default 8")
    parser.add_argument("--scan", type=int, default=300, help="inspect at most this many repositories, default 300")
    args = parser.parse_args(argv)
    try:
        ci.need(args.wanted > 0 and 0 < args.scan <= 1000)
        print(json.dumps(candidates(args.query or list(QUERIES), args.wanted, args.scan), sort_keys=True))
        return 0
    except SOURCE_ERRORS:
        print("ci_candidates: invalid input or unavailable source", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
