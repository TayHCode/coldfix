#!/usr/bin/env python3
"""List recurring failures, fixed failures and strong transcript leads. Makes no model calls."""
import argparse
import json
import re
import sys

import coldfix as c
import flakiness
import coldpass_replay
import regression_adapter

MIN_COMMITS = 3
MIN_REPOS = 2
TRANSCRIPT_SCORE = 0.6
EXCLUDED = {"flake", "env-infra"}
ERROR = re.compile(r"\b(?:FAIL(?:ED)?|ERROR|Error|error|AssertionError)\b")
NOISE = re.compile(r"Process completed with exit code|^Traceback|^FAILED \(")
DIAGNOSTIC = re.compile(r"(?:FAIL|ERROR): test\w*")
MARKUP = re.compile(r"!\[[^\]]*\]\([^)]*\)|<[^>]+>|\*\*")


PREFIX = re.compile(r"^([^\t]*)\t[^\t]*\t(?=\d{4}-\d\d-\d\dT)")


def headline(log, job):
    for raw in log.splitlines():
        prefix = PREFIX.match(raw)
        line = re.sub(r"^(?:<timestamp>\s*|##\[\w+\]\s*)+", "", c.normalized(raw[prefix.end():] if prefix else raw))
        if ERROR.search(line) and not NOISE.search(line):
            return (prefix[1] if prefix else job), re.sub(r"\d+", "<n>", line)[:300]
    return None


def revision(row):
    if row.get("type") == "coldpass":
        return c.recorded_revision(row)
    for key in ("head_sha", "sha", "head"):
        value = row.get(key)
        if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{40}", value):
            return value.lower()
    return None

def finding(text):
    lines = [" ".join(line.split()) for line in MARKUP.sub("", text).splitlines()]
    return next((line for line in lines if line), "")[:300]


def replayable(record, row, findings):
    if row.get("type") == "coldpass":
        return False
    if record.get("kind") == "review":
        return record.get("source_ref") in findings
    return bool(revision(row) and (DIAGNOSTIC.search(record["error"]) or regression_adapter.PYTEST_ID.search(record["error"])))


def fingerprint(record, row):
    if record.get("kind") == "review":
        return c.signature(record)[:32], finding(record["error"]) or record["command"]
    if record.get("source") == "failure-ingest" and row.get("type") == "ci":
        found = headline(row.get("log_tail", ""), row["job"])
        if found:
            return c.digest(c.encoded([row["workflow"], *found]))[:32], found[1]
    return c.signature(record)[:32], record["command"]


def candidates(store, exclude=(), pairs=()):
    flaky = flakiness.detect(store)[1]
    findings = {pair["finding_url"] for pair in pairs if pair.get("after") and pair.get("finding_url")}
    groups = {}
    for record in store["failures"].values():
        row = flakiness.metadata(record)
        classification = row.get("classification")
        category = classification.get("category") if isinstance(classification, dict) else None
        if record["id"] in flaky or isinstance(category, str) and category in EXCLUDED:
            continue
        key, line = fingerprint(record, row)
        group = groups.setdefault(key, dict(fingerprint=key, headline=line, evidence=[], repos=set(), commits=set(),
                                            transcript=False, replayable=False))
        group["evidence"].append(record["id"])
        group["repos"].add(record["repo"])
        group["commits"].add((record["repo"], revision(row)))
        group["replayable"] |= replayable(record, row, findings)
        if row.get("type") == "coldpass":
            group["readiness"] = coldpass_replay.readiness(store, group["evidence"], pairs)
            group["replayable"] = group["readiness"]["ready"]
        score = row.get("score")
        group["transcript"] |= (record.get("command") == "transcript correction" and record.get("source_ref", "").startswith("transcript:")
                                and row.get("type") == "transcript_correction" and type(score) in (int, float)
                                and TRANSCRIPT_SCORE <= score <= 1)
    rows = [dict(group, evidence=sorted(group["evidence"]), repos=sorted(group["repos"]), commits=len(group["commits"]))
            for key, group in groups.items() if key not in exclude
            and ("readiness" in group or group["transcript"] or group["replayable"] or len(group["commits"]) >= MIN_COMMITS or len(group["repos"]) >= MIN_REPOS)]
    return sorted(rows, key=lambda row: (-row["commits"], -len(row["repos"]), row["fingerprint"]))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", required=True)
    args = parser.parse_args(argv)
    print(json.dumps(candidates(c.load_store(c.ordinary_path(args.store))[1]), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
