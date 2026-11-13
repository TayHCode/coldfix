import json
import subprocess
import sys
import tempfile

with tempfile.TemporaryDirectory() as store:
    base = [sys.executable, "coldfix.py", "--store", store]
    for error in ("one", "one", "two"):
        done = subprocess.run([*base, "capture", "--command", "t", "--repo", "r", "--agent", "a", "--error", error], capture_output=True, text=True)
        assert done.returncode == 0, done.stderr
    everything = json.loads(subprocess.run([*base, "propose"], capture_output=True, text=True).stdout)["groups"]
    only = subprocess.run([*base, "propose", "--recurring"], capture_output=True, text=True)
    assert only.returncode == 0, only.stderr
    groups = json.loads(only.stdout)["groups"]
    assert groups == [g for g in everything if g["recurring"]] and len(groups) == 1, groups
