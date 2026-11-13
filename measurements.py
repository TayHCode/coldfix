"""Private, deterministic measurements over a validated Coldfix snapshot."""

from collections import Counter
from datetime import timedelta
import json

from coldfix import (ColdfixError, GROUP, ID, digest, encoded,
                     latest_lessons, new_record, occurred, ordinary_path, read_text,
                     receipt_id, require, save, signature, LIVE, STATUSES, text, time_key, unique_object)


BASE = ("failures", "proposals", "lessons", "receipts")
INPUTS = {"activations", "expected_sources", "coverage", "extraction", "pairs", "privacy", "model_runs"}
OUTCOME_FIELDS = {"run_id", "lesson_id", "lesson_revision", "latest_revision", "latest_status", "grouping",
                  "group_id", "activation", "window", "recurrences", "observed", "evidence", "replay_result",
                  "status", "gaps"}
REPORT_FIELDS = {"run_id", "snapshot", "snapshot_sha256", "input", "input_sha256", "window_days", "outcomes",
                 "sources", "extraction", "dedup", "privacy", "model_calls", "status", "basis"}


def fields(row, names):
    require(isinstance(row, dict) and set(row) == set(names.split()), "invalid measurement evidence fields")


def boolean(value):
    require(type(value) is bool, "measurement label must be boolean")


def count(value):
    require(type(value) is int and value >= 0, "measurement count must be a nonnegative integer")


def refs(values, store):
    require(isinstance(values, list) and len(set(values)) == len(values), "invalid measurement event IDs")
    require(all(isinstance(v, str) and v in store["failures"] for v in values), "unknown measurement event ID")


def interval(value):
    occurred(value)
    start = time_key(value)
    return start, start + timedelta(days=1) if len(value) == 10 else start


def validate_input(value, store):
    require(isinstance(value, dict) and set(value) <= INPUTS | {"version"}
            and type(value.get("version")) is int and value["version"] == 1, "invalid measurement input")
    value = {"version": 1, **{key: value.get(key, []) for key in sorted(INPUTS)}}
    require(all(isinstance(value[key], list) for key in INPUTS), "measurement evidence must be arrays")
    require(all(isinstance(s, str) and s.strip() for s in value["expected_sources"])
            and len(set(value["expected_sources"])) == len(value["expected_sources"]), "invalid expected sources")
    seen = set()
    for row in value["activations"]:
        fields(row, "lesson_revision at commit evidence_ref")
        revision = row["lesson_revision"]
        require(isinstance(revision, str) and revision in store["lessons"] and revision not in seen,
                "unknown or duplicate activation revision")
        seen.add(revision)
        lesson = store["lessons"][revision]
        require(lesson["status"] in LIVE and lesson["verification"] == "proven",
                "activation requires a proven active or enforced revision")
        interval(row["at"])
        require(isinstance(row["commit"], str) and len(row["commit"]) in (40, 64)
                and all(c in "0123456789abcdef" for c in row["commit"]), "invalid activation commit")
        text(row["evidence_ref"], "activation evidence")
    seen = set()
    for row in value["coverage"]:
        fields(row, "source repos since until items_read complete gaps evidence_ref")
        text(row["source"], "source")
        require(row["source"] in value["expected_sources"] and row["source"] not in seen,
                "unexpected or duplicate coverage source")
        seen.add(row["source"])
        require(isinstance(row["repos"], list) and row["repos"]
                and all(isinstance(r, str) and r.strip() for r in row["repos"]), "invalid coverage repos")
        require(interval(row["since"])[0] < interval(row["until"])[0], "invalid coverage interval")
        if row["items_read"] is not None:
            count(row["items_read"])
        boolean(row["complete"])
        require(isinstance(row["gaps"], list), "invalid collection gaps")
        for gap in row["gaps"]:
            text(gap, "collection gap")
        text(row["evidence_ref"], "coverage evidence")
    seen = set()
    for row in value["extraction"]:
        fields(row, "source source_ref expected")
        text(row["source"], "source")
        text(row["source_ref"], "sample source reference")
        key = (row["source"], row["source_ref"])
        require(key not in seen, "duplicate extraction sample item")
        seen.add(key)
        boolean(row["expected"])
    seen = set()
    for row in value["pairs"]:
        fields(row, "left right same")
        refs([row["left"], row["right"]], store)
        boolean(row["same"])
        key = tuple(sorted((row["left"], row["right"])))
        require(key not in seen, "duplicate labeled pair")
        seen.add(key)
    seen = set()
    for row in value["privacy"]:
        fields(row, "case_id blocked leaks evidence_ref")
        text(row["case_id"], "privacy case ID")
        require(row["case_id"] not in seen, "duplicate privacy case")
        seen.add(row["case_id"])
        boolean(row["blocked"])
        count(row["leaks"])
        text(row["evidence_ref"], "privacy evidence")
    seen = set()
    for row in value["model_runs"]:
        fields(row, "run_id complete calls evidence_ref")
        text(row["run_id"], "model run ID")
        require(row["run_id"] not in seen, "duplicate model run")
        seen.add(row["run_id"])
        boolean(row["complete"])
        text(row["evidence_ref"], "model run evidence")
        require(isinstance(row["calls"], list), "invalid model calls")
        call_refs = set()
        for call in row["calls"]:
            fields(call, "role resolved_target evidence_ref")
            for key in call:
                text(call[key], "model call evidence")
            require(call["evidence_ref"] not in call_refs, "duplicate model call")
            call_refs.add(call["evidence_ref"])
    return value


def source_inventory(store, data):
    sources = {}
    for row in store["failures"].values():
        sources.setdefault(row["source"], []).append(row)
    inventory = [dict(source=source, records=len(rows),
                      dated=sum("occurred" in row for row in rows),
                      kinds=dict(sorted(Counter(r.get("kind", "unknown") for r in rows).items())))
                 for source, rows in sorted(sources.items())]
    coverage = {r["source"]: r for r in data["coverage"]}
    complete = bool(data["expected_sources"]) and all(
        source in coverage and coverage[source]["complete"] and not coverage[source]["gaps"]
        and coverage[source]["items_read"] is not None for source in data["expected_sources"])
    return dict(status="PASS" if complete else "INCOMPLETE", inventory=inventory,
                expected_sources=data["expected_sources"], coverage=data["coverage"],
                basis="Caller-attested source inventory and coverage; stored records are not items read.")


def outcome(store, data, lesson, activation, days, run_id, as_of):
    tip = latest_lessons(store)[lesson["lesson_id"]]
    receipt = store["receipts"].get(lesson["receipt"])
    replay = dict(status=receipt["status"] if receipt else "INCOMPLETE", receipt=lesson["receipt"],
                  verification=lesson["verification"],
                  before={k: receipt["before"][k] for k in ("commit", "tree", "status")} if receipt else None,
                  after={k: receipt["after"][k] for k in ("commit", "tree", "status")} if receipt else None)
    result = dict(run_id=run_id, lesson_id=lesson["lesson_id"], lesson_revision=lesson["id"],
                  latest_revision=tip["id"], latest_status=tip["status"], grouping=lesson["grouping"],
                  group_id=lesson["group_id"], activation=activation, window=None,
                  recurrences=dict(before=None, after=None), observed=dict(before=None, after=None),
                  evidence=dict(before=[], after=[], uncertain=[], undated=[], duplicates=[]),
                  replay_result=replay, status="INCOMPLETE", gaps=[])
    if activation is None:
        result["gaps"] = ["No explicit delivery/activation evidence. Replay's fixed commit is not activation."]
        return result
    start, end = interval(activation["at"])
    result["observed"] = dict(before=0, after=0)
    before, after = start - timedelta(days=days), end + timedelta(days=days)
    result["window"] = dict(days=days, before=[before.isoformat(), start.isoformat()],
                          after=[end.isoformat(), after.isoformat()], bounds="[start, end)",
                          activation_precision="date" if start != end else "timestamp")
    gaps = result["gaps"]
    revision_created = time_key(lesson["created"])
    if (start == end and start < revision_created) or (start != end and end <= revision_created):
        gaps.append("Activation predates the referenced lesson revision; delivery timing is invalid.")
    if after > time_key(as_of):
        gaps.append("After window has not elapsed.")
    successor = next((r for r in store["lessons"].values() if r["previous"] == lesson["id"]), None)
    if successor and time_key(successor["created"]) < after:
        gaps.append("A successor revision interrupts the after window; delivery continuity is unknown.")
    rows = [r for r in store["failures"].values()
            if (r["group_id"] if lesson["grouping"] == "exact" else signature(r)) == lesson["group_id"]]
    repos = {r["repo"].casefold() for r in rows}
    coverage = {r["source"]: r for r in data["coverage"]}
    if not data["expected_sources"]:
        gaps.append("Expected source inventory and collection coverage are unknown.")
    for source in data["expected_sources"]:
        c = coverage.get(source)
        if (not c or not c["complete"] or c["gaps"] or c["items_read"] is None
                or not repos <= {r.casefold() for r in c["repos"]}
                or interval(c["since"])[0] > before or interval(c["until"])[0] < after):
            gaps.append("Collection coverage is incomplete for source " + source)
    identities = {}
    for row in sorted(rows, key=lambda r: r["id"]):
        if "occurred" not in row:
            result["evidence"]["undated"].append(row["id"])
            continue
        identities.setdefault((row["repo"].casefold(), row["kind"], row["source_ref"]), []).append(row)
    for records in identities.values():
        ids = sorted(r["id"] for r in records)
        if len(ids) > 1:
            result["evidence"]["duplicates"].append(ids)
        if len({interval(r["occurred"]) for r in records}) > 1:
            result["evidence"]["uncertain"].extend(ids)
            continue
        lo, hi = interval(records[0]["occurred"])
        for side, left, right in (("before", before, start), ("after", end, after)):
            if left <= lo < right and (lo == hi or hi <= right):
                result["observed"][side] += 1
                result["evidence"][side].extend(ids)
            elif lo < right and hi > left:
                result["evidence"]["uncertain"].extend(ids)
    for key in ("uncertain", "undated"):
        result["evidence"][key] = sorted(set(result["evidence"][key]))
        if result["evidence"][key]:
            gaps.append("Matching events have " + key + " occurrence times.")
    for key in ("before", "after"):
        result["evidence"][key].sort()
    if not gaps:
        result.update(status="PASS", recurrences=dict(result["observed"]))
    return result


def extraction_scores(store, data):
    result = []
    sources = sorted(set(data["expected_sources"]) | {r["source"] for r in data["extraction"]})
    for source in sources:
        rows = [dict(r, event_ids=sorted(event["id"] for event in store["failures"].values()
                                        if event.get("source_ref") == r["source_ref"]))
                for r in data["extraction"] if r["source"] == source]
        totals = Counter((r["expected"], bool(r["event_ids"])) for r in rows)
        tp, fp, fn, tn = (totals[k] for k in ((True, True), (False, True), (True, False), (False, False)))
        precision = tp / (tp + fp) if tp + fp else None
        recall = tp / (tp + fn) if tp + fn else None
        result.append(dict(source=source, samples=len(rows), tp=tp, fp=fp, fn=fn, tn=tn,
                           precision=precision, recall=recall, decisions=rows,
                           status="PASS" if precision is not None and recall is not None else "INCOMPLETE"))
    return dict(status="PASS" if result and all(r["status"] == "PASS" for r in result) else "INCOMPLETE",
                by_source=result, basis="One binary extraction decision per labeled source item; sample only.")


def dedup_scores(store, data):
    result = {}
    for grouping in ("exact", "normalized"):
        decisions = []
        for row in data["pairs"]:
            left, right = (store["failures"][row[k]] for k in ("left", "right"))
            merged = left["group_id"] == right["group_id"] if grouping == "exact" else signature(left) == signature(right)
            decisions.append(dict(row, merged=merged))
        result[grouping] = dict(pairs=len(decisions),
                                false_merges=sum(r["merged"] and not r["same"] for r in decisions) if decisions else None,
                                missed_merges=sum(not r["merged"] and r["same"] for r in decisions) if decisions else None,
                                decisions=decisions)
    return dict(status="PASS" if data["pairs"] else "INCOMPLETE", by_grouping=result)


def build(store, data, days, run_id, created):
    activations = {r["lesson_revision"]: r for r in data["activations"]}
    lessons = [store["lessons"][r] for r in activations]
    covered = {r["lesson_id"] for r in lessons}
    lessons.extend(r for r in latest_lessons(store).values() if r["lesson_id"] not in covered)
    outcomes = []
    for lesson in sorted(lessons, key=lambda r: r["id"]):
        row = dict(version=1, created=created, **outcome(store, data, lesson, activations.get(lesson["id"]), days, run_id, created))
        row["id"] = receipt_id(row)
        outcomes.append(row)
    privacy = dict(status="PASS" if data["privacy"] and all(r["blocked"] and r["leaks"] == 0 for r in data["privacy"])
                   else "FAIL" if data["privacy"] else "INCOMPLETE", cases=len(data["privacy"]),
                   blocked=sum(r["blocked"] for r in data["privacy"]) if data["privacy"] else None,
                   leaks=sum(r["leaks"] for r in data["privacy"]) if data["privacy"] else None,
                   evidence=data["privacy"], basis="Caller-attested canary test receipts; sample only.",
                   public_release_check="INCOMPLETE: no public staging or publication performed")
    runs = []
    for row in data["model_runs"]:
        observed = dict(sorted(Counter(c["role"] for c in row["calls"]).items()))
        runs.append(dict(row, status="PASS" if row["complete"] else "INCOMPLETE", observed_by_role=observed,
                         calls_by_role=observed if row["complete"] else None))
    calls = dict(status="PASS" if runs and all(r["complete"] for r in runs) else "INCOMPLETE",
                 measurement_calls=0, runs=runs, basis="Caller-attested actual call log; missing logs are unknown.")
    snapshot = {key: {r: digest(encoded(row)) for r, row in sorted(store[key].items())} for key in BASE}
    report = dict(version=1, created=created, run_id=run_id, snapshot=snapshot, snapshot_sha256=digest(encoded(snapshot)),
                  basis="PASS denotes evidence completeness, not effectiveness. Recurrences count source observations, not causal effects.",
                  input=data, input_sha256=digest(encoded(data)), window_days=days, outcomes=[r["id"] for r in outcomes],
                  sources=source_inventory(store, data), extraction=extraction_scores(store, data), dedup=dedup_scores(store, data),
                  privacy=privacy, model_calls=calls)
    states = [r["status"] for r in outcomes] + [report[k]["status"] for k in ("sources", "extraction", "dedup", "privacy", "model_calls")]
    report["status"] = "FAIL" if "FAIL" in states else "PASS" if outcomes and all(s == "PASS" for s in states) else "INCOMPLETE"
    report["id"] = receipt_id(report)
    return report, outcomes


def validate_record(record, collection, stem):
    fields_expected = OUTCOME_FIELDS if collection == "outcomes" else REPORT_FIELDS
    require(set(record) == fields_expected | {"id", "created", "version"}
            and type(record["version"]) is int and record["version"] == 1, "invalid measurement record fields")
    require(isinstance(stem, str) and ID.fullmatch(stem) and record["id"] == stem == receipt_id(record),
            "invalid measurement record digest")
    occurred(record["created"])
    require(len(record["created"]) > 10 and ID.fullmatch(text(record["run_id"], "measurement run")),
            "invalid measurement run")
    require(record["status"] in ("PASS", "FAIL", "INCOMPLETE"), "invalid measurement state")
    if collection == "outcomes":
        for key in ("lesson_id", "lesson_revision", "latest_revision"):
            require(ID.fullmatch(text(record[key], key)), "invalid outcome lesson reference")
        require(record["latest_status"] in STATUSES
                and record["grouping"] in ("exact", "normalized")
                and GROUP.fullmatch(text(record["group_id"], "outcome group")), "invalid outcome grouping or state")
        for key in ("observed", "recurrences"):
            fields(record[key], "before after")
            for value in record[key].values():
                if value is not None:
                    count(value)
        fields(record["evidence"], "before after uncertain undated duplicates")
        for key, values in record["evidence"].items():
            require(isinstance(values, list), "invalid outcome evidence")
            groups = values if key == "duplicates" else [values]
            require(all(isinstance(group, list) and all(isinstance(v, str) and ID.fullmatch(v) for v in group)
                        for group in groups), "invalid outcome evidence IDs")
        require(isinstance(record["gaps"], list) and all(isinstance(v, str) and v.strip() for v in record["gaps"]),
                "invalid outcome gaps")
        fields(record["replay_result"], "status receipt verification before after")
        require(record["activation"] is None or isinstance(record["activation"], dict), "invalid outcome activation")
        require(record["window"] is None or isinstance(record["window"], dict), "invalid outcome window")
    return record


def validate_reports(store):
    for report in store["measurements"].values():
        snapshot = report["snapshot"]
        require(isinstance(snapshot, dict) and set(snapshot) == set(BASE), "invalid measurement snapshot")
        subset = {}
        for collection in BASE:
            require(isinstance(snapshot[collection], dict), "invalid measurement snapshot collection")
            subset[collection] = {}
            for ref, expected in snapshot[collection].items():
                require(ref in store[collection] and digest(encoded(store[collection][ref])) == expected,
                        "measurement snapshot missing or changed")
                subset[collection][ref] = store[collection][ref]
        data = validate_input(report["input"], subset)
        count(report["window_days"])
        require(report["window_days"] > 0, "measurement window must be positive")
        expected, outcomes = build(subset, data, report["window_days"], report["run_id"], report["created"])
        require(report == expected and all(store["outcomes"].get(r["id"]) == r for r in outcomes),
                "measurement results are missing or mismatched")


def measure(args, root, store):
    try:
        value = json.loads(read_text(args.input), object_pairs_hook=unique_object) if args.input else {"version": 1}
        data = validate_input(value, store)
        require(args.window_days > 0, "measurement window must be positive")
        run = new_record()
        report, outcomes = build(store, data, args.window_days, run["id"], run["created"])
        for row in [*outcomes, report]:
            validate_record(row, "outcomes" if "lesson_id" in row else "measurements", row["id"])
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise ColdfixError("invalid measurement evidence; nothing measured") from None
    root = ordinary_path(root)
    require(not root.exists() or root.stat().st_mode & 0o077 == 0, "measurement store must be private (0700)")
    for collection in ("outcomes", "measurements"):
        folder = ordinary_path(root / collection)
        require(not folder.exists() or folder.stat().st_mode & 0o077 == 0, "measurement collection must be private (0700)")
    for row in outcomes:
        save(root, "outcomes", row)
    save(root, "measurements", report)
    return dict(report_id=report["id"], outcome_ids=report["outcomes"], status=report["status"],
                model_calls=0, _exit={"PASS": 0, "FAIL": 1, "INCOMPLETE": 2}[report["status"]])
