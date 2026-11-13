#!/usr/bin/env python3
"""Answer the seven North Star status questions from a private store. Makes no model calls and writes nothing."""
import argparse
from collections import Counter
import fcntl
import json
import os
import sqlite3
import sys

import check_delivery
import coldfix as c
import learn_daily
import transcript_delivery

LOCKS = {".lessons.lock": "lesson store write", ".learn-daily.lock": "daily or backlog", ".check-delivery.lock": "check delivery",
         ".transcript-lessons.lock": "transcript lessons", ".transcript-delivery.lock": "transcript delivery"}
ACTIVE = ("active", "conditional", "enforced")


def scanned(ledger):
    if not ledger.exists():
        return {}
    db = sqlite3.connect("file:" + str(ledger) + "?mode=ro", uri=True)
    try:
        return dict(db.execute("SELECT status, count(*) FROM scans GROUP BY status").fetchall())
    finally:
        db.close()


def held(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(fd)


def summary(root, store, folder, scan_ledger):
    drafts = transcript_delivery.load(root / learn_daily.LEDGER, dict(drafts={}, runs=[]))
    leads = transcript_delivery.load(root / transcript_delivery.LEDGER, dict(leads={}, batches=[]))
    deliveries = transcript_delivery.load(root / transcript_delivery.DELIVERIES, dict(leads={}))["leads"]
    checks = check_delivery.records(root)
    lessons = c.latest_lessons(store).values()
    files = transcript_delivery.lessons(folder).values()
    scans = scanned(scan_ledger)
    lead_states = Counter(lead["status"] for lead in leads["leads"].values())
    origins = check_delivery.origins([], checks)
    runs = [row for row in drafts["runs"] if "skipped" not in row]
    failing = []
    if runs and ("error" in runs[-1] or runs[-1].get("replay_errors")):
        failing.append("daily run " + runs[-1]["day"])
    if leads["batches"] and leads["batches"][-1].get("errors"):
        failing.append("transcript batch " + leads["batches"][-1]["started"])
    return dict(
        transcripts_analyzed=dict(total=sum(scans.values()), by_status=scans),
        candidates=dict(ci_and_review_drafts=len(drafts["drafts"]),
                        transcript_leads=sum(n for state, n in lead_states.items() if state not in ("no_lesson", "retry"))),
        verified=dict(ci_and_review=sum(row["verification"] == "proven" for row in lessons),
                      transcript=lead_states["proven"]),
        active=dict(shared_lessons=sorted((m["id"] for m in files if m.get("status") in ACTIVE),
                                          key=lambda key: int(key[1:])),
                    ci_and_review=sum(row["status"] == "active" for row in lessons)),
        repos_receiving=sorted({row["repo"] for row in checks.values() if row.get("state") == "delivered"}
                               | {repo for e in deliveries.values() if e["status"] == "delivered"
                                  for repo in e.get("approved_repos", [])}),
        prevented_or_fixed=dict(
            enforced=sorted(m["id"] for m in files if m.get("status") == "enforced"),
            retired_because_fixed=sorted(m["id"] for m in files
                                         if m.get("status") == "retired" and m.get("check", "").strip()),
            checks_added_by_coldfix=origins["added_by_coldfix"],
            already_enforced_by_fix=origins["already_enforced_by_fix"]),
        running=sorted(name for lock, name in LOCKS.items() if held(root / lock)),
        failing=failing,
        awaiting_approval=[key for key, _ in transcript_delivery.waiting(root)],
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", required=True)
    parser.add_argument("--lessons", required=True, help="shared lessons folder")
    parser.add_argument("--scan-ledger", required=True, help="transcript scan ledger (transcripts.sqlite)")
    args = parser.parse_args(argv)
    folder = c.ordinary_path(args.lessons)
    if not folder.is_dir():
        parser.error("lessons folder not found")
    root, store = c.load_store(args.store)
    print(json.dumps(summary(root, store, folder, c.ordinary_path(args.scan_ledger)), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
