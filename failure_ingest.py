#!/usr/bin/env python3
"""Import CI attempts and explicit T3 blockers into the private store."""

from datetime import datetime, timezone
import json
import math
import os
import re
import shlex
import subprocess
import sys
from urllib.parse import quote, urlsplit

import coldfix as c
import flakiness
from github_sources import FAILED, GitHub, SHA


REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*\Z")
CATEGORIES = {"flake", "real-bug", "env-infra", "agent-mistake", "dependency", "unknown"}
RULES = (
    ("env-infra", "runner-or-disk", r"no space left on device|runner (?:has )?lost communication|runner (?:is )?offline"),
    ("env-infra", "network-unavailable", r"could not resolve host|temporary failure in name resolution|network is unreachable|\bECONNRESET\b"),
    ("dependency", "dependency-resolution", r"\bERESOLVE\b|\bERR_PNPM_OUTDATED_LOCKFILE\b|no matching distribution found for"),
    ("real-bug", "test-or-compiler-diagnostic", r"(?m)^\s*(?:AssertionError:|E\s+AssertionError|error TS[0-9]+:)"),
)
SOURCE_ERRORS = (c.ColdfixError, KeyError, TypeError, AttributeError, ValueError, OverflowError, RecursionError, OSError, UnicodeError)
TAIL = 8000
RECEIPT_WAIT_SECONDS = 7200
DEFERRED_RETRY_SECONDS = 7 * 86400


def need(value):
    c.require(value, "invalid failure-ingest input or incomplete source")


def fields(row, required, optional=""):
    need(isinstance(row, dict) and set(required.split()) <= set(row) <= set((required + " " + optional).split()))


def line(value):
    return c.text(value, "metadata", single_line=True)


def repo_name(value):
    need(isinstance(value, str) and REPO.fullmatch(value))
    return value.lower()


def integer(value):
    need(type(value) is int and value >= 1)
    return value


def seconds(value):
    need(type(value) in (int, float) and math.isfinite(value) and value >= 0)
    return value


def instant(value):
    need(isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)", value))
    c.occurred(value)
    return value


def links(values):
    need(isinstance(values, list))
    for value in values:
        line(value)
        parsed = urlsplit(value)
        need(parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password)
    return values


def classify(log, reported=None, retry=None):
    if reported is not None:
        return dict(category=reported, basis="caller-attested blocker category", rules=[])
    if retry is not None:
        return dict(category="flake", basis="caller-attested successful later attempt at the same head", rules=["same-head-retry"])
    diagnostic = re.sub(r"(?m)^[^\t\n]+\t[^\t\n]+\t\d{4}-\d{2}-\d{2}T[^\s]+[ \t]+", "", log)
    matches = [(category, rule) for category, rule, pattern in RULES if re.search(pattern, diagnostic, re.I)]
    categories = {category for category, _ in matches}
    return dict(category=next(iter(categories)) if len(categories) == 1 else "unknown",
                basis="diagnostic heuristic; no causal proof" if len(categories) == 1 else "conflicting or insufficient diagnostics",
                rules=[rule for _, rule in matches])


LOG_PREFIX = re.compile(r"^(?:(?P<step>[^\t\n]*\t[^\t\n]*)\t)?\ufeff?(?:(?P<time>\d{4}-\d\d-\d\dT[0-9:.]+Z) )?")
VERIFY_FAILED = "FAIL: real verification command failed"
FLEET_VERIFY = "./.fleet/verify.sh"
RUN_GROUP = "##[group]Run "
EXIT_ERROR = re.compile(r"##\[error\]Process completed with exit code [1-9]\d*\.")
LOG_COLOR = re.compile(r"(?:\x1b|\^\[)\[[0-9;]*m")


def log_lines(log):
    return [LOG_PREFIX.sub("", line).strip() for line in log.splitlines()]


def failing_command(log):
    return FLEET_VERIFY if VERIFY_FAILED in log_lines(log) else None


def recordable_step_command(command):
    return (len(command) <= TAIL and command.isprintable() and "***" not in command
            and not c.privacy_counts(command, filename_exempt=True))


def failed_step_commands(log, jobs):
    conclusions, windows = {}, {}
    for job in jobs:
        for step in job.get("steps") or []:
            key = job["name"] + "\t" + step["name"]
            conclusion = step["conclusion"]
            conclusions.setdefault(key, set()).add(conclusion)
            if step.get("started_at") and step.get("completed_at"):
                windows.setdefault(job["name"], []).append(
                    (step["started_at"][:19], step["completed_at"][:19], conclusion))
    raw_job = jobs[0]["name"] if len(jobs) == 1 else None
    step, header, echoed, command, began = None, "", "", "", ""
    commands = set()
    for raw in log.split("\n"):
        prefix = LOG_PREFIX.match(raw)
        if prefix["step"] != step:
            step, header, echoed, command = prefix["step"], "", "", ""
        when = (prefix["time"] or "")[:19]
        line = raw[prefix.end():].strip()
        if line.startswith("##[group]") or line == "Post job cleanup.":
            header = line.removeprefix(RUN_GROUP) if line.startswith(RUN_GROUP) else ""
            echoed, command, began = "", "", when
        elif line.startswith("shell: ") and header:
            if header == echoed:
                command = header
            header = ""
        elif header:
            if echoed or line.startswith("##["):
                header = ""
            else:
                echoed = LOG_COLOR.sub("", line).strip()
        elif command and EXIT_ERROR.fullmatch(line):
            if step is None or step.endswith("\tUNKNOWN STEP"):
                job_name = raw_job if step is None else step.rsplit("\t", 1)[0]
                failed = [conclusion for start, end, conclusion in windows.get(job_name, ())
                          if start <= began <= when <= end] == ["failure"]
            else:
                failed = conclusions.get(step) == {"failure"}
            if failed:
                commands.add(command)
            command = ""
    return commands


def recorded_command(failure):
    metadata = flakiness.metadata(failure)
    prefix = {"coldbox": "coldbox:", "verify-trace": "verify-trace:", "ci": "https://github.com/"}.get(metadata.get("type"))
    command = metadata.get("command") if prefix else None
    if prefix == "https://github.com/" and command is None:
        command = failing_command(metadata.get("log_tail") or "") or metadata.get("failed_step_command")
    if command is not None:
        c.require(failure.get("source") == "failure-ingest" and failure.get("source_ref", "").startswith(prefix),
                  "untrusted recorded command")
    return command


def ci_event(row):
    fields(row, "type repo run_id workflow job head_sha pr runner duration_seconds attempt conclusion occurred log_tail",
           "jobs retry_evidence check command failed_step_command repository_id")
    need(row["type"] == "ci")
    repo = repo_name(row["repo"])
    for key in ("workflow", "job", "runner"):
        line(row[key])
    for key in ("run_id", "attempt"):
        integer(row[key])
    need(isinstance(row["head_sha"], str) and SHA.fullmatch(row["head_sha"]))
    need(row["conclusion"] in FAILED | {"success"})
    if "check" in row:
        line(row["check"])
    need(row.get("command", FLEET_VERIFY) == FLEET_VERIFY)
    if "failed_step_command" in row:
        command = line(row["failed_step_command"])
        need(row["conclusion"] in FAILED and recordable_step_command(command))
    if "repository_id" in row:
        integer(row["repository_id"])
    need(row["pr"] is None or type(row["pr"]) is int and row["pr"] > 0)
    seconds(row["duration_seconds"])
    instant(row["occurred"])
    c.text(row["log_tail"], "log tail", optional=row["conclusion"] == "success")
    c.text(row["log_tail"][-TAIL:], "bounded log tail", optional=row["conclusion"] == "success")
    if "jobs" in row:
        need(isinstance(row["jobs"], list) and row["jobs"])
        seen = set()
        for job in row["jobs"]:
            fields(job, "id name runner duration_seconds conclusion")
            integer(job["id"])
            need(job["id"] not in seen and (job["conclusion"] == "success" if row["conclusion"] == "success" else job["conclusion"] in FAILED))
            seen.add(job["id"])
            line(job["name"])
            line(job["runner"])
            seconds(job["duration_seconds"])
    retry = row.get("retry_evidence")
    if retry is not None:
        fields(retry, "attempt head_sha conclusion url")
        integer(retry["attempt"])
        need(retry["attempt"] > row["attempt"] and retry["head_sha"] == row["head_sha"] and retry["conclusion"] == "success")
        need(retry["url"] == f"https://github.com/{repo}/actions/runs/{row['run_id']}/attempts/{retry['attempt']}")
    metadata = dict(row, repo=repo, log_tail=row["log_tail"][-TAIL:])
    metadata["classification"] = classify(metadata["log_tail"], retry=retry)
    ref = f"https://github.com/{repo}/actions/runs/{row['run_id']}#attempt-{row['attempt']}"
    if "check" in row:
        ref += ":job:" + quote(row["job"], safe="") + ":check:" + quote(row["check"], safe="")
    return dict(kind="ci", repo=repo, command=row["workflow"], error=c.encoded(metadata).strip(),
                agent="unknown", thread="", source_ref=ref, occurred=row["occurred"])


def t3_event(row):
    fields(row, "type thread_id repo route model status goal blocker_category what_tried evidence_links time_spent_seconds coldfix_fixable human_needed reason occurred")
    need(row["type"] == "t3" and row["status"] in ("partial", "blocked"))
    need(row["blocker_category"] in CATEGORIES)
    for key in ("thread_id", "repo", "route", "model"):
        line(row[key])
    for key in ("goal", "reason"):
        c.text(row[key], "blocker text")
    need(isinstance(row["what_tried"], list))
    for value in row["what_tried"]:
        c.text(value, "attempted action")
    links(row["evidence_links"])
    seconds(row["time_spent_seconds"])
    instant(row["occurred"])
    for key in ("coldfix_fixable", "human_needed"):
        need(type(row[key]) is bool)
    metadata = dict(row, classification=classify("", reported=row["blocker_category"]))
    ref = "t3:thread:" + quote(row["thread_id"], safe="") + ":blocker:" + quote(row["occurred"], safe="") + ":" + row["status"]
    return dict(kind="stuck", repo=row["repo"], command="T3 " + row["status"], error=c.encoded(metadata).strip(),
                agent=row["model"], thread=row["thread_id"], source_ref=ref, occurred=row["occurred"])


TRACE_FIELDS = set("schema_version run_id head step command status duration_ms inputs_hash".split())


def trace_event(row):
    fields(row, "type repo run_id head step command status duration_ms excerpt inputs_hash occurred")
    need(row["type"] == "verify-trace" and row["status"] in ("pass", "fail", "skip"))
    repo = repo_name(row["repo"])
    for key in ("run_id", "step"):
        line(row[key])
    c.text(row["command"], "step command")
    need(row["status"] != "fail" or isinstance(row["excerpt"], str))
    c.text(row["excerpt"] if row["status"] == "fail" else "-", "failing excerpt")
    need(row["head"] is None or isinstance(row["head"], str) and SHA.fullmatch(row["head"]))
    need(type(row["duration_ms"]) is int and row["duration_ms"] >= 0)
    need(row["inputs_hash"] is None or isinstance(row["inputs_hash"], str) and re.fullmatch(r"[0-9a-f]{64}", row["inputs_hash"]))
    instant(row["occurred"])
    metadata = dict(row, repo=repo, classification=classify(row["excerpt"] or ""))
    ref = f"verify-trace:{repo}:" + quote(row["run_id"], safe="") + ":" + quote(row["step"], safe="")
    return dict(kind="test", repo=repo, command=row["command"], error=c.encoded(metadata).strip(),
                agent="unknown", thread="", source_ref=ref, occurred=row["occurred"])


def trace_events(content, repo, occurred):
    seen, events = set(), []
    for text_line in content.split("\n"):
        if not text_line.strip():
            continue
        row = json.loads(text_line, object_pairs_hook=c.unique_object)
        need(isinstance(row, dict) and type(row.get("schema_version")) is int and row["schema_version"] in (1, 2, 3))
        need(TRACE_FIELDS <= set(row))
        event = trace_event(dict(type="verify-trace", repo=repo, run_id=row["run_id"], head=row["head"],
                                 step=row["step"], command=row["command"], status=row["status"],
                                 duration_ms=row["duration_ms"], excerpt=row.get("failing_excerpt") if row["status"] == "fail" else None,
                                 inputs_hash=row["inputs_hash"], occurred=occurred))
        key = (row["run_id"], row["step"])
        need(key not in seen)
        seen.add(key)
        if row["status"] == "fail":
            events.append(event)
    need(bool(seen))
    return events


UNDO_STEP = "step_id timestamp agent command reversibility reason pre_snapshot_id post_snapshot_id changed_paths"


def coldrewind_step(step):
    fields(step, UNDO_STEP)
    integer(step["step_id"])
    instant(step["timestamp"])
    line(step["agent"])
    need(isinstance(step["command"], list) and step["command"])
    for argument in step["command"]:
        c.text(argument, "step command", optional=True)
    need(step["reversibility"] in ("reversible", "partial", "irreversible"))
    c.text(step["reason"], "step reason", optional=True)
    for key in ("pre_snapshot_id", "post_snapshot_id"):
        need(step[key] is None or isinstance(step[key], str) and line(step[key]))
    need(isinstance(step["changed_paths"], list))
    for path in step["changed_paths"]:
        c.text(path, "changed path")
    return step


def coldrewind_event(value, repo, log=None):
    fields(value, "schema_version step")
    need(type(value["schema_version"]) is int and value["schema_version"] == 1)
    step = coldrewind_step(value["step"])
    command = step["command"]
    need(step["agent"] == "coldrewind" and len(command) == 3 and command[:2] == ["coldrewind", "undo"]
         and re.fullmatch(r"[1-9]\d*", command[2]))
    metadata = dict(type="coldrewind", repo=repo, step=step)
    undone = step
    if log is not None:
        fields(log, "schema_version steps")
        need(type(log["schema_version"]) is int and log["schema_version"] == 1 and isinstance(log["steps"], list))
        steps = {}
        for row in log["steps"]:
            need(coldrewind_step(row)["step_id"] not in steps)
            steps[row["step_id"]] = row
        undone = steps.get(int(command[2]))
        need(undone is not None)
        metadata["undone"] = undone
    ref = f"coldrewind:{repo}:undo:{step['step_id']}:" + quote(step["timestamp"], safe="")
    return dict(kind="revert", repo=repo, command=shlex.join(undone["command"]), error=c.encoded(metadata).strip(),
                agent=undone["agent"], thread="", source_ref=ref, occurred=step["timestamp"])


def coldrewind_content(metadata):
    fields(metadata, "type repo step", "undone")
    log = dict(schema_version=1, steps=[metadata["undone"]]) if "undone" in metadata else None
    event = coldrewind_event(dict(schema_version=1, step=metadata["step"]), repo_name(metadata["repo"]), log)
    return event["command"], c.encoded(sorted(metadata["step"]["changed_paths"])), False


def unclassified(metadata):
    return {k: v for k, v in metadata.items() if k != "classification"}


def coldbox_content(metadata):
    return c.encoded([metadata["repo"], metadata["check"], metadata["command"]]), re.sub(r"\d+", "<n>", c.normalized(metadata["log_tail"])), True


def trace_content(metadata):
    need(metadata["status"] == "fail")
    trace_event(unclassified(metadata))
    return metadata["command"], metadata["excerpt"], False


def ci_content(metadata):
    ci_event(unclassified(metadata))
    return c.encoded([metadata["workflow"], metadata["job"]]), metadata["log_tail"], False


def verify_pair_content(metadata, command):
    return command, metadata["failing_check"]["excerpt"], False


def settle_codex_content(metadata):
    need(isinstance(metadata["findings"], list) and metadata["findings"])
    return "codex review finding", c.encoded([[f["path"], f["title"]] for f in metadata["findings"]]), False


def coldpass_content(metadata):
    if metadata["failure_class"]:
        return "coldpass " + metadata["failure_class"], metadata["failure_class"], True
    return c.encoded(["coldpass unclassified", metadata["repo"]]), metadata["diagnostic"], True


def t3_content(metadata):
    t3_event(unclassified(metadata))
    return "T3 blocker", c.encoded([metadata["goal"], metadata["blocker_category"], metadata["reason"]]), False


GROUPING = {
    "coldbox": coldbox_content,
    "verify-trace": trace_content,
    "ci": ci_content,
    "verify-fix-pair": verify_pair_content,
    "settle-codex-lesson": settle_codex_content,
    "coldpass": coldpass_content,
    "t3": t3_content,
    "coldrewind": coldrewind_content,
}


def grouping_content(record):
    metadata = json.loads(record["error"], object_pairs_hook=c.unique_object)
    kind = metadata["type"]
    need(isinstance(kind, str) and kind in GROUPING)
    if kind == "verify-fix-pair":
        return verify_pair_content(metadata, record["command"])
    return GROUPING[kind](metadata)


def record_id(source_ref):
    return c.digest("failure-ingest-v1\n" + source_ref)[:32]


def record(event):
    row = c.event_record(event, "failure-ingest")
    row["id"] = record_id(event["source_ref"])
    return c.validate(row, "failures", row["id"])


def same_import(previous, incoming):
    before, after = ({key: value for key, value in row.items() if key != "created"}
                     for row in (previous, incoming))
    if before == after:
        return True
    old, new = flakiness.metadata(before), flakiness.metadata(after)
    field = "failed_step_command"
    if (before["source"] != "failure-ingest" or after["source"] != "failure-ingest"
            or old.get("type") != "ci" or new.get("type") != "ci"
            or (field in old) == (field in new)):
        return False
    enriched, metadata = (before, old) if field in old else (after, new)
    del metadata[field]
    enriched["error"] = c.encoded(metadata).strip()
    enriched["group_id"] = enriched["signature"] = c.group_id(enriched)
    return before == after


def import_records(root, records):
    from flakiness import collection

    # The existing lesson lock protects the read/check/write invariant, including retries.
    with c.lesson_lock(root):
        _, store = c.load_store(root)
        pending, seen = [], {**store["failures"], **store["passes"]}
        for row in records:
            previous = seen.get(row["id"])
            if previous is not None:
                need(same_import(previous, row))
            else:
                pending.append(row)
                seen[row["id"]] = row
        for row in pending:
            c.save(root, collection(row), row)
        _, actual = c.load_store(root)
        need(all(row["id"] in actual[collection(row)] for row in records))
    return dict(imported=len(pending), skipped=len(records) - len(pending))


def gh(command, *, raw=False, missing=None):
    result = subprocess.run(["gh", *command], capture_output=True, text=True, check=False)
    absent = (missing,) if isinstance(missing, str) else missing or ()
    if result.returncode and any(message in result.stderr for message in absent):
        return None
    need(result.returncode == 0)
    return result.stdout if raw else json.loads(result.stdout, object_pairs_hook=c.unique_object)


def failed_log(github, run_id, attempt, jobs, whole=True):
    if whole:
        log = gh(["run", "view", str(run_id), "--repo", "github.com/" + github.repo, "--attempt", str(attempt), "--log-failed"],
                 raw=True, missing=("log not found", "HTTP 410:") if jobs else "log not found")
        if log is not None and log.strip():
            return log, failed_step_commands(log, jobs)
    if not jobs:
        return f"GitHub kept no log for run {run_id} attempt {attempt} and listed no failed jobs", set()
    logs, commands = zip(*(job_log(github, job, TAIL // len(jobs)) for job in jobs))
    return "\n".join(logs), set().union(*commands)


def job_log(github, job, size):
    log = gh(["api", f"repos/{github.repo}/actions/jobs/{job['id']}/logs"], raw=True, missing=("HTTP 404", "HTTP 410:", "HTTP 410)"))
    if log is not None:
        return log[-size:], failed_step_commands(log, [job])
    notes, page = [], 1
    while True:
        batch = github.api(f"repos/{github.repo}/check-runs/{job['id']}/annotations?per_page=100&page={page}")
        need(isinstance(batch, list))
        notes += batch
        if len(batch) < 100:
            break
        page += 1
    steps = [step["name"] for step in job.get("steps") or [] if step["conclusion"] not in ("success", "skipped")]
    messages = [note["message"].strip() for note in notes if note["annotation_level"] == "failure" and note["message"].strip()]
    need(bool(messages))
    lines = [f"{job['name']}: GitHub kept no log; unfinished or failed steps: {', '.join(steps) or 'none'}"]
    lines += [f"{job['name']}: {message}" for message in messages]
    return "\n".join(lines)[-size:], set()


def fetch(repo, run_id, expected_attempt=None, hold=lambda jobs: None, covered=frozenset()):
    github = GitHub(repo)
    run = github.api(f"repos/{repo}/actions/runs/{run_id}")
    if expected_attempt is not None and isinstance(run, dict) and run.get("run_attempt") != expected_attempt:
        run = github.api(f"repos/{repo}/actions/runs/{run_id}/attempts/{expected_attempt}")
    need(isinstance(run, dict) and run["id"] == run_id and run["status"] == "completed" and run["conclusion"] in FAILED)
    integer(run["id"])
    need(repo_name(run["repository"]["full_name"]) == repo)
    attempt = integer(run["run_attempt"])
    need(expected_attempt is None or attempt == expected_attempt)
    jobs, failed, reported = [], [], 0
    for job in github.pages(f"actions/runs/{run_id}/attempts/{attempt}/jobs", key="jobs"):
        if job["conclusion"] not in FAILED:
            continue
        if job["runner_name"] in covered:
            reported += 1
            continue
        failed.append(job)
        need(job["run_id"] == run_id and job["status"] == "completed")
        integer(job["run_id"])
        started, completed = instant(job["started_at"]), instant(job["completed_at"])
        duration = seconds((c.time_key(completed) - c.time_key(started)).total_seconds())
        runner = job["runner_name"]
        need(runner is None or isinstance(runner, str))
        jobs.append(dict(id=job["id"], name=job["name"], runner=runner or "unknown",
                         duration_seconds=duration, conclusion=job["conclusion"]))
    jobs.sort(key=lambda job: job["id"])
    if reported and not jobs:
        return "coldbox"
    reason = hold(jobs)
    if reason:
        return reason
    prs = run["pull_requests"]
    need(isinstance(prs, list))
    numbers = sorted({integer(pr["number"]) for pr in prs})
    # Multiple PR associations cannot be reduced to one number without losing evidence.
    need(len(numbers) <= 1)
    started, completed = instant(run["run_started_at"]), instant(run["updated_at"])
    log, step_commands = failed_log(github, run_id, attempt, failed, whole=not reported)
    need(bool(log.strip()))
    row = dict(type="ci", repo=repo, run_id=run_id, workflow=run["name"],
               job="; ".join(job["name"] for job in jobs) or "unknown",
               runner="; ".join(sorted({job["runner"] for job in jobs})) or "unknown",
               head_sha=run["head_sha"], pr=numbers[0] if numbers else None,
               duration_seconds=seconds((c.time_key(completed) - c.time_key(started)).total_seconds()),
               attempt=attempt, conclusion=run["conclusion"], occurred=completed, log_tail=log[-TAIL:])
    if jobs:
        row["jobs"] = jobs
    command = failing_command(log)
    if command:
        row["command"] = command
    elif len(step_commands) == 1:
        command = step_commands.pop()
        if recordable_step_command(command):
            row["failed_step_command"] = command
    row["repository_id"] = integer(run["repository"]["id"])
    return record(ci_event(row))


def poll(root, repos, limit, workflow=None):
    _, store = c.load_store(root)
    known = {row["id"] for row in store["failures"].values() if row["source"] == "failure-ingest"}
    covered = set()
    missing_logs = {}
    diagnostic_receipts = set()
    ci_command = os.environ.get("COLDFIX_CI_COMMAND", "")
    for row in store["failures"].values():
        if not row.get("source_ref", "").startswith("coldbox:"):
            continue
        try:
            metadata = json.loads(row["error"])
            github = metadata.get("github") or {}
            if ci_command and re.search(r"(?:^|/)" + re.escape(ci_command) + r"(?:\s|$)", metadata.get("command", "")):
                diagnostic_receipts.add((metadata["id"], metadata["repo"], str(github["run_id"]), str(github["run_attempt"])))
                repo = repo_name(metadata["repo"])
                run_id, attempt = int(github["run_id"]), int(github["run_attempt"])
                integer(run_id)
                integer(attempt)
                ref = f"https://github.com/{repo}/actions/runs/{run_id}#attempt-{attempt}"
                if record_id(ref) not in known:
                    missing_logs[ref] = [repo, dict(databaseId=run_id, attempt=attempt,
                                                   conclusion="failure", workflowName=github.get("workflow"))]
            else:
                covered.add((metadata["id"], metadata["repo"], str(github["run_id"]), str(github["run_attempt"])))
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
    covered -= diagnostic_receipts
    ledger = root / "collected-receipts.json"
    seen = json.loads(ledger.read_text()) if ledger.exists() else {}
    collected = {name.removesuffix(".json") for name in seen if isinstance(seen, list) or seen[name] == "imported"}
    waiting_path = root / "github-waiting.json"
    deferred = json.loads(waiting_path.read_text()) if waiting_path.exists() else {}
    before = dict(deferred)
    now = datetime.now(timezone.utc)
    total = dict(imported=0, skipped=0)
    failed = unlisted = waiting = coldbox = 0
    for repo in repos:
        try:
            runs = gh(["run", "list", "--repo", "github.com/" + repo, "--status", "completed", "--limit", str(limit),
                       "--json", "databaseId,attempt,conclusion,workflowName,updatedAt"])
            need(isinstance(runs, list) and len(runs) <= limit)
        except SOURCE_ERRORS:
            unlisted += 1
            continue
        listed = {f"https://github.com/{repo}/actions/runs/{run.get('databaseId')}#attempt-{run.get('attempt')}" for run in runs}
        runs = runs + [run for ref, (owner, run) in deferred.items() if owner == repo and ref not in listed and ref not in missing_logs]
        runs += [run for ref, (owner, run) in missing_logs.items()
                 if owner == repo and ref not in listed
                 and (ref not in deferred or deferred[ref][1].get("retry_after", 0) <= now.timestamp())
                 and (workflow is None or run.get("workflowName") in (None, workflow))][:limit]
        seen = set()
        for run in runs:
            if workflow is not None and run.get("workflowName") not in (None, workflow):
                continue
            integer(run["databaseId"])
            integer(run["attempt"])
            need(run["conclusion"] in FAILED | {"success", "cancelled", "neutral", "skipped", "stale"})
            ref = f"https://github.com/{repo}/actions/runs/{run['databaseId']}#attempt-{run['attempt']}"
            need(ref not in seen)
            seen.add(ref)
            if run["conclusion"] not in FAILED:
                continue
            identity = c.digest("failure-ingest-v1\n" + ref)[:32]
            if identity in known:
                total["skipped"] += 1
                deferred.pop(ref, None)
                continue
            expired = False
            try:
                key = (repo.lower(), str(run["databaseId"]), str(run["attempt"]))
                boxed = {receipt[0] for receipt in covered if receipt[1:] == key}
                age = (now - c.time_key(run["updatedAt"])).total_seconds() if "updatedAt" in run else None
                recent = age is not None and age < RECEIPT_WAIT_SECONDS
                expired = age is not None and age > DEFERRED_RETRY_SECONDS
                def hold(jobs, recent=recent):
                    if recent and any(job["runner"].startswith("cbx-") and job["runner"] not in collected for job in jobs):
                        return "waiting_for_receipt"
                    return None
                options = dict(hold=hold) if recent else {}
                if boxed:
                    options["covered"] = boxed
                row = fetch(repo, run["databaseId"], run["attempt"], **options)
                if row == "coldbox":
                    coldbox += 1
                    deferred.pop(ref, None)
                    continue
                if row == "waiting_for_receipt":
                    waiting += 1
                    deferred[ref] = [repo, run]
                    continue
                result = import_records(root, [row])
            except SOURCE_ERRORS:
                failed += 1
                if ref in missing_logs:
                    deferred[ref] = [repo, dict(run, retry_after=now.timestamp() + DEFERRED_RETRY_SECONDS)]
                elif expired:
                    deferred.pop(ref, None)
                continue
            deferred.pop(ref, None)
            for key in total:
                total[key] += result[key]
            known.add(identity)
    if deferred != before:
        c.atomic_create(waiting_path, c.encoded(deferred), replace=True)
    if failed:
        total["failed"] = failed
    if unlisted:
        total["unlisted"] = unlisted
    if coldbox:
        total["coldbox"] = coldbox
    if waiting:
        total["waiting_for_receipt"] = waiting
    return total


def main(argv=None):
    parser = c.Parser(description=__doc__)
    parser.add_argument("--store", default=".coldfix", help="private existing-format failure store")
    modes = parser.add_subparsers(dest="mode", required=True)
    command = modes.add_parser("json", help="import one JSON object or an array of CI/T3 records")
    command.add_argument("--input", required=True)
    command = modes.add_parser("trace", help="import failed steps from a verify step trace")
    command.add_argument("--input", required=True, help=".cold/verify-trace.jsonl from the repository root")
    command.add_argument("--repo", required=True)
    command.add_argument("--occurred", required=True, help="ISO timestamp with timezone for the verify run")
    command = modes.add_parser("coldrewind", help="import one `coldrewind undo --json` result (schema_version 1) as revert evidence")
    command.add_argument("--input", required=True)
    command.add_argument("--repo", required=True)
    command.add_argument("--log", help="`coldrewind log --json` output (schema_version 1); the undone step's agent and command are used")
    command = modes.add_parser("github", help="fetch and import one completed failed GitHub run")
    command.add_argument("--repo", required=True)
    command.add_argument("--run-id", type=int, required=True)
    command = modes.add_parser("poll", help="one bounded pass over configured repositories")
    command.add_argument("--config", required=True, help='JSON object with a "repos" array')
    command.add_argument("--limit", type=int, default=20, help="recent completed runs examined per repository, default 20")
    args = parser.parse_args(argv)
    try:
        root = c.ordinary_path(args.store)
        need(not root.exists() or root.is_dir() and root.stat().st_mode & 0o077 == 0)
        for name in ("failures", "passes"):
            folder = c.ordinary_path(root / name)
            need(not folder.exists() or folder.is_dir() and folder.stat().st_mode & 0o077 == 0)
        if args.mode == "json":
            value = json.loads(c.read_text(args.input), object_pairs_hook=c.unique_object)
            rows = value if isinstance(value, list) else [value]
            records = [record(ci_event(row) if row.get("type") == "ci" else t3_event(row)) for row in rows]
            result = import_records(root, records)
        elif args.mode == "trace":
            rows = trace_events(c.read_text(args.input), repo_name(args.repo), args.occurred)
            result = import_records(root, [record(row) for row in rows])
        elif args.mode == "coldrewind":
            value = json.loads(c.read_text(args.input), object_pairs_hook=c.unique_object)
            log = json.loads(c.read_text(args.log), object_pairs_hook=c.unique_object) if args.log else None
            result = import_records(root, [record(coldrewind_event(value, repo_name(args.repo), log))])
        elif args.mode == "github":
            result = import_records(root, [fetch(repo_name(args.repo), integer(args.run_id))])
        else:
            value = json.loads(c.read_text(args.config), object_pairs_hook=c.unique_object)
            fields(value, "repos")
            need(isinstance(value["repos"], list) and value["repos"] and args.limit > 0)
            repos = [repo_name(repo) for repo in value["repos"]]
            need(len(set(repos)) == len(repos))
            result = poll(root, repos, args.limit)
        print(json.dumps(result, sort_keys=True))
        return 2 if result.get("failed") or result.get("unlisted") else 0
    except SOURCE_ERRORS:
        print("failure_ingest: invalid input, incomplete source, or I/O failure; retry preserves completed records", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
