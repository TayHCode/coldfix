LEDGER = """import json


def append_row(path, row):
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\\n")


def read_rows(path):
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle.read().{split} if line]


def numbered_rows(text):
    return {{number: json.loads(line) for number, line in enumerate(text.{split}, 1) if line}}
"""

FIXTURES = {
    "bad": {"ledger.py": LEDGER.format(split="splitlines()")},
    "good": {"ledger.py": LEDGER.format(split='split("\\n")')},
}
