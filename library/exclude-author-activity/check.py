import json
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.getcwd())
FAILURE = "author-reset-clock"


def holds():
    from activity import last_maintainer_activity
    events = [{"login": "maintainer", "at": "2026-01-01"}, {"login": "author", "at": "2026-03-01"}]
    return last_maintainer_activity(events, "author") == "2026-01-01"


def main():
    try:
        ok = holds()
    except Exception as error:
        print(json.dumps({"status": "INCOMPLETE", "reason": repr(error)}))
        return 2
    print(json.dumps({"status": "PASS"} if ok else {"status": "FAIL", "failure": FAILURE}))
    return 0 if ok else 1


sys.exit(main())
