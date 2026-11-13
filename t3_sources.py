#!/usr/bin/env python3
"""Collect explicit T3 failures from a bounded, read-only local SQLite snapshot."""

from collections import Counter
from datetime import datetime, timezone
import json
import re
import sqlite3
import sys
from urllib.parse import quote

import coldfix as c


TABLES = {
    "runs": ("orchestration_v2_projection_runs", "run_id thread_id status requested_at completed_at provider payload_json"),
    "items": ("orchestration_v2_projection_turn_items", "turn_item_id thread_id run_id type status updated_at payload_json"),
    "threads": ("orchestration_v2_projection_threads", "thread_id project_id payload_json"),
    "projects": ("projection_projects", "project_id title workspace_root"),
}
RUN_STATES = {"queued", "running", "completed", "failed", "cancelled", "interrupted"}
ITEM_STATES = {"pending", "running", "waiting", "completed", "failed", "cancelled", "interrupted"}
EXCERPT = 1000
STOPPED = re.compile(r"(?i)\b(?:cancelled|canceled|cancellation|interrupted|interruption)\b|\buser (?:stopped|stop)\b")


def need(value):
    c.require(value, "invalid or unsupported T3 source; no batch published")


def string(value, empty=False):
    return c.text(value, "T3 field", optional=empty)


def identity(value):
    return c.text(value, "T3 identity", single_line=True)


def timestamp(value):
    string(value)
    need(re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value))
    c.occurred(value)
    return c.time_key(value)


def payload(row, id_column, *, run=False):
    value = json.loads(row["payload_json"], object_pairs_hook=c.unique_object)
    need(isinstance(value, dict) and value["id"] == row[id_column] and value["threadId"] == row["thread_id"])
    if not run and "run_id" in row.keys():
        need(value["runId"] == row["run_id"])
    if run or id_column == "turn_item_id":
        need(value["status"] == row["status"])
    return value


def collect(db, project, threads, limit, since=None):
    events, counts, truncation, excerpts = [], {}, {}, Counter()
    cutoff = c.time_key(since) if since else None
    connection = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        for table, columns in TABLES.values():
            found = {row["name"] for row in connection.execute("PRAGMA table_info(" + table + ")")}
            need(set(columns.split()) <= found)
        project_row = connection.execute("SELECT project_id,title,workspace_root FROM projection_projects WHERE project_id=?",
                                         (project,)).fetchone()
        need(project_row is not None)
        string(project_row["title"])
        repo = string(project_row["workspace_root"])
        for thread in threads:
            row = connection.execute("SELECT thread_id,project_id,payload_json FROM orchestration_v2_projection_threads WHERE thread_id=?",
                                     (thread,)).fetchone()
            need(row is not None and row["project_id"] == project)
            value = json.loads(row["payload_json"], object_pairs_hook=c.unique_object)
            need(isinstance(value, dict) and value["id"] == thread and value["projectId"] == project)

        rows = {}
        placeholders = ",".join("?" for _ in threads)
        for name in ("runs", "items"):
            table, columns = TABLES[name]
            where = " WHERE thread_id IN (" + placeholders + ")"
            total = connection.execute("SELECT count(*) FROM " + table + where, threads).fetchone()[0]
            rows[name] = connection.execute("SELECT " + ",".join(columns.split()) + " FROM " + table + where
                                            + " ORDER BY " + columns.split()[0] + " LIMIT ?", (*threads, limit)).fetchall()
            counts[name] = dict(total=total, retained=len(rows[name]))
            truncation[name] = total > len(rows[name])

        runs = {}
        for row in rows["runs"]:
            identity(row["run_id"])
            need(row["status"] in RUN_STATES and row["run_id"] not in runs)
            payload(row, "run_id", run=True)
            timestamp(row["requested_at"])
            if row["completed_at"] is not None:
                need(timestamp(row["completed_at"]) >= timestamp(row["requested_at"]))
            if row["status"] in {"completed", "failed"}:
                need(row["completed_at"] is not None)
            identity(row["provider"])
            runs[row["run_id"]] = row

        def reference(thread, kind, ref):
            return "t3:project:" + quote(project, safe="") + ":thread:" + quote(thread, safe="") + ":" + kind + ":" + quote(ref, safe="")

        def emit(thread, kind, ref, command, diagnostic, when, run, time_field):
            timestamp(when)
            if cutoff is not None and c.time_key(when) < cutoff:
                return
            diagnostic = string(diagnostic)
            if len(diagnostic) > EXCERPT:
                excerpts["truncated"] += 1
            event = dict(kind="stuck", command=command, error=diagnostic[:EXCERPT], repo=repo,
                         agent=run["provider"] if run else "unknown", thread=thread,
                         source_ref=reference(thread, kind, ref), occurred=when)
            c.event_record(event, "t3")
            events.append(event)
            excerpts["retained"] += 1
            event_times[event["source_ref"]] = time_field

        def linked_run(row):
            ref = row["run_id"]
            if ref is None:
                return None
            identity(ref)
            need(ref in runs or truncation["runs"])
            run = runs.get(ref)
            need(run is None or run["thread_id"] == row["thread_id"])
            return run

        event_times, item_ids = {}, set()
        for row in rows["items"]:
            identity(row["turn_item_id"])
            need(row["turn_item_id"] not in item_ids)
            item_ids.add(row["turn_item_id"])
            need(row["status"] in ITEM_STATES)
            string(row["type"])
            timestamp(row["updated_at"])
            value = payload(row, "turn_item_id")
            need(value["type"] == row["type"])
            run = linked_run(row)
            if row["type"] != "error" or row["status"] != "failed":
                continue
            failure = value["failure"]
            need(isinstance(failure, dict))
            diagnostic = string(failure["message"])
            if run is not None and run["status"] not in {"completed", "failed"}:
                continue
            # A truncated run snapshot cannot establish whether this item was cancelled.
            if row["run_id"] is not None and run is None:
                continue
            if STOPPED.search(diagnostic):
                continue
            emit(row["thread_id"], "item", row["turn_item_id"], "T3 error", diagnostic,
                 row["updated_at"], run, "turn_items.updated_at")

        events.sort(key=lambda event: (c.time_key(event["occurred"]), event["source_ref"]))
        receipt = dict(version=1, source="t3-sqlite", collected_at=datetime.now(timezone.utc).isoformat(),
                       db=str(db), project=project, threads=list(threads),
                       limit=limit, since=since, counts=counts, truncated=truncation,
                       snapshot_complete=not any(truncation.values()), effectiveness_coverage_complete=False,
                       excerpts=dict(max_chars=EXCERPT, **excerpts), event_time_fields=event_times,
                       unsupported=["wrong_fix", "prose stuck or blocked endings", "failed runs without diagnostics"], events=len(events))
        return events, receipt
    finally:
        connection.rollback()
        connection.close()


def main(argv=None):
    parser = c.Parser(description=__doc__)
    parser.add_argument("--db", required=True, help="local T3 SQLite file, opened read-only")
    parser.add_argument("--project", required=True, help="exact T3 project ID")
    parser.add_argument("--thread", required=True, action="append", help="exact thread ID in the project; repeat to select more")
    parser.add_argument("--limit", type=int, default=10000, help="maximum rows read per source table, default 10000")
    parser.add_argument("--since", help="inclusive event date or timestamp with timezone")
    parser.add_argument("--store", default=".coldfix", help="private .coldfix directory")
    parser.add_argument("--output", default="t3-events.jsonl", help="new JSONL filename inside the store")
    args = parser.parse_args(argv)
    try:
        db = c.ordinary_path(args.db)
        need(db.is_file())
        project = identity(args.project)
        threads = tuple(sorted({identity(thread) for thread in args.thread}))
        need(0 < len(threads) <= 100 and 0 < args.limit <= 100000)
        if args.since is not None:
            c.occurred(args.since)
        root = c.ordinary_path(args.store)
        need(root.name == ".coldfix")
        need(not root.exists() or root.is_dir() and root.stat().st_mode & 0o077 == 0)
        need(re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*\.jsonl", args.output))
        output = c.ordinary_path(root / args.output)
        receipt_path = c.ordinary_path(root / (args.output + ".receipt.json"))
        need(not output.exists() and not receipt_path.exists())
        events, receipt = collect(db, project, threads, args.limit, args.since)
        content = "".join(json.dumps(row, ensure_ascii=True, sort_keys=True) + "\n" for row in events)
        c.input_records(content, "jsonl", "t3")
        receipt["batch_sha256"] = c.digest(content)
        root.mkdir(parents=True, mode=0o700, exist_ok=True)
        c.atomic_create(receipt_path, c.encoded(receipt))
        c.atomic_create(output, content)
        print(json.dumps(dict(events=len(events), truncated=not receipt["snapshot_complete"],
                              **{key + "_seen": value["retained"] for key, value in receipt["counts"].items()}), sort_keys=True))
        return 0
    except (c.ColdfixError, sqlite3.Error, KeyError, TypeError, ValueError, OverflowError, RecursionError, OSError, UnicodeError):
        print("t3_sources: invalid input, unsupported source, or local I/O failure; no batch published", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
