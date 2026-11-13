import json
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.getcwd())
FAILURE = "undocumented-flag"


import re


def holds():
    with open("cli.py") as stream:
        flags = re.findall(r"add_argument\(\s*[\"'](--[\w-]+)", stream.read())
    with open("README.md") as stream:
        readme = stream.read()
    return bool(flags) and all(flag in readme for flag in flags)


def main():
    try:
        ok = holds()
    except Exception as error:
        print(json.dumps({"status": "INCOMPLETE", "reason": repr(error)}))
        return 2
    print(json.dumps({"status": "PASS"} if ok else {"status": "FAIL", "failure": FAILURE}))
    return 0 if ok else 1


sys.exit(main())
