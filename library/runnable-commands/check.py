import json
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.getcwd())
FAILURE = "documented-command-fails"


import re
import shutil
import subprocess
import tempfile


def holds():
    with open("README.md") as stream:
        blocks = re.findall(r"^```sh\n(.*?)^```", stream.read(), re.M | re.S)
    commands = [line for block in blocks for line in block.splitlines() if line.strip()]
    with tempfile.TemporaryDirectory() as temporary:
        copy = os.path.join(temporary, "tree")
        shutil.copytree(os.getcwd(), copy)
        return bool(commands) and all(
            subprocess.run(command, shell=True, cwd=copy, capture_output=True,
                           env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1")).returncode == 0
            for command in commands)


def main():
    try:
        ok = holds()
    except Exception as error:
        print(json.dumps({"status": "INCOMPLETE", "reason": repr(error)}))
        return 2
    print(json.dumps({"status": "PASS"} if ok else {"status": "FAIL", "failure": FAILURE}))
    return 0 if ok else 1


sys.exit(main())
