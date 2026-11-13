"""Derive flaky failures from private attempts and stored classifications."""

import json

import coldfix as c


def metadata(record):
    try:
        value = json.loads(record["error"], object_pairs_hook=c.unique_object)
    except (ValueError, TypeError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


def collection(record):
    row = metadata(record)
    return "passes" if record["source"] == "failure-ingest" and row.get("type") == "ci" and row.get("conclusion") == "success" else "failures"


def validate_pass(record, stem):
    from failure_ingest import ci_event

    c.validate(record, "failures", stem)
    c.require(collection(record) == "passes", "invalid passing attempt")
    ci_event({key: value for key, value in metadata(record).items() if key != "classification"})
    return record


def attempts(record, row):
    if record["source"] == "failure-ingest" and row.get("type") == "ci":
        from failure_ingest import ci_event

        ci_event({key: value for key, value in row.items() if key != "classification"})
        jobs = [job["name"] for job in row["jobs"]] if "jobs" in row else [row["job"]]
        identity = dict(run_id=row["run_id"], attempt=row["attempt"], source_ref=record["source_ref"])
        return [((row["repo"].lower(), row["head_sha"], job, row.get("check")), identity) for job in jobs]
    return []


def detect(store):
    groups = {}
    for name in ("failures", "passes"):
        for record in store[name].values():
            row = metadata(record)
            classification = row.get("classification")
            classified = isinstance(classification, dict) and classification.get("category") == "flake"
            projected = attempts(record, row)
            if classified and not projected:
                projected = [((record["repo"], None, record["command"], None),
                              dict(run_id=None, attempt=None, source_ref=record.get("source_ref")))]
            for key, identity in projected:
                group = groups.setdefault(key, dict(failed_attempts={}, passed_attempts={}, failures=set(), classified=False))
                group["failed_attempts" if name == "failures" else "passed_attempts"][c.encoded(identity)] = identity
                if name == "failures":
                    group["failures"].add(record["id"])
                    group["classified"] |= classified
    rows, failures = [], set()
    for key, group in sorted(groups.items(), key=lambda item: c.encoded(item[0])):
        if not group["failed_attempts"] or not (group["passed_attempts"] or group["classified"]):
            continue
        repo, sha, job, check = key
        rows.append(dict(repo=repo, sha=sha, job=job, check=check,
                         **{name: [value for _, value in sorted(group[name].items())]
                            for name in ("failed_attempts", "passed_attempts")}))
        failures.update(group["failures"])
    return rows, failures
