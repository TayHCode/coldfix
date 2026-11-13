def retain_events(matches):
    lines = []
    for source in matches:
        for line in source.read_text().splitlines():
            # ruleid: jsonl-splitlines
            event = json.loads(line)
            if event["command"].startswith("fix commit "):
                lines.append(line)
    return lines


def compact_diagnostics(row):
    diagnostics = row.pop("stderr", "")
    # ruleid: jsonl-splitlines
    row["case_results"] = [json.loads(line) for line in diagnostics.splitlines()
                           if line.startswith('{"case":')]
