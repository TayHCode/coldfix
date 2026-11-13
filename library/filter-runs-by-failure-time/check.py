import json
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.getcwd())
FAILURE = "rerun-failure-dropped"


def holds():
    from runs import failures_since
    rerun = {"id": 1, "conclusion": "failure", "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-09T00:00:00Z"}
    old = {"id": 2, "conclusion": "failure", "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-02T00:00:00Z"}
    return [run["id"] for run in failures_since([rerun, old], "2026-01-08T00:00:00Z")] == [1]


def main():
    try:
        ok = holds()
    except Exception as error:
        print(json.dumps({"status": "INCOMPLETE", "reason": repr(error)}))
        return 2
    print(json.dumps({"status": "PASS"} if ok else {"status": "FAIL", "failure": FAILURE}))
    return 0 if ok else 1


sys.exit(main())
