#!/usr/bin/env python3
"""Print the weekly learning numbers for a private store. Makes no model calls and writes nothing."""
import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
from itertools import chain
import json
import re
import statistics
import sys

import check_delivery
import coldfix as c
import flakiness
import coldpass_replay
from learn_daily import LEDGER, fix_pairs, waiting
from learn_candidates import candidates
from learn_replay import PROOF_TESTS, reason_buckets
import transcript_delivery

TRACKED = "promote-to-check.json"
STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)")
ISSUE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/issues/[1-9]\d*")


def newly_proven(lessons, start):
    proven = [row for row in lessons.values() if row["verification"] == "proven"]
    before = {row["lesson_id"] for row in proven if c.time_key(row["created"]) < start}
    return len({row["lesson_id"] for row in proven if c.time_key(row["created"]) >= start} - before)


def rejected_replays(drafts, since):
    reasons = []
    for row in drafts:
        attempts = row.get("replay_attempts")
        if attempts is not None:
            reasons.extend(attempt["reason"] for attempt in attempts if attempt["day"] >= since)
        elif row["day"] >= since and row.get("replay", {}).get("status") == "rejected":
            reasons.append(row["replay"]["reason"])
    return reasons


def generated_test_calls(root, start):
    path = root / PROOF_TESTS
    tests = json.loads(c.read_text(path)) if path.exists() else {}
    return sum(c.time_key(asked) >= start for row in tests.values() for asked in row.get("asked", [row["created"]]))


def injection_rows(path):
    rows = []
    for number, line in enumerate(c.read_text(path).split("\n")[:-1], 1):
        try:
            row = json.loads(line)
            at = datetime.fromisoformat(row["at"]) if STAMP.fullmatch(row["at"]) else None
            valid = (at is not None and (row["session"] is None or isinstance(row["session"], str))
                     and type(row["bytes"]) is int and row["bytes"] >= 0)
        except (ValueError, TypeError, KeyError):
            valid = False
        c.require(valid, f"{path.name} line {number} is not an injection record")
        rows.append((at, row["session"], row["bytes"]))
    return rows


def context_injected(path, start):
    rows = [row for row in injection_rows(path) if row[0] >= start]
    sessions = Counter()
    for _, session, size in rows:
        if session is not None:
            sessions[session] += size
    return dict(sessions=len(sessions), events=sum(session is not None for _, session, _ in rows),
                median_bytes_per_session=statistics.median(sessions.values()) if sessions else 0,
                total_bytes=sum(sessions.values()), events_without_session=sum(session is None for _, session, _ in rows))


WEEK = timedelta(days=7)
CI_TYPES = ("ci", "coldbox")


def happened(row):
    return c.time_key(row.get("occurred", row["created"]))


def failure_kind(row):
    if row["source"] in ("github", "cold-review") and row.get("kind") in ("ci", "review"):
        return row["kind"]
    if row["source"] == "failure-ingest" and flakiness.metadata(row).get("type") in CI_TYPES \
            and flakiness.collection(row) == "failures":
        return "ci"
    return None


def repeat_failures(store, start, end):
    events = sorted((happened(row), row["repo"].casefold(), kind, c.signature(row))
                    for row in store["failures"].values() if (kind := failure_kind(row)))
    seen, result = set(), {}
    for at, repo, kind, group in events:
        if at > end:
            break
        if at >= start:
            row = result.setdefault(repo, {}).setdefault(kind, dict(failures=0, repeats=0))
            row["failures"] += 1
            row["repeats"] += (repo, kind, group) in seen
        seen.add((repo, kind, group))
    for kinds in result.values():
        for row in kinds.values():
            row["rate"] = round(row["repeats"] / row["failures"], 2)
    return result


def first_proof(store, record):
    proven = None
    while record and record["verification"] == "proven":
        proven = record["created"]
        record = store["lessons"].get(record["previous"])
    return proven


def ci_recurrence(store, rows, *, window=None, failures=None):
    if not rows:
        return []
    exact, normalized = {}, {}
    for row in store["failures"].values() if failures is None else failures:
        if row["source"] == "ci-fix-learning-local-reproduction":
            continue
        at = happened(row)
        if window and not window[0] <= at <= window[1]:
            continue
        repo = row["repo"].casefold() if window else None
        exact.setdefault((repo, row["group_id"]), []).append(at)
        normalized.setdefault((repo, c.signature(row)), []).append(at)
    latest, result = c.latest_lessons(store), []
    for lesson_id, delivery in sorted(rows.items()):
        record = latest.get(lesson_id)
        proven = record and first_proof(store, record)
        if not proven or record["status"] == "retired" or delivery.get("state") == "retired":
            continue
        repo = delivery["repo"].casefold() if window else None
        times = (exact if record["grouping"] == "exact" else normalized).get((repo, record["group_id"]), [])
        if window:
            proof_at = c.time_key(proven)
            if proof_at > window[1] or record["status"] in c.ENDED:
                continue
            times = [at for at in times if at > proof_at]
        delivered = delivery["at"] if delivery.get("state") == "delivered" else None
        result.append(dict(lesson=lesson_id, delivered=delivered, route=delivery.get("route") if delivered else None,
                           proven=proven, **transcript_delivery.around(delivered or proven, times)))
    return result


def briefing_cohorts(folder, store, now, backlog_store=None):
    files = transcript_delivery.lessons(folder)
    briefing = folder / "briefing.md"
    published = set(re.findall(r"(?m)^- \*\*(L\d+)\b", c.read_text(briefing))) if briefing.is_file() else set()
    result = {cohort: dict(lessons=0, repeats=0) for cohort in ("in_briefing", "held_back")}
    rows, cohorts = {}, {}
    for name in transcript_delivery.proven_candidates(files):
        meta = files[name]
        if not transcript_delivery.briefing_only(meta):
            continue
        held = transcript_delivery.held_back(meta)
        if held == (meta["id"] in published):
            continue
        lesson_id = meta["source"].split(":", 1)[1]
        rows[lesson_id] = dict(repo=meta["scope"].removeprefix("repo:"))
        cohorts[lesson_id] = "held_back" if held else "in_briefing"

    stores = (store,) if backlog_store is None else (store, backlog_store)
    window = (now - timedelta(days=14), now)
    for source in stores:
        failures = chain.from_iterable(peer["failures"].values() for peer in stores)
        for row in ci_recurrence(source, rows, window=window, failures=failures):
            group = result[cohorts[row["lesson"]]]
            group["lessons"] += 1
            group["repeats"] += row["failures_since"] > 0
            del rows[row["lesson"]]
    return result


def compared(rows, now):
    rows = [row for row in rows if row.get("route") != "existing-test"]
    result = {}
    for name, picked in (("delivered", [row for row in rows if row["delivered"]]),
                         ("undelivered", [row for row in rows if not row["delivered"] and row.get("proven")])):
        whole = [row for row in picked if c.time_key(row["delivered"] or row["proven"]) + WEEK <= now]
        result[name] = dict(lessons=len(whole), failures_before=sum(row["failures_before"] for row in whole),
                            failures_after=sum(row["failures_after"] for row in whole))
    return result


def report(root, now, days=7, folder=None, injections=None, oss_store=None):
    first = now.date() - timedelta(days=days - 1)
    start = datetime(first.year, first.month, first.day, tzinfo=timezone.utc)
    since = first.isoformat()
    store = c.load_store(root)[1]
    recent = lambda row: c.time_key(row["created"]) >= start
    ledger_path = root / LEDGER
    ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else dict(drafts={}, runs=[])
    rejected = rejected_replays(ledger["drafts"].values(), since)
    progress = root / "learn-review-replay.json"
    if progress.exists():
        source = c.ordinary_path(json.loads(c.read_text(progress))["report"])
        c.require(root in source.parents, "review replay report must stay in the private store")
        rows = json.loads(c.read_text(source))["results"]
        rejected.extend(row["reason"] for row in rows if row.get("before") and row.get("after") and row.get("reason"))
    runs = [row for row in ledger["runs"] if row["day"] >= since and "skipped" not in row]
    drafts = [row for row in ledger["drafts"].values() if row["day"] >= since]
    pairs = fix_pairs(root, coldpass_inputs=True)
    coldpass_replay.prepare_pairs(root, store, pairs)
    pending = waiting(store, ledger, pairs)
    delivery_rows = check_delivery.records(root)
    proven_ci = check_delivery.proven(root, store, ledger)
    readiness = [row for row in candidates(store, pairs=pairs) if "readiness" in row]
    failures = [row for row in store["failures"].values() if recent(row)]
    result = dict(
        window=dict(start=start.isoformat(), end=now.isoformat(), days=days),
        failures_collected=dict(Counter(flakiness.metadata(row).get("type") or row["source"] for row in failures)),
        flaky_failures=len(flakiness.detect(store)[1].intersection(row["id"] for row in failures)),
        candidates_waiting=len(pending),
        daily_runs=len(runs),
        daily_runs_failed=sum("error" in row or bool(row.get("replay_errors")) for row in runs),
        model_calls=sum(row["calls"] for row in runs) + generated_test_calls(root, start),
        drafts=dict(Counter(row["verdict"] for row in drafts)),
        lessons_proven=newly_proven(store["lessons"], start),
        lessons_delivered=check_delivery.origins(proven_ci, delivery_rows, start),
        repeat_failures=repeat_failures(store, start, now),
        ci_lesson_recurrence=ci_recurrence(store, {lesson["lesson_id"]: {} for lesson in proven_ci} | delivery_rows),
        lessons_rejected=reason_buckets(rejected),
    )
    if oss_store:
        outside = c.load_store(oss_store)[1]
        result["outside_repos"] = dict(
            lessons_proven=newly_proven(outside["lessons"], start),
            active=sum(row["status"] in c.LIVE and row["verification"] == "proven"
                       for row in c.latest_lessons(outside).values()),
            proof="replayed",
        )
    if injections:
        result["context_injected"] = context_injected(injections, start)
    if readiness:
        result["coldpass_readiness"] = [dict(fingerprint=row["fingerprint"], **row["readiness"]) for row in readiness]
        result["readiness_reasons"] = reason_buckets(code for row in readiness
            for codes in row["readiness"]["evidence"].values() for code in codes)
    if folder:
        result["lesson_recurrence"], result["promote_to_check"] = transcript_delivery.recurrence(root, store, folder)
        tracked = transcript_delivery.load(folder / TRACKED, {})
        c.require(isinstance(tracked, dict) and all(re.fullmatch(r"L[1-9]\d*", k) and isinstance(v, str)
                                                    and ISSUE.fullmatch(v) for k, v in tracked.items()),
                  f"{TRACKED} must map lesson ids to GitHub issue links")
        result["promote_to_check_untracked"] = [lesson for lesson in result["promote_to_check"] if lesson not in tracked]
        files = transcript_delivery.lessons(folder).values()
        result["enforced"] = sum(m.get("status") == "enforced" for m in files)
        result["retired_because_fixed"] = sum(m.get("status") == "retired" and bool(m.get("check", "").strip())
                                              for m in files)
    result["recurrence_comparison"] = compared(result["ci_lesson_recurrence"] + result.get("lesson_recurrence", []), now)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", required=True)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--lessons", help="shared lessons folder: add each lesson's failures before and after delivery")
    parser.add_argument("--injections", help="Coldrecall hook injections.jsonl: add the median bytes injected per session")
    parser.add_argument("--oss-store", help="read-only public-repository lesson store")
    args = parser.parse_args(argv)
    oss_store = args.oss_store and c.ordinary_path(args.oss_store)
    if oss_store and not oss_store.is_dir():
        parser.error("OSS store not found")
    folder = args.lessons and c.ordinary_path(args.lessons)
    if folder and not folder.is_dir():
        parser.error("lessons folder not found")
    injections = args.injections and c.ordinary_path(args.injections)
    if injections and not injections.is_file():
        parser.error("injections log not found")
    print(json.dumps(report(c.ordinary_path(args.store), datetime.now(timezone.utc), args.days, folder, injections, oss_store),
                     indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
