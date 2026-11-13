import json
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.getcwd())
FAILURE = "quoted-path-setting"


def holds():
    from units import path_changed
    return (path_changed("/srv/my data/50%") == "PathChanged=/srv/my\\x20data/50%%"
            and path_changed("/srv/plain") == "PathChanged=/srv/plain")


def main():
    try:
        ok = holds()
    except Exception as error:
        print(json.dumps({"status": "INCOMPLETE", "reason": repr(error)}))
        return 2
    print(json.dumps({"status": "PASS"} if ok else {"status": "FAIL", "failure": FAILURE}))
    return 0 if ok else 1


sys.exit(main())
