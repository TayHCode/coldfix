import json
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.getcwd())
FAILURE = "unregistered-test"


import ast
import re
import shutil
import subprocess
import tempfile


def holds():
    tree = os.getcwd()
    defined = 0
    for name in os.listdir(os.path.join(tree, "tests")):
        if name.endswith(".py"):
            with open(os.path.join(tree, "tests", name)) as stream:
                parsed = ast.parse(stream.read())
            defined += sum(isinstance(node, ast.FunctionDef) and node.name.startswith("test")
                           for node in ast.walk(parsed))
    with tempfile.TemporaryDirectory() as temporary:
        copy = os.path.join(temporary, "tree")
        shutil.copytree(tree, copy)
        result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                                cwd=copy, capture_output=True, text=True,
                                env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    ran = re.search(r"^Ran (\d+) tests? in ", result.stderr, re.M)
    if ran is None:
        raise RuntimeError("unittest did not report a test count")
    return defined > 0 and int(ran.group(1)) == defined


def main():
    try:
        ok = holds()
    except Exception as error:
        print(json.dumps({"status": "INCOMPLETE", "reason": repr(error)}))
        return 2
    print(json.dumps({"status": "PASS"} if ok else {"status": "FAIL", "failure": FAILURE}))
    return 0 if ok else 1


sys.exit(main())
