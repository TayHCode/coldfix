def ingest_events(content, source):
    records = []
    for number, line in enumerate(content.splitlines(), 1):
        if not line.strip():
            continue
        try:
            # ruleid: jsonl-splitlines
            records.append(event_record(json.loads(line, object_pairs_hook=unique_object), source))
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
            raise ColdfixError(f"invalid JSONL event at line {number}; nothing imported") from None
    return records
