def unclassified(metadata):
    return {k: v for k, v in metadata.items() if k != "classification"}


def coldbox_content(metadata):
    return c.encoded([metadata["repo"], metadata["check"], metadata["command"]]), re.sub(r"\d+", "<n>", c.normalized(metadata["log_tail"])), True


def trace_content(metadata):
    need(metadata["status"] == "fail")
    trace_event(unclassified(metadata))
    return metadata["command"], metadata["excerpt"], False


def ci_content(metadata):
    ci_event(unclassified(metadata))
    return c.encoded([metadata["workflow"], metadata["job"]]), metadata["log_tail"], False


def verify_pair_content(metadata):
    return c.fleet_label("verify"), metadata["failing_check"]["excerpt"], False


def settle_codex_content(metadata):
    need(isinstance(metadata["findings"], list) and metadata["findings"])
    return "codex review finding", c.encoded([[f["path"], f["title"]] for f in metadata["findings"]]), False


def t3_content(metadata):
    t3_event(unclassified(metadata))
    return "T3 blocker", c.encoded([metadata["goal"], metadata["blocker_category"], metadata["reason"]]), False


GROUPING = {
    "coldbox": coldbox_content,
    "verify-trace": trace_content,
    "ci": ci_content,
    "verify-fix-pair": verify_pair_content,
    "settle-codex-lesson": settle_codex_content,
    "t3": t3_content,
}


def grouping_content(record):
    metadata = json.loads(record["error"], object_pairs_hook=c.unique_object)
    kind = metadata["type"]
    need(isinstance(kind, str) and kind in GROUPING)
    return GROUPING[kind](metadata)


def record_id(source_ref):
    return c.digest("failure-ingest-v1\n" + source_ref)[:32]


def record(event):
    row = c.event_record(event, "failure-ingest")
    row["id"] = record_id(event["source_ref"])
    return c.validate(row, "failures", row["id"])


def import_records(root, records):
    from flakiness import collection

    # The existing lesson lock protects the read/check/write invariant, including retries.
    with c.lesson_lock(root):
        _, store = c.load_store(root)
        pending, seen = [], {**store["failures"], **store["passes"]}
        for row in records:
            previous = seen.get(row["id"])
            if previous is not None:
                need({k: v for k, v in previous.items() if k != "created"} == {k: v for k, v in row.items() if k != "created"})
            else:
                pending.append(row)
                seen[row["id"]] = row
        for row in pending:
            c.save(root, collection(row), row)
        _, actual = c.load_store(root)
        need(all(row["id"] in actual[collection(row)] for row in records))
    return dict(imported=len(pending), skipped=len(records) - len(pending))
