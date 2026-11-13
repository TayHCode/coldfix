import json


def append_correction(path, note):
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"note": note}, ensure_ascii=False) + "\n")


def load_corrections(text):
    corrections = []
    for line in text.splitlines():
        try:
            # ruleid: jsonl-splitlines
            corrections.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return corrections
