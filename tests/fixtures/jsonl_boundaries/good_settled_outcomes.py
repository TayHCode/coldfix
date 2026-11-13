def compact_diagnostics(row):
    diagnostics = row.pop("stderr", "")
    # ok: jsonl-splitlines
    row["case_results"] = [json.loads(line) for line in diagnostics.split("\n")
                           if line.startswith('{"case":')]
