import json


def append_receipt(path, title):
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"title": title}, ensure_ascii=False) + "\n")


def load_receipts(text):
    # ruleid: jsonl-splitlines
    return [json.loads(line) for line in text.splitlines() if line.strip()]
