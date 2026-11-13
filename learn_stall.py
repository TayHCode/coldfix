#!/usr/bin/env python3
"""Append an agentfleet alert note when Coldfix learning stalls. Reads `journalctl -o json` lines on stdin: the
oldest user-journal entry, which marks how far back the journal reaches, then this host's collect, daily,
transcript, backlog and OSS units, plus the start-job results of every coldfix unit: a unit whose latest run failed
is an alert. The collect unit also carries the hourly check-delivery summary. Pipes new STALLED or RECOVERED notes
into the command given. With --issues, keeps one open issue labelled stall-alert per active alert in that GitHub
repository, recorded next to the state file, and closes it when the alert recovers."""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

UNITS = {"coldfix-collect.service", "coldfix-daily.service", "coldfix-transcripts.service"}
BACKLOGS = {"coldfix-backlog.service", "coldfix-oss.service"}
STREAK = 3
LABEL = "stall-alert"
FAILED = ("failed", "unavailable", "missing", "error", "errors", "discovery_failed", "unlisted")
INSPECT = (f"Inspect: `ssh {platform.node()} journalctl --user -u coldfix-collect -u coldfix-transcripts "
           "-u coldfix-backlog -u coldfix-oss -n 80`.")


def entries(lines):
    for line in lines:
        try:
            entry = json.loads(line)
            yield datetime.fromtimestamp(int(entry["__REALTIME_TIMESTAMP"]) / 1e6, timezone.utc), entry
        except (ValueError, KeyError, TypeError):
            continue


def summary(entry):
    if entry.get("_SYSTEMD_USER_UNIT") not in UNITS or not isinstance(entry.get("MESSAGE"), str):
        return None, None
    try:
        row = json.loads(entry["MESSAGE"])
    except ValueError:
        return None, None
    if not isinstance(row, dict):
        return None, None
    if isinstance(row.get("check_delivery"), dict):
        return "delivery", row["check_delivery"]
    if "sources" in row:
        return "collect", row
    if "day" in row:
        return "daily", row
    if entry["_SYSTEMD_USER_UNIT"] == "coldfix-transcripts.service" and "scanned" in row:
        return "transcripts", row
    return None, None


def pause(entry):
    unit, message = entry.get("_SYSTEMD_USER_UNIT"), entry.get("MESSAGE")
    if not isinstance(message, str):
        return None
    if unit in BACKLOGS and message.startswith("ci-backlog"):
        return unit, message.removeprefix("ci-backlog ") if message.startswith("ci-backlog paused") else None
    if unit == "coldfix-transcripts.service":
        try:
            row = json.loads(message)
        except ValueError:
            return None
        if isinstance(row, dict) and ("paused" in row or "scanned" in row):
            return unit, "paused: " + str(row["paused"]) if "paused" in row else None
    return None


def finished(journal):
    outcomes, lines = {}, {}
    for entry in journal:
        own, message, unit = entry.get("_SYSTEMD_USER_UNIT"), entry.get("MESSAGE"), entry.get("USER_UNIT")
        if isinstance(own, str) and own.startswith("coldfix-") and isinstance(message, str) and message.strip():
            lines[own] = message.strip()
        if (isinstance(unit, str) and unit.startswith("coldfix-") and entry.get("JOB_TYPE") == "start"
                and entry.get("JOB_RESULT") in ("done", "failed", "dependency", "timeout")):
            outcomes[unit] = entry["JOB_RESULT"], lines.get(unit, "")
    return outcomes


def parts(kind, row):
    if kind != "collect":
        return {"learn_daily" if kind == "daily" else "transcripts": row}
    return {name: part for name, part in [*row.items(), *row["sources"].items()]
            if name != "sources" and isinstance(part, dict)}


def stalls(journal, now):
    covered = journal[0][0] if journal else now
    rows = [(time, *summary(entry)) for time, entry in journal]
    rows = [row for row in rows if row[1]]

    def total(kinds, hours, count):
        return sum(count(k, row) for time, k, row in rows if k in kinds and time >= now - timedelta(hours=hours))

    found, judged = {}, set()
    for key, kinds, hours, count, message in (
            ("imports", ("collect", "transcripts"), 6,
             lambda kind, row: sum(part.get("imported", 0) for part in parts(kind, row).values()),
             "0 failures imported from every source in 6 h"),
            ("proven", ("daily",), 24, lambda kind, row: row.get("replay", {}).get("proven", 0),
             "0 lessons proven in 24 h"),
            ("transcripts", ("transcripts",), 6, lambda kind, row: row["scanned"], "0 transcripts scanned in 6 h"),
            ("transcript_proven", ("transcripts",), 24, lambda kind, row: row.get("proven", 0),
             "0 transcript lessons proven in 24 h")):
        if now - covered >= timedelta(hours=hours):
            judged.add(key)
            if total(kinds, hours, count) == 0:
                found[key] = message
    deliveries = [row for _, kind, row in rows if kind == "delivery"]
    if deliveries:
        judged.add("undelivered")
        overdue = deliveries[-1].get("overdue")
        if isinstance(overdue, int) and overdue > 0:
            found["undelivered"] = (f"{overdue} proven lessons are still prose only, with no check in their "
                                      "repository, 24 h after proof")
    for kind in ("collect", "daily", "transcripts"):
        recent = [row for _, k, row in rows if k == kind][-STREAK:]
        if len(recent) == STREAK:
            judged.update(f"failing:{kind}:{name}" for name, part in parts(kind, recent[-1]).items()
                          if not any(part.get(key) for key in FAILED))
            failing = [{name for name, part in parts(kind, row).items() if any(part.get(key) for key in FAILED)}
                       for row in recent]
            for name in set.intersection(*failing):
                found[f"failing:{kind}:{name}"] = f"{name} failed in {STREAK} {kind} runs in a row"
    for unit, (outcome, line) in finished(entry for _, entry in journal).items():
        key = "failed:" + unit.removesuffix(".service")
        judged.add(key)
        if outcome != "done":
            found[key] = f"{unit.removesuffix('.service')} failed its last run; its last line: {line[:300]}"
    latest = dict(state for state in map(pause, (entry for _, entry in journal)) if state)
    for unit, reason in latest.items():
        key = "paused:" + unit.removesuffix(".service")
        judged.add(key)
        if reason:
            found[key] = f"{unit.removesuffix('.service')} {reason}"
    return found, judged


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + ".tmp")
    pending.write_text(json.dumps(value, sort_keys=True))
    os.replace(pending, path)


def track(repo, found, judged, stamp, path):
    def gh(*words):
        return subprocess.run(["gh", *words, "-R", repo], text=True, capture_output=True, check=True,
                              timeout=60).stdout.strip()

    issues = json.loads(path.read_text()) if path.exists() else {}
    for key in sorted((issues.keys() - found.keys()) & judged):
        gh("issue", "close", str(issues.pop(key)), "--comment",
           f"RECOVERED coldfix on {platform.node()} at {stamp}: {key} no longer holds.")
        save(path, issues)
    missing = [key for key in sorted(found) if key not in issues
               or gh("issue", "view", str(issues[key]), "--json", "state", "--jq", ".state") != "OPEN"]
    listed = json.loads(gh("issue", "list", "--label", LABEL, "--state", "open", "--limit", "100",
                           "--json", "number,title")) if missing else []
    open_titles = {issue["title"]: issue["number"] for issue in sorted(listed, key=lambda issue: -issue["number"])}
    for key in missing:
        title = f"STALLED coldfix on {platform.node()}: {key}"
        if open_titles.get(title, issues.get(key)) != issues.get(key):
            issues[key] = open_titles[title]
        else:
            url = gh("issue", "create", "--title", title, "--label", LABEL,
                     "--body", f"STALLED coldfix on {platform.node()} at {stamp}: {found[key]}.\n\n{INSPECT}\n\n"
                               "`learn_stall.py` closes this issue when the condition clears.")
            issues[key] = int(url.rsplit("/", 1)[-1])
        save(path, issues)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--issues", metavar="OWNER/REPO")
    parser.add_argument("deliver", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    deliver = args.deliver[1:] if args.deliver[:1] == ["--"] else args.deliver
    now = datetime.now(timezone.utc)
    active = json.loads(args.state.read_text()).get("active", {}) if args.state.exists() else {}
    found, judged = stalls(sorted(entries(sys.stdin), key=lambda item: item[0]), now)
    found.update({key: message for key, message in active.items() if key not in judged})
    stamp = now.strftime("%Y-%m-%dT%H:%MZ")
    lines = [f"STALLED coldfix on {platform.node()} at {stamp}: {found[key]}. {INSPECT}" for key in sorted(found.keys() - active.keys())]
    lines += [f"RECOVERED coldfix on {platform.node()} at {stamp}: {active[key]} no longer holds."
              for key in sorted(active.keys() - found.keys())]
    note = "".join(f"\n### {now:%Y-%m-%d} · agentfleet-alert\n{line}\n" for line in lines)
    untracked = None
    if args.issues:
        try:
            track(args.issues, found, judged, stamp, args.state.with_name(args.state.stem + "-issues.json"))
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            untracked = error
    if note and deliver:
        subprocess.run(deliver, input=note, text=True, check=True, timeout=60)
    elif note:
        sys.stdout.write(note)
    save(args.state, dict(active=found))
    print(json.dumps(dict(active=sorted(found), alerted=len(lines))), file=sys.stderr)
    if untracked:
        print(f"issue tracking failed: {untracked} {getattr(untracked, 'stderr', '')}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
