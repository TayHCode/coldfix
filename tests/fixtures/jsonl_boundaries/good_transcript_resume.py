import json


def rewrite_session_ids(transcript, session_id):
    lines = []
    for line in transcript.split("\n"):
        if not line:
            continue
        try:
            # ok: jsonl-splitlines
            record = json.loads(line)
        except json.JSONDecodeError:
            lines.append(line)
            continue
        record["sessionId"] = session_id
        lines.append(json.dumps(record, ensure_ascii=False))
    return "\n".join(lines) + "\n"
