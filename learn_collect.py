#!/usr/bin/env python3
"""Ingest new failed verify runs and Coldbox receipts into a private store. Makes no model calls."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import coldfix as c
import failure_ingest as fi
import codex_review_source as review_source
import github_sources

HERE = Path(__file__).resolve().parent
SETTLE_SECONDS = 60
PARSER = hashlib.sha256(b"".join((HERE / name).read_bytes() for name in ("coldfix.py", "failure_ingest.py"))).hexdigest()
RECEIPT_ONLY_REJECTION = "coldbox receipt repo must be owner/repo; nothing imported"


def reviews(root, repos):
    github_sources.private_store(root)
    total = dict(imported=0, deduped=0, unavailable=0)
    for repo in repos:
        try:
            events = []
            for row in github_sources.GitHub(repo).pages("pulls/comments"):
                if row.get("in_reply_to_id") or not review_source.is_bot(row.get("user")) or review_source.MARKER not in (row.get("body") or ""):
                    continue
                url = row["html_url"]
                event = dict(kind="review", command="PR review finding", error=row["body"], repo=repo,
                             agent="codex-review", source_ref=url, occurred=row["created_at"])
                events.append(c.event_record(event, "github"))
            counts = import_reviews(root, events)
            total["imported"] += counts["imported"]
            total["deduped"] += counts["deduped"]
        except fi.SOURCE_ERRORS as error:
            total["unavailable"] += 1
            total.setdefault("reason", {})[repo] = type(error).__name__
    return total


def import_reviews(root, records):
    with c.lesson_lock(root):
        _, store = c.load_store(root)
        seen = {row.get("source_ref") for row in store["failures"].values()}
        imported = 0
        for row in records:
            if row["source_ref"] not in seen:
                c.save(root, "failures", row)
                seen.add(row["source_ref"])
                imported += 1
    return dict(imported=imported, deduped=len(records) - imported)


def provisional(path):
    try:
        row = json.loads(path.read_text())
        vm = row.get("vm")
        return row.get("verdict") == "incomplete" and isinstance(vm, dict) and "destroyed_at" not in vm
    except (OSError, ValueError, AttributeError, RecursionError):
        return False


def receipts(root, directory, now=None, on_reject=None):
    now = time.time() if now is None else now
    ledger = root / "collected-receipts.json"
    seen = json.loads(ledger.read_text()) if ledger.exists() else {}
    if isinstance(seen, list):
        seen = dict.fromkeys(seen, "imported")
    total = dict(imported=0, skipped=0, rejected=0, known_invalid=0)
    for path in sorted(Path(directory).glob("*.json")):
        if seen.get(path.name) == "imported" or now - path.stat().st_mtime < SETTLE_SECONDS or provisional(path):
            continue
        try:
            invalid = dict(sha256=hashlib.sha256(path.read_bytes()).hexdigest(), parser=PARSER)
        except OSError:
            invalid = None
        if invalid and isinstance(seen.get(path.name), dict) and seen[path.name].get("invalid") == invalid:
            total["known_invalid"] += 1
            continue
        done = subprocess.run([sys.executable, str(HERE / "coldfix.py"), "--store", str(root), "ingest",
                               "--format", "coldbox", "--input", str(path)], capture_output=True, text=True)
        if done.returncode:
            reason = done.stderr.strip().removeprefix("coldfix: ")
            if on_reject is not None:
                on_reject(reason)
            total["rejected"] += 1
            if invalid and reason == RECEIPT_ONLY_REJECTION:
                seen[path.name] = dict(reason=reason, invalid=invalid)
                c.atomic_create(ledger, c.encoded(seen), replace=True)
            continue
        result = json.loads(done.stdout)
        total["imported"] += result["imported_records"]
        total["skipped"] += result["skipped_duplicates"]
        seen[path.name] = "imported"
        c.atomic_create(ledger, c.encoded(seen), replace=True)
    return total


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", required=True)
    parser.add_argument("--owner", required=True, help="discover this owner's private repositories")
    parser.add_argument("--receipts", default="/var/lib/coldbox/receipts", help="local receipt directory; empty to skip")
    parser.add_argument("--workflow", default="verify")
    parser.add_argument("--limit", type=int, default=30, help="recent completed runs listed per repository")
    args = parser.parse_args(argv)
    root = c.ordinary_path(args.store)
    root.mkdir(mode=0o700, exist_ok=True)
    result, unreachable = collect(root, args.owner, args.receipts, args.workflow, args.limit)
    print(json.dumps(result, sort_keys=True))
    return 2 if unreachable else 0


def collect(root, owner, receipt_dir="", workflow="verify", limit=30):
    from ci_fix_learning import discover

    result = {}
    if receipt_dir:
        result["coldbox"] = receipts(root, receipt_dir) if Path(receipt_dir).is_dir() else dict(missing=receipt_dir)
    if os.environ.get("COLDFIX_SOURCES"):
        from collect_sources import collect as collect_sources
        try:
            result["sources"] = collect_sources(root, os.environ["COLDFIX_SOURCES"])
        except (OSError, ValueError, TypeError) as error:
            result["sources"] = {"config": {"unavailable": 1, "reason": type(error).__name__}}
    try:
        repos = discover(owner)
        github = fi.poll(root, repos, limit, workflow)
        unreachable = bool(repos) and github.get("unlisted") == len(repos)
    except fi.SOURCE_ERRORS as error:
        github, unreachable = dict(discovery_failed=type(error).__name__), True
        repos = []
    result["github"] = github
    try:
        result["codex_review"] = reviews(root, repos) if repos else dict(imported=0, deduped=0, unavailable=int(unreachable), reason="repository discovery unavailable" if unreachable else None)
    except fi.SOURCE_ERRORS as error:
        result["codex_review"] = dict(imported=0, deduped=0, unavailable=1, reason=type(error).__name__)
    return result, unreachable


if __name__ == "__main__":
    sys.exit(main())
