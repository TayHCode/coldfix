import json
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.getcwd())
FAILURE = "coerced-date"


from datetime import date


def holds():
    from dates import parse_date
    if parse_date("2026-02-28") != date(2026, 2, 28):
        return False
    try:
        parse_date("2026-02-31")
    except ValueError:
        return True
    return False


def main():
    try:
        ok = holds()
    except Exception as error:
        print(json.dumps({"status": "INCOMPLETE", "reason": repr(error)}))
        return 2
    print(json.dumps({"status": "PASS"} if ok else {"status": "FAIL", "failure": FAILURE}))
    return 0 if ok else 1


sys.exit(main())
