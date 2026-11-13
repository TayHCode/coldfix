import json
import subprocess
import sys
import tempfile

LEAK_ERROR, LEAK_COMMAND, BOGUS = "ZZLEAK-4711 boom", "pytest ZZLEAK-CMD", "ZZBOGUS-0042"


def run(store, *args):
    return subprocess.run([sys.executable, "coldfix.py", "--store", store, *args], capture_output=True, text=True)


with tempfile.TemporaryDirectory() as store:
    made = run(store, "capture", "--command", LEAK_COMMAND, "--repo", "r", "--agent", "a", "--error", LEAK_ERROR)
    assert made.returncode == 0, made.stderr
    created = json.loads(made.stdout)
    shown = run(store, "show", "--id", created["failure_id"])
    assert shown.returncode == 0, shown.stderr
    record = json.loads(shown.stdout)
    assert record["id"] == created["failure_id"] and record["group_id"] == created["group_id"], record
    assert LEAK_ERROR not in shown.stdout and LEAK_COMMAND not in shown.stdout, "show echoed captured text"
    missing = run(store, "show", "--id", BOGUS)
    assert missing.returncode == 2 and BOGUS not in missing.stdout + missing.stderr, missing.stderr
