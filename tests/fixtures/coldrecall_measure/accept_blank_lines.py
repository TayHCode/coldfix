import json
import subprocess
import sys
import tempfile


def event(error):
    return json.dumps(dict(command="verify", error=error, repo="example", agent="worker",
                           kind="test", source_ref="blank-lines", occurred="2026-10-20"))


with tempfile.TemporaryDirectory() as store:
    done = subprocess.run([sys.executable, "coldfix.py", "--store", store, "ingest"], text=True, capture_output=True,
                          input=event("first") + "\n\n   \n" + event("second"))
    result = json.loads(done.stdout) if done.returncode == 0 else {}
    sys.exit(0 if result.get("imported_records") == 2 and result.get("skipped_blank_lines") == 2 else 1)
