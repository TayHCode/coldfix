import json
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.getcwd())
FAILURE = "bare-name-match"


def holds():
    from results import passed
    results = {"tests.test_a.A.test_x": "fail", "tests.test_b.B.test_x": "ok"}
    return not passed(results, "tests.test_a.A.test_x") and passed(results, "tests.test_b.B.test_x")


def main():
    try:
        ok = holds()
    except Exception as error:
        print(json.dumps({"status": "INCOMPLETE", "reason": repr(error)}))
        return 2
    print(json.dumps({"status": "PASS"} if ok else {"status": "FAIL", "failure": FAILURE}))
    return 0 if ok else 1


sys.exit(main())
