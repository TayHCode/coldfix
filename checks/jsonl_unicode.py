"""Check that JSONL ingestion preserves Unicode separators inside strings."""
import json
from pathlib import Path
from datetime import date
from subprocess import run
import sys
import tempfile

sys.path.insert(0, str(Path.cwd() / "tests"))
from test_cli import CLI


def check(separator):
    error = "first" + separator + "second"
    event = dict(command="verify", error=error, repo="example", agent="worker",
                 kind="test", source_ref="unicode-case", occurred=date(2026, 10, 20).isoformat())
    with tempfile.TemporaryDirectory() as temporary:
        store = Path(temporary) / "store"
        result = run([sys.executable, str(CLI), "--store", str(store), "ingest"],
                                input=json.dumps(event, ensure_ascii=False) + "\n",
                                text=True, capture_output=True)
        if result.returncode == 2 and "invalid JSONL event" in result.stderr:
            return False
        if result.returncode != 0:
            raise RuntimeError("ingestion could not be checked")
        records = list((store / "failures").glob("*.json"))
        return len(records) == 1 and json.loads(records[0].read_text())["error"] == error


if __name__ == "__main__":
    separators = [chr(int(sys.argv[1], 16))] if len(sys.argv) == 2 else ["\u0085", "\u2028", "\u2029"]
    ok = all(check(separator) for separator in separators)
    print(json.dumps({"status": "PASS"} if ok else
                     {"status": "FAIL", "failure": "jsonl-unicode-separator"}))
    sys.exit(0 if ok else 1)
