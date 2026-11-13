import json


def append_receipt(path, title):
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"title": title}, ensure_ascii=False) + "\n")


def load_receipts(text):
    # ok: jsonl-splitlines
    return [json.loads(line) for line in text.split("\n") if line.strip()]
