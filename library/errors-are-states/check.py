import json
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.getcwd())
FAILURE = "error-opened-gate"


def holds():
    from gate import gate_opens, run_check

    def broken():
        raise OSError("checker unavailable")

    return (run_check(broken) == "INCOMPLETE" and not gate_opens(broken)
            and gate_opens(lambda: True) and not gate_opens(lambda: False))


def main():
    try:
        ok = holds()
    except Exception as error:
        print(json.dumps({"status": "INCOMPLETE", "reason": repr(error)}))
        return 2
    print(json.dumps({"status": "PASS"} if ok else {"status": "FAIL", "failure": FAILURE}))
    return 0 if ok else 1


sys.exit(main())
