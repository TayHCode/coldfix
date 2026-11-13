"""Verify step-trace ingest proofs. Synthetic fixtures; no network or credentials."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import coldfix as c


ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "failure_ingest.py"
FIXTURES = ROOT / "tests" / "fixtures"
OCCURRED = "2026-10-30T12:00:00Z"
HEAD = "a" * 40


class VerifyTraceIngestTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        self.store = self.work / ".coldfix"

    def cli(self, trace, *extra, repo="example/project"):
        return subprocess.run([sys.executable, str(ENTRY), "--store", str(self.store), "trace", "--input", str(trace),
                               "--repo", repo, "--occurred", OCCURRED, *extra], capture_output=True, text=True)

    def records(self):
        return list(c.load_store(self.store)[1]["failures"].values())

    def write(self, *rows, name="trace.jsonl"):
        path = self.work / name
        path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
        return path

    def step(self, **changes):
        row = dict(schema_version=1, run_id="run-1", head=HEAD, step="unit", command="python3 -m unittest",
                   status="fail", duration_ms=10, failing_excerpt="AssertionError: expected 2", inputs_hash=None)
        row.update(changes)
        return row

    def test_failed_step_becomes_evidence_and_other_steps_are_not_imported(self):
        result = self.cli(FIXTURES / "verify-trace.jsonl")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), dict(imported=1, skipped=0))
        (row,) = self.records()
        evidence = json.loads(row["error"])
        self.assertEqual((row["kind"], row["repo"], row["command"], row["source"]),
                         ("test", "example/project", "python3 -m unittest", "failure-ingest"))
        self.assertEqual({key: evidence[key] for key in ("step", "command", "excerpt", "head", "run_id", "duration_ms")},
                         dict(step="unit", command="python3 -m unittest", excerpt="AssertionError: expected 2, got 3",
                              head=HEAD, run_id="run-1", duration_ms=4210))
        self.assertEqual(evidence["inputs_hash"], "b" * 64)
        self.assertEqual(evidence["classification"]["category"], "real-bug")

    def test_reimport_is_idempotent_and_changed_content_is_rejected(self):
        self.assertEqual(self.cli(FIXTURES / "verify-trace.jsonl").returncode, 0)
        before = {p: p.read_bytes() for p in self.store.rglob("*.json")}
        again = self.cli(FIXTURES / "verify-trace.jsonl")
        self.assertEqual(json.loads(again.stdout), dict(imported=0, skipped=1))
        self.assertEqual(before, {p: p.read_bytes() for p in self.store.rglob("*.json")})
        changed = self.cli(self.write(self.step(failing_excerpt="different")))
        self.assertEqual(changed.returncode, 2)
        self.assertEqual(len(self.records()), 1)

    def test_same_failure_at_another_head_groups_with_the_first(self):
        self.assertEqual(self.cli(FIXTURES / "verify-trace.jsonl").returncode, 0)
        self.assertEqual(self.cli(FIXTURES / "verify-trace-later-head.jsonl").returncode, 0)
        first, second = self.records()
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(c.signature(first), c.signature(second))

    def test_other_schema_versions_are_rejected_without_importing(self):
        for trace in (self.write(self.step(schema_version=4)),
                      self.write(self.step(), self.step(step="lint", schema_version=4)),
                      self.write(self.step(schema_version="1")),
                      self.write(self.step(schema_version=True)),
                      self.write({k: v for k, v in self.step().items() if k != "schema_version"})):
            result = self.cli(trace)
            self.assertEqual(result.returncode, 2, trace)
            self.assertNotIn("Traceback", result.stderr)
            self.assertFalse(self.store.exists())

    def test_current_and_older_trace_versions_are_supported(self):
        for version in (1, 2, 3):
            with self.subTest(version=version):
                result = self.cli(self.write(self.step(schema_version=version, run_id=f"version-{version}")))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), dict(imported=1, skipped=0))

    def test_unknown_fields_are_tolerated(self):
        result = self.cli(self.write(self.step(future="x", nested={"a": [1]})))
        self.assertEqual(json.loads(result.stdout), dict(imported=1, skipped=0), result.stderr)

    def test_malformed_traces_are_rejected_whole(self):
        bad = [self.step(status="error"), self.step(failing_excerpt=None), self.step(failing_excerpt=" "),
               self.step(head="main"), self.step(duration_ms=-1), self.step(duration_ms=1.5),
               self.step(inputs_hash="ABC"), self.step(step=""), self.step(command=""), self.step(run_id=["x"])]
        for row in bad:
            result = self.cli(self.write(self.step(step="lint", status="pass"), row))
            self.assertEqual(result.returncode, 2, row)
            self.assertFalse(self.store.exists(), row)
        missing = {k: v for k, v in self.step().items() if k != "command"}
        self.assertEqual(self.cli(self.write(missing)).returncode, 2)
        self.assertEqual(self.cli(self.write(self.step(), self.step())).returncode, 2)
        broken = self.work / "broken.jsonl"
        broken.write_text(json.dumps(self.step()) + "\n{not json\n")
        self.assertEqual(self.cli(broken).returncode, 2)
        empty = self.work / "empty.jsonl"
        empty.write_text("\n")
        self.assertEqual(self.cli(empty).returncode, 2)
        self.assertFalse(self.store.exists())

    def test_passing_trace_imports_nothing(self):
        result = self.cli(self.write(self.step(status="pass", failing_excerpt=None), self.step(step="b", status="skip")))
        self.assertEqual(json.loads(result.stdout), dict(imported=0, skipped=0))
        self.assertEqual(self.records(), [])

    def test_null_head_is_kept_as_null(self):
        self.assertEqual(self.cli(self.write(self.step(head=None))).returncode, 0)
        self.assertIsNone(json.loads(self.records()[0]["error"])["head"])

    def test_unicode_separators_inside_strings_survive(self):
        excerpt = "first\u0085second third fourth\x0cfifth"
        self.assertEqual(self.cli(self.write(self.step(failing_excerpt=excerpt))).returncode, 0)
        self.assertEqual(json.loads(self.records()[0]["error"])["excerpt"], excerpt)

    def test_invalid_repo_or_time_is_rejected(self):
        trace = self.write(self.step())
        self.assertEqual(self.cli(trace, repo="not a repo").returncode, 2)
        bad = subprocess.run([sys.executable, str(ENTRY), "--store", str(self.store), "trace", "--input", str(trace),
                              "--repo", "example/project", "--occurred", "yesterday"], capture_output=True, text=True)
        self.assertEqual(bad.returncode, 2)


if __name__ == "__main__":
    unittest.main()
