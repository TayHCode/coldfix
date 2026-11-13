import json
import pathlib
import subprocess
import sys
import tempfile

LOG = "first\nsecond part\nthird\u0085part\nlast\n"
WANT = ("third\u0085part\nlast", "third\u0085part\nlast\n")

with tempfile.TemporaryDirectory() as folder:
    log, store = pathlib.Path(folder, "run.log"), str(pathlib.Path(folder, "store"))
    log.write_bytes(LOG.encode("utf-8"))
    base = [sys.executable, "coldfix.py", "--store", store]
    made = subprocess.run([*base, "capture", "--command", "t", "--repo", "r", "--agent", "a", "--log-file", str(log), "--tail", "2"], capture_output=True, text=True)
    assert made.returncode == 0, made.stderr
    stored = json.loads(next(pathlib.Path(store, "failures").glob("*.json")).read_text(encoding="utf-8"))
    assert stored["error"] in WANT, repr(stored["error"])
