"""Read-only, text-free summaries of one validated private store."""

import json
import re

import coldfix as c


SOURCES = ("github", "t3", "other_unknown")


def event_source(row):
    ref = row.get("source_ref", "")
    if re.fullmatch(r"t3:project:[^:\s]+:thread:[^:\s]+:item:[^:\s]+", ref):
        return "t3"
    if re.fullmatch(r"t3:thread:[^:\s]+:blocker:[^:\s]+:(?:partial|blocked)", ref):
        return "t3"
    if re.fullmatch(
        r"https://github\.com/[^/?#\s]+/[^/?#\s]+/"
        r"(?:pull/[1-9][0-9]*|issues/[1-9][0-9]*|commit/[0-9a-fA-F]{40,64}|"
        r"actions/runs/[1-9][0-9]*(?:/job/[1-9][0-9]*)?)(?:#[^\s]*)?", ref
    ) or re.fullmatch(r"https://api\.github\.com/repos/[^/?#\s]+/[^/?#\s]+/check-runs/[1-9][0-9]*", ref):
        return "github"
    return "other_unknown"


def completion(root, store):
    marker_path = c.ordinary_path(root / "run.json")
    if not marker_path.exists():
        return None
    marker = json.loads(c.read_text(marker_path), object_pairs_hook=c.unique_object)
    c.require(isinstance(marker, dict) and set(marker) ==
              {"version", "run_id", "report_id", "summary", "inputs_sha256"}
              and type(marker["version"]) is int and marker["version"] == 1, "invalid run marker")
    report = store["measurements"][marker["report_id"]]
    run_id = marker["run_id"]
    c.require(c.ID.fullmatch(c.text(run_id, "run ID")), "invalid run ID")
    c.require(dict(run_id=run_id, complete=True, calls=[], evidence_ref="coldfix:run:" + run_id)
              in report["input"]["model_runs"] and report["outcomes"]
              and marker["inputs_sha256"] == c.digest(c.read_text(root / "inputs.json")), "invalid run binding")
    snapshot = report["snapshot"]
    expected = dict(events=len(snapshot["failures"]),
                    groups=len({c.signature(store["failures"][ref]) for ref in snapshot["failures"]}),
                    lessons=len({store["lessons"][ref]["lesson_id"] for ref in snapshot["lessons"]}),
                    receipts=len(snapshot["receipts"]), outcomes=len(report["outcomes"]),
                    model_calls=0, status=report["status"])
    c.require(marker["summary"] == expected, "invalid run summary")
    return dict(time=report["created"], basis="measurement_report_created", status=report["status"])


def status(path):
    try:
        root, store = c.load_store(path)
        scope = "direct_store"
        completed = completion(root, store)
        if not any((root / key).exists() for key in c.FIELDS) and completed is None:
            runs = c.ordinary_path(root / "runs")
            c.require(not runs.exists() or runs.is_dir(), "invalid runs directory")
            candidates = []
            for attempt in sorted(runs.glob("run-*")):
                attempt = c.ordinary_path(attempt)
                marker = c.ordinary_path(attempt / "run.json")
                if marker.exists():
                    candidate_root, candidate = c.load_store(attempt)
                    done = completion(candidate_root, candidate)
                    candidates.append((c.time_key(done["time"]), attempt.name, candidate, done))
            if candidates:
                _, _, store, completed = max(candidates, key=lambda row: row[:2])
                scope = "latest_completed_run"
            else:
                scope = "empty"
        return summarize(store, scope, completed)
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise c.ColdfixError("cannot summarize invalid status store; left unchanged") from None


def summarize(store, scope, completed):
    failures = store["failures"]
    sources = {ref: event_source(row) for ref, row in failures.items()}
    by_source = {source: dict(events=0, groups=0, lessons=0, outcomes=0) for source in SOURCES}

    def linked(refs):
        c.require(all(ref in sources for ref in refs), "missing status evidence")
        return sorted({sources[ref] for ref in refs}) or ["other_unknown"]

    def tally(kind, attribution):
        for source in attribution:
            by_source[source][kind] += 1

    groups = {}
    for ref, row in failures.items():
        tally("events", [sources[ref]])
        groups.setdefault(c.signature(row), []).append(ref)
    for refs in groups.values():
        tally("groups", linked(refs))
    lessons = c.latest_lessons(store)
    states = []
    for row in sorted(lessons.values(), key=lambda row: row["lesson_id"]):
        attribution = linked(row["evidence"])
        tally("lessons", attribution)
        states.append(dict(lesson_id=row["lesson_id"], revision=row["id"], status=row["status"],
                           verification=row["verification"], level=row["level"], sources=attribution))
    for row in store["outcomes"].values():
        lesson = store["lessons"][row["lesson_revision"]]
        c.require(lesson["lesson_id"] == row["lesson_id"], "mismatched outcome lesson")
        refs = set(lesson["evidence"])
        for key, values in row["evidence"].items():
            if key == "duplicates":
                refs.update(ref for group in values for ref in group)
            else:
                refs.update(values)
        tally("outcomes", linked(refs))
    top = [dict(signature=key, events=len(refs), sources=linked(refs))
           for key, refs in sorted(groups.items(), key=lambda item: (-len(item[1]), item[0])) if len(refs) >= 2][:5]
    return dict(scope=scope, totals=dict(events=len(failures), groups=len(groups), lessons=len(lessons),
                                        outcomes=len(store["outcomes"])), by_source=by_source,
                attribution="Groups, latest lessons, and outcomes count once per linked source; source totals may overlap.",
                top_recurring_groups=top, latest_lessons=states, last_completed_run=completed)


def terminal(value):
    totals = value["totals"]
    lines = ["Coldfix status (" + value["scope"] + ")",
             "  ".join(f"{key}: {count}" for key, count in totals.items()),
             "Source          Events  Groups  Lessons  Outcomes"]
    for source, counts in value["by_source"].items():
        lines.append(f"{source:<15} {counts['events']:>6}  {counts['groups']:>6}  {counts['lessons']:>7}  {counts['outcomes']:>8}")
    lines.append(value["attribution"])
    lines.append("Top recurring normalized groups (up to 5):")
    lines.extend(f"  {row['signature']}  events={row['events']}  sources={','.join(row['sources'])}"
                 for row in value["top_recurring_groups"])
    if not value["top_recurring_groups"]:
        lines.append("  none")
    lines.append("Latest lesson states:")
    lines.extend(f"  {row['lesson_id']}  revision={row['revision']}  {row['status']}  {row['verification']}  {row['level']}"
                 for row in value["latest_lessons"])
    if not value["latest_lessons"]:
        lines.append("  none")
    done = value["last_completed_run"]
    lines.append("Last completed run: " + (done["time"] + " (measurement report creation; " + done["status"] + ")"
                                            if done else "unknown (no completion marker in scope)"))
    return "\n".join(lines)
