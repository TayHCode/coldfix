import json
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.getcwd())
FAILURE = "stale-line-compared-at-new-head"


def holds():
    from comments import carry_forward
    comment = {"path": "src/app.py", "line": 12, "original_line": 10, "commit": "c1", "reviewed_head": "c1"}
    carried = carry_forward(comment, "c2")
    return (carried["commit"] == "c2" and carried["reviewed_head"] == "c1"
            and carried["line"] == 12 and carried.get("original_line") is None)


def main():
    try:
        ok = holds()
    except Exception as error:
        print(json.dumps({"status": "INCOMPLETE", "reason": repr(error)}))
        return 2
    print(json.dumps({"status": "PASS"} if ok else {"status": "FAIL", "failure": FAILURE}))
    return 0 if ok else 1


sys.exit(main())
