def grouping_content(record):
    metadata = json.loads(record["error"], object_pairs_hook=c.unique_object)
    if metadata["type"] == "verify-trace":
        need(metadata["status"] == "fail")
        trace_event({k: v for k, v in metadata.items() if k != "classification"})
        return metadata["command"], metadata["excerpt"]
    if metadata["type"] == "ci":
        ci_event({k: v for k, v in metadata.items() if k != "classification"})
        return c.encoded([metadata["workflow"], metadata["job"]]), metadata["log_tail"]
    if metadata["type"] == "verify-fix-pair":
        return c.fleet_label("verify"), metadata["failing_check"]["excerpt"]
    if metadata["type"] == "settle-codex-lesson":
        need(isinstance(metadata["findings"], list) and metadata["findings"])
        return "codex review finding", c.encoded([[f["path"], f["title"]] for f in metadata["findings"]])
    t3_event({k: v for k, v in metadata.items() if k != "classification"})
    return "T3 blocker", c.encoded([metadata["goal"], metadata["blocker_category"], metadata["reason"]])


def record(event):
    row = c.event_record(event, "failure-ingest")
    row["id"] = c.digest("failure-ingest-v1\n" + event["source_ref"])[:32]
    return c.validate(row, "failures", row["id"])


def import_records(root, records):
    # The existing lesson lock protects the read/check/write invariant, including retries.
    with c.lesson_lock(root):
        _, store = c.load_store(root)
        pending, seen = [], dict(store["failures"])
        for row in records:
            previous = seen.get(row["id"])
            if previous is not None:
                need({k: v for k, v in previous.items() if k != "created"} == {k: v for k, v in row.items() if k != "created"})
            else:
                pending.append(row)
                seen[row["id"]] = row
        for row in pending:
            c.save(root, "failures", row)
        _, actual = c.load_store(root)
        need(all(row["id"] in actual["failures"] for row in records))
    return dict(imported=len(pending), skipped=len(records) - len(pending))
