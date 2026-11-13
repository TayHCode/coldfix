#!/usr/bin/env python3
"""Rank the workflow features that keep public CI jobs from replaying locally. Read-only gh GETs."""

import argparse
import base64
from collections import Counter
import json
from pathlib import Path
import re
import sys
import time

import ci_candidates as cand
import ci_fix_learning as ci
import coldfix as c
from failure_ingest import SOURCE_ERRORS

CHECKS = {"unittest", "pytest", "py.test", "mypy", "ruff", "tsc"}
WRAPPERS = {"poetry", "uv", "pipenv", "pdm", "hatch", "xvfb-run", "coverage"}
JOB_KEYS = {"if", "uses", "needs", "services", "container", "env", "defaults", "environment"}
STEP_KEYS = {"if", "shell", "working-directory", "env", "continue-on-error", "timeout-minutes"}
EXPRESSION = re.compile(r"\$\{\{\s*([A-Za-z_]+)")


def command_head(words):
    words = [w for w in words if not re.fullmatch(r"[A-Za-z_]\w*=\S*", w)]
    tokens = []
    while words and words[0] in WRAPPERS | {"sudo"}:
        tokens.append("wrapper:" + words[0]) if words[0] != "sudo" else tokens.append("run:sudo")
        words = words[1:]
        if words[:1] == ["run"]:
            words = words[1:]
    if not words:
        return tokens
    if words[0] in ("python", "python3") or re.fullmatch(r"python3\.\d+", words[0]):
        words = words[2:] if words[1:2] == ["-m"] and len(words) > 2 else words[:1]
        if words[:2] == ["pip", "install"]:
            words = ["pip"]
    return tokens + ["run:" + words[0]]


def run_tokens(text):
    tokens = []
    for line in text.replace("\\\n", " ").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        segments = [s.strip() for s in re.split(r"&&|;|\|\||\|", line) if s.strip()]
        if len(segments) > 1 or re.search(r"[><`]|\$\(", line):
            tokens.append("shell-syntax")
        for segment in segments:
            tokens += command_head(segment.split())
    return tokens


def step_run(body):
    match = re.search(r"^      (?:- |  )run:[ ]*(.*)$", body, re.M)
    if not match:
        return None
    value = match[1].strip()
    if value[:1] in "|>":
        lines = body[match.end():].splitlines()
        return "\n".join(line.strip() for line in lines if line.strip()), True
    return ci.scalar(value), False


def action_token(body, keys):
    use = ci.scalar(re.search(r"^      (?:- |  )uses:\s*(.+)$", body, re.M)[1].split()[0]).split("@")[0]
    inputs = re.findall(r"^          ([\w-]+):", body, re.M)
    if use == "actions/checkout":
        return [] if "with" not in keys else ["action:actions/checkout-with"]
    if use == "actions/setup-python":
        return [] if inputs in ([], ["python-version"]) else ["action:actions/setup-python-inputs"]
    return ["action:" + use]


def root_trigger(content):
    match = re.search(r"^(?:on|\"on\"|'on'):(.*(?:\n(?:[ #].*|))*)", content, re.M)
    return match[1] if match else ""


def classify(content):
    if "\t" in content:
        return {}
    tokens = set()
    if not re.search(r"\bpush\b", root_trigger(content)):
        tokens.add("trigger:no-push")
    if re.search(r"^(?:env|defaults):", content, re.M):
        tokens.add("workflow:env")
    for kind in EXPRESSION.findall(content):
        tokens.add("secrets" if kind == "secrets" else "matrix" if kind == "matrix" else "expression:" + kind)
    if re.search(r"^\s+(?:strategy|matrix):", content, re.M):
        tokens.add("matrix")
    root = re.search(r"^jobs:\s*$", content, re.M)
    if not root:
        return {}
    lines = content[root.end():].splitlines()
    starts = [i for i, line in enumerate(lines) if re.fullmatch(r"  [A-Za-z_][\w-]*:\s*", line)]
    found = {}
    for n, start in enumerate(starts):
        block = lines[start:starts[n + 1] if n + 1 < len(starts) else len(lines)]
        text = "\n".join(block)
        job = block[0].strip()[:-1]
        have = set(tokens)
        runner = next((ci.scalar(m[1]) for line in block if (m := re.fullmatch(r"    runs-on:\s*(.+)", line))), "")
        if not re.fullmatch(r"ubuntu-(?:latest|\d{2}\.\d{2})", runner):
            have.add("runner:" + (re.sub(r"[^\w.-]", "", runner.split("-")[0]) or "unknown"))
        for key in re.findall(r"^    ([\w-]+):", text, re.M):
            if key in JOB_KEYS:
                have.add("job:" + key)
            if key == "strategy":
                have.add("matrix")
        checks = 0
        steps = re.split(r"(?m)^(?=      - )", "\n".join(block[1:]))
        for body in steps:
            if not body.startswith("      - "):
                continue
            keys = re.findall(r"^      (?:- |  )([\w-]+):", body, re.M)
            have.update("step:" + key for key in keys if key in STEP_KEYS)
            if "uses" in keys:
                have.update(action_token(body, keys))
            run = step_run(body)
            if run:
                value, multiline = run
                found_tokens = run_tokens(value)
                checks += any(t[4:] in CHECKS for t in found_tokens if t.startswith("run:"))
                have.update(t for t in found_tokens if not (t.startswith("run:") and t[4:] in CHECKS))
                if multiline and len(value.splitlines()) > 1:
                    have.add("run:multiline")
        if checks:
            found[job] = sorted(have)
        elif any("run:" in step for step in steps):
            found[job] = sorted(have | {"no-check-step"})
    return found


def rank(repos):
    best, counts = {}, Counter()
    for repo, files in repos.items():
        jobs = [(tuple(tokens), path + "#" + job) for path, content in sorted(files.items())
                for job, tokens in classify(content).items()]
        usable = [j for j in jobs if "no-check-step" not in j[0]]
        pool = usable or jobs
        if not pool:
            best[repo] = None
            continue
        tokens, where = min(pool, key=lambda j: (len(j[0]), j[1]))
        best[repo] = dict(job=where, tokens=list(tokens), all_jobs=[list(j[0]) for j in jobs])
    scanned = len(best)
    blocked = {r: b for r, b in best.items() if b}
    for row in blocked.values():
        counts.update(row["tokens"])
    sole = Counter(row["tokens"][0] for row in blocked.values() if len(row["tokens"]) == 1)
    order = [t for t, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
    chosen, curve = set(), []
    for token in order:
        chosen.add(token)
        done = sum(any(set(t) <= chosen for t in row["all_jobs"]) for row in blocked.values())
        curve.append(dict(add=token, repos_usable=done))
    now = sum(any(not t for t in row["all_jobs"]) for row in blocked.values())
    ranking = [dict(blocker=t, repos_blocked=counts[t], repos_unlocked_alone=sole.get(t, 0)) for t in order]
    return dict(repositories=scanned, repositories_without_a_workflow_job=scanned - len(blocked),
                usable_today=now, ranking=ranking, cumulative_by_blocked_count=curve, repos=best)


def markdown(report, source):
    lines = ["# Replay blockers over the pass-2 repositories", "",
             f"Source: {source}",
             f"Repositories {report['repositories']}; with no readable job {report['repositories_without_a_workflow_job']}; "
             f"a job needing none of the blockers below today {report['usable_today']}.", "",
             "Each repository is represented by its job with the fewest blockers (jobs without a check command rank last).",
             "`blocked` counts repositories whose best job has the blocker. `alone` counts repositories where it is the only one.",
             "Approximation: every non-check step counts as a blocker and an unavailable python-version is ignored,",
             "so this ranks the work; the measured result is the post-change candidate selection.", "",
             "| rank | blocker | blocked | alone |", "| --- | --- | ---: | ---: |"]
    for n, row in enumerate(report["ranking"], 1):
        lines.append(f"| {n} | `{row['blocker']}` | {row['repos_blocked']} | {row['repos_unlocked_alone']} |")
    lines += ["", "Cumulative: supporting blockers in the order above, repositories with a usable job:", ""]
    for n, row in enumerate(report["cumulative_by_blocked_count"], 1):
        lines.append(f"{n}. add `{row['add']}`: {row['repos_usable']}")
    return "\n".join(lines) + "\n"


def search_files(query):
    found, page = {}, 1
    while page <= 10:
        limits = json.loads(ci.run(["gh", "api", "rate_limit", "--jq", ".resources.code_search"]))
        if limits["remaining"] == 0:
            time.sleep(max(0, limits["reset"] - time.time()) + 1)
        try:
            raw = ci.run(["gh", "api", "--hostname", "github.com", "--method", "GET", "search/code",
                          "-f", "q=" + query, "-f", "per_page=100", "-f", f"page={page}"])
        except ci.Skipped:
            break
        items = json.loads(raw, object_pairs_hook=c.unique_object)["items"]
        if not items:
            break
        for row in items:
            repo = row["repository"]["full_name"].lower()
            if row["repository"]["private"] is False and re.fullmatch(r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml", row["path"]):
                found.setdefault(repo, set()).add(row["path"])
        page += 1
    return found


def collect(queries, scan):
    wanted = {}
    for query in queries:
        for repo, paths in search_files(query).items():
            wanted.setdefault(repo, set()).update(paths)
    repos = {}
    for repo in list(wanted)[:scan]:
        files = {}
        for path in sorted(wanted[repo]):
            try:
                file = ci.GitHub(repo).api("contents/" + path)
                ci.need(file["encoding"] == "base64")
                files[path] = base64.b64decode(file["content"]).decode("utf-8")
            except (ci.Skipped, *SOURCE_ERRORS, UnicodeDecodeError):
                continue
        repos[repo] = files
        print(f"ci-blockers: {len(repos)}/{min(scan, len(wanted))}", file=sys.stderr, flush=True)
    return repos


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    fetch = sub.add_parser("collect", help="search public repositories and save their matched workflow files")
    fetch.add_argument("out", help="private JSON file to write")
    fetch.add_argument("--query", action="append", help="code search query, repeat; default is the pass-2 queries")
    fetch.add_argument("--scan", type=int, default=1000)
    ranked = sub.add_parser("rank", help="classify saved workflow files")
    ranked.add_argument("files")
    ranked.add_argument("--json", required=True)
    ranked.add_argument("--markdown", required=True)
    ranked.add_argument("--source", default="saved workflow files")
    args = parser.parse_args(argv)
    try:
        if args.command == "collect":
            ci.need(args.scan > 0)
            data = collect(args.query or PASS2_QUERIES, args.scan)
            Path(args.out).write_text(json.dumps(data, sort_keys=True))
            print(json.dumps(dict(repositories=len(data), files=sum(len(f) for f in data.values()))))
        else:
            report = rank(json.loads(Path(args.files).read_text()))
            Path(args.json).write_text(json.dumps(report, indent=1, sort_keys=True) + "\n")
            Path(args.markdown).write_text(markdown(report, args.source))
            print(json.dumps({k: report[k] for k in ("repositories", "usable_today")}))
        return 0
    except SOURCE_ERRORS:
        print("ci_blockers: invalid input or unavailable source", file=sys.stderr)
        return 2


PASS2_QUERIES = [*cand.QUERIES,
                 'path:.github/workflows extension:yml size:<700 "python -m unittest"',
                 'path:.github/workflows extension:yml size:<700 "python3 -m unittest"',
                 'path:.github/workflows extension:yml size:<700 "ruff check"',
                 'path:.github/workflows extension:yml size:<700 "tsc --noEmit"']

if __name__ == "__main__":
    sys.exit(main())
