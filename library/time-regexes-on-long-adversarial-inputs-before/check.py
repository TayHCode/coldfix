import json
import os
import subprocess
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.getcwd())
FAILURE = "regex-backtracks-on-long-input"
PROBE = "from paths import python_files; python_files('a/' * 25000 + 'b')"


def holds():
    from paths import python_files
    if python_files("see src/app/main.py and notes.txt") != ["src/app/main.py"]:
        raise AssertionError("python_files must find the file in a short input")
    try:
        subprocess.run([sys.executable, "-B", "-c", PROBE], cwd=os.getcwd(), check=True, timeout=2,
                       capture_output=True)
    except subprocess.TimeoutExpired:
        return False
    return True


def main():
    try:
        ok = holds()
    except Exception as error:
        print(json.dumps({"status": "INCOMPLETE", "reason": repr(error)}))
        return 2
    print(json.dumps({"status": "PASS"} if ok else {"status": "FAIL", "failure": FAILURE}))
    return 0 if ok else 1


sys.exit(main())
