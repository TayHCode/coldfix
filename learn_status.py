#!/usr/bin/env python3
"""Answer the seven North Star status questions from a private store. Makes no model calls; writes only the --page file."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import fcntl
import html
import json
import os
import re
import sqlite3
import sys

import check_delivery
from ci_batch import LEDGER as BATCH_LEDGER
import coldfix as c
import learn_daily
import learn_report
from learn_stall import SOURCE_LABELS
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


REASONS = (
    "a review finding is proven only with its flagged lines", "fix is not a descendant of the recorded failure",
    "pytest test changed between revisions", "pytest test inputs changed between revisions",
    "source repository checkout unavailable",
    "an invalidated review lesson is re-proven only under the flagged-lines rule", "check fails the corrected message",
    "check flags ordinary messages", "check misses the mistake", "code mistake needs repository replay",
    "corrected message does not apply the rule", "corrected message is not a later agent message",
    "incident cannot be exported", "incident not admitted", "invalid source repository", "jev", "jev check failed",
    "jev review failed", "lead too long to read whole", "missing_upstream_fix",
    "no agent message shows the mistake", "no change between the replay revisions for the requested paths",
    "no changed unittest matches the recorded failure", "no descendant contains a unittest matching the recorded failure",
    "no earlier turns before the mistake", "no exact pair", "no fix exists",
    "no fixed review pair or descendant test matches the recorded CI failure", "no recorded failing command",
    "not_replayable_correction_only",
    "private text in lesson", "recorded commit not available on GitHub", "recorded failure has no commit",
    "recorded pytest failure has no assertion message", "recorded verify entrypoint test inputs cannot be pinned",
    "replay with the lesson does not apply the rule",
    "the fix does not change the finding's flagged lines",
    "the fix's edit of the finding's flagged lines also adds or changes unflagged lines",
)


def reason_group(text):
    text = re.sub(r"^[0-9a-f]{32}: ", "", text or "")
    group = re.split(r"[;:]", text, maxsplit=1)[0].strip()
    return group if group in REASONS else "other"


def draft_state(row):
    replay = row.get("replay") or {}
    if replay.get("status") == "proven":
        return "proven", None
    if replay.get("status") == "rejected":
        return "rejected", reason_group(replay.get("reason"))
    if row.get("verdict") == "FAIL":
        if row.get("reason", "").startswith("duplicate of "):
            return "duplicate", None
        return "rejected", "privacy screen" if row.get("reason") == "screen_rejected" else "failed draft review"
    return "waiting", None


def funnel(drafts, leads, records, files):
    states, rejected = [], []
    for row in drafts:
        state, reason = draft_state(row)
        states.append(state)
        if reason:
            rejected.append(reason)
    for lead in leads:
        if lead["status"] == "no_lesson":
            continue
        if lead["status"] in ("rejected", "unprovable"):
            states.append("rejected")
            rejected.append(reason_group(lead.get("reason")))
        else:
            states.append(lead["status"] if lead["status"] in ("proven", "duplicate") else "waiting")
    counts = Counter(states)
    withdrawn = ({m.get("id") for m in files if m.get("status") in c.ENDED and m.get("retired_by") == "recheck"}
                 | {entry.get("lesson") or key for key, entry in records if entry.get("status") in ("withdrawn", "vetoed")})
    return dict(found=len(states), proven=counts["proven"], rejected=dict(Counter(rejected)),
                waiting=counts["waiting"], duplicate=counts["duplicate"],
                retired=sum(m.get("status") in c.ENDED and m.get("retired_by") != "recheck" for m in files),
                withdrawn=len(withdrawn))


def summary(root, store, folder, scan_ledger, oss_store=None, backlog_store=None, peer_roots=()):
    drafts = transcript_delivery.load(root / learn_daily.LEDGER, dict(drafts={}, runs=[]))
    leads = transcript_delivery.load(root / transcript_delivery.LEDGER, dict(leads={}, batches=[]))
    delivered = transcript_delivery.load(root / transcript_delivery.DELIVERIES, dict(leads={}))
    deliveries = delivered["leads"]
    checks = check_delivery.records(root)
    lessons = c.latest_lessons(store).values()
    outside = c.latest_lessons(oss_store).values() if oss_store else []
    backlog = c.latest_lessons(backlog_store).values() if backlog_store else []
    files = list(transcript_delivery.lessons(folder).values())
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
                      outside_repos=sum(row["verification"] == "proven" for row in outside),
                      own_repo_ci_backlog=sum(row["verification"] == "proven" for row in backlog),
                      transcript=lead_states["proven"]),
        active=dict(shared_lessons=sorted((m["id"] for m in files if m.get("status") in ACTIVE),
                                          key=lambda key: int(key[1:])),
                    ci_and_review=sum(row["status"] == "active" for row in lessons),
                    outside_repos=sum(row["status"] == "active" for row in outside),
                    own_repo_ci_backlog=sum(row["status"] == "active" for row in backlog)),
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
        awaiting_approval=[key for key, _ in transcript_delivery.waiting(root)] + [key for key, _ in transcript_delivery.held(folder)],
        lessons=funnel([*drafts["drafts"].values(), *(row for peer in peer_roots for row in
                        transcript_delivery.load(peer / learn_daily.LEDGER, dict(drafts={}))["drafts"].values())],
                       leads["leads"].values(),
                       [*deliveries.items(), *delivered.get("proven", {}).items()], files),
        projects=dict(sorted(Counter(repo.lower() for m in files if m.get("status") in ACTIVE for repo in m["repos"]).items())),
    )


def repeats(cohorts):
    if not cohorts:
        return ""
    briefed, held = cohorts["in_briefing"], cohorts["held_back"]
    return f"""<h2>Repeats of the lesson's own failure after proof</h2>
<p>In briefing {briefed['repeats']:,}/{briefed['lessons']:,} vs held back {held['repeats']:,}/{held['lessons']:,}</p>
<p>Each fraction shows the share of current briefing-only lessons with a matching failure in their repository in the past 14 days.</p>"""


def page(value, proven_from_transcripts=0):
    lessons = value["lessons"]
    def rows(items):
        return "".join(f"<tr><th scope=\"row\">{html.escape(str(key))}</th><td>{count:,}</td></tr>" for key, count in items) \
            or "<tr><td colspan=\"2\">None</td></tr>"
    totals = [("Found", lessons["found"]), ("Proven", lessons["proven"]), ("Rejected", sum(lessons["rejected"].values())),
              ("Waiting", lessons["waiting"]), ("Merged as duplicates", lessons["duplicate"])]
    after = [("Proven from transcripts", proven_from_transcripts), ("Enforced", len(value["prevented_or_fixed"]["enforced"])),
             ("Retired", lessons["retired"]), ("Withdrawn", lessons["withdrawn"])]
    reasons = sorted(lessons["rejected"].items(), key=lambda row: (-row[1], row[0]))
    projects = sorted(value["projects"].items(), key=lambda row: (-row[1], row[0]))
    sources = value.get("sources", {})
    source_rows = "".join(f'<tr><th scope="row">{html.escape(name)}</th><td>{html.escape(state)}</td></tr>'
                          for name, state in sources.items())
    source_table = f"<h2>Sources</h2><table><tbody>{source_rows}</tbody></table>" if source_rows else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Coldfix status</title>
<style>body{{margin:0;font:16px/1.5 system-ui,sans-serif;background:#fff;color:#111}}main{{max-width:760px;margin:auto;padding:24px 16px}}
table{{width:100%;border-collapse:collapse;margin-bottom:24px}}th,td{{padding:6px 0;border-bottom:1px solid #ddd}}th{{text-align:left;font-weight:400}}
td{{text-align:right;font-variant-numeric:tabular-nums}}p{{color:#555}}
@media(prefers-color-scheme:dark){{body{{background:#111;color:#eee}}th,td{{border-color:#333}}p{{color:#aaa}}}}</style></head>
<body><main><h1>What Coldfix has learned</h1>
<p>Updated {datetime.now(timezone.utc):%d %b %Y %H:%M} UTC, all time.</p>
{source_table}
<h2>Lessons</h2><table><tbody>{rows(totals)}</tbody></table>
<p>Found counts CI and review drafts (own, backlog and outside repositories) plus chat leads that held a lesson; each is in exactly one of proven, rejected, waiting or merged as a duplicate.</p>
<h2>After delivery</h2><table><tbody>{rows(after)}</tbody></table>
<p>Delivered lessons, counted apart from the states above. Enforced lessons have their check on the repository's default branch, so agents are no longer briefed on them. Retired lessons were fixed, superseded or contradicted. Withdrawn lessons were taken back by recheck, a veto or a later replay.</p>
<h2>Rejected, by reason</h2><table><tbody>{rows(reasons)}</tbody></table>
<h2>Projects that get lessons</h2><table><tbody>{rows(projects)}</tbody></table>
<p>Count of in-force shared lessons per project.</p>{repeats(value.get("briefing_recurrence"))}</main></body></html>
"""


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", required=True)
    parser.add_argument("--lessons", required=True, help="shared lessons folder")
    parser.add_argument("--scan-ledger", required=True, help="transcript scan ledger (transcripts.sqlite)")
    parser.add_argument("--oss-store", help="open-source store whose lessons count as outside repos, read-only")
    parser.add_argument("--backlog-store", help="own-repo CI backlog store whose lessons are counted, read-only")
    parser.add_argument("--page", help="also write the lesson counts to this static HTML file")
    parser.add_argument("--source-states", help="JSON object of source states from the scheduler")
    args = parser.parse_args(argv)
    folder = c.ordinary_path(args.lessons)
    if not folder.is_dir():
        parser.error("lessons folder not found")
    if args.oss_store and not c.ordinary_path(args.oss_store).is_dir():
        parser.error("OSS store not found")
    if args.backlog_store and not c.ordinary_path(args.backlog_store).is_dir():
        parser.error("backlog store not found")
    root, store = c.load_store(args.store)
    oss_root, oss_store = c.load_store(args.oss_store) if args.oss_store else (None, None)
    backlog_root, backlog_store = c.load_store(args.backlog_store) if args.backlog_store else (None, None)
    value = summary(root, store, folder, c.ordinary_path(args.scan_ledger), oss_store, backlog_store,
                    [peer for peer in (oss_root, backlog_root) if peer])
    sources = {}
    if args.source_states is not None:
        try:
            sources = json.loads(args.source_states)
        except (ValueError, RecursionError):
            parser.error("invalid source state JSON")
        if not isinstance(sources, dict) or any(not isinstance(state, str) for state in sources.values()):
            parser.error("source states must be an object of strings")
        if not sources:
            sources = dict.fromkeys(SOURCE_LABELS.values(), "Idle. Scheduler state unavailable")
    if oss_root:
        reason = transcript_delivery.load(oss_root / BATCH_LEDGER, {}).get("oss_pause", {}).get("reason")
        if reason:
            sources["OSS"] = "Paused. " + reason
    if sources:
        value["sources"] = sources
    value["briefing_recurrence"] = learn_report.briefing_cohorts(folder, store, datetime.now(timezone.utc), backlog_store)
    if args.page:
        proven_from_transcripts = sum(m.get("status") == "active"
                                      and m.get("source", "").startswith(transcript_delivery.TRANSCRIPT_PROVEN_SOURCE)
                                      for m in transcript_delivery.lessons(folder).values())
        c.atomic_create(args.page, page(value, proven_from_transcripts), replace=True)
    print(json.dumps(value, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
