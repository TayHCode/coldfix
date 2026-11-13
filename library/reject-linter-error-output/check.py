import json
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.getcwd())
FAILURE = "error-read-as-findings"


def holds():
    from lint import has_findings
    return (has_findings(1, "a.py:1: E1 unused import\n")
            and not has_findings(1, "usage: lint [-h] path\nError: no such option: --strict\n")
            and not has_findings(0, ""))


def main():
    try:
        ok = holds()
    except Exception as error:
        print(json.dumps({"status": "INCOMPLETE", "reason": repr(error)}))
        return 2
    print(json.dumps({"status": "PASS"} if ok else {"status": "FAIL", "failure": FAILURE}))
    return 0 if ok else 1


sys.exit(main())
