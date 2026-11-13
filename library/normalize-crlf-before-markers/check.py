import json
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.getcwd())
FAILURE = "crlf-marker-missed"


def holds():
    from marker import is_delivered
    return (is_delivered("<!-- delivered -->\r\n# Rule\r\n")
            and is_delivered("<!-- delivered -->\n# Rule\n")
            and not is_delivered("# Rule\r\n"))


def main():
    try:
        ok = holds()
    except Exception as error:
        print(json.dumps({"status": "INCOMPLETE", "reason": repr(error)}))
        return 2
    print(json.dumps({"status": "PASS"} if ok else {"status": "FAIL", "failure": FAILURE}))
    return 0 if ok else 1


sys.exit(main())
