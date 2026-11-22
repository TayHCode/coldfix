"""ColdRewind undo ingest proofs. Synthetic fixtures; no network or credentials."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import types
import unittest

import coldfix as c
import failure_ingest
import learn_candidates


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "coldrewind-undo.json"


class ColdrewindIngestTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.store = self.work / ".coldfix"

    def cli(self, path, log=None):
        out, err = io.StringIO(), io.StringIO()
        extra = [] if log is None else ["--log", str(self.write(log, "log.json"))]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = failure_ingest.main(["--store", str(self.store), "coldrewind", "--input", str(path), "--repo", "example/project", *extra])
            except SystemExit as error:
                code = error.code
        return types.SimpleNamespace(returncode=code, stdout=out.getvalue(), stderr=err.getvalue())

    def write(self, value, name="undo.json"):
        path = self.work / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def undo(self, **changes):
        value = json.loads(FIXTURE.read_text())
        value["step"].update(changes)
        return value

    def test_undo_becomes_an_unproven_candidate_with_command_paths_and_agent(self):
        result = self.cli(FIXTURE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), dict(imported=1, skipped=0))
        store = c.load_store(self.store)[1]
        (row,) = store["failures"].values()
        self.assertEqual((row["kind"], row["repo"], row["agent"], row["command"]),
                         ("revert", "example/project", "coldrewind", "coldrewind undo 7"))
        self.assertEqual(json.loads(row["error"])["step"]["changed_paths"], ["config/<redacted>.toml", "src/client.py"])
        (candidate,) = learn_candidates.candidates(store)
        self.assertEqual((candidate["evidence"], candidate["undo"], candidate["replayable"]), ([row["id"]], True, False))
        self.assertEqual(json.loads(self.cli(FIXTURE).stdout), dict(imported=0, skipped=1))

    def log(self, **changes):
        undone = dict(step_id=7, timestamp="2026-11-21T14:00:02Z", agent="Claude Code",
                      command=["sed", "-i", "s/timeout=30/timeout=0/", "config/<redacted>.toml"], reversibility="reversible",
                      reason="", pre_snapshot_id="snap-0006", post_snapshot_id="snap-0007", changed_paths=["config/<redacted>.toml"])
        undone.update(changes)
        return dict(schema_version=1, steps=[undone, self.undo()["step"]])

    def test_the_log_supplies_the_undone_steps_agent_and_command(self):
        result = self.cli(FIXTURE, self.log())
        self.assertEqual(result.returncode, 0, result.stderr)
        store = c.load_store(self.store)[1]
        (row,) = store["failures"].values()
        self.assertEqual((row["agent"], row["command"], row["occurred"], row["source_ref"]),
                         ("Claude Code", "sed -i s/timeout=30/timeout=0/ 'config/<redacted>.toml'", "2026-11-21T14:03:11Z",
                          "coldrewind:example/project:undo:8:2026-11-21T14%3A03%3A11Z"))
        self.assertEqual(json.loads(row["error"])["step"]["changed_paths"], ["config/<redacted>.toml", "src/client.py"])
        (candidate,) = learn_candidates.candidates(store)
        self.assertEqual((candidate["evidence"], candidate["undo"]), ([row["id"]], True))

    def test_a_log_without_the_undone_step_or_with_a_bad_shape_imports_nothing(self):
        log = self.log()
        cases = [self.log(step_id=6), dict(log, schema_version=2), dict(log, schema_version=True), {"steps": log["steps"]},
                 dict(log, extra=1), self.log(agent=""), self.log(command=[]), self.log(extra=1),
                 dict(log, steps=log["steps"] + [log["steps"][0]]), [log]]
        for case in cases:
            with self.subTest(case=case):
                result = self.cli(FIXTURE, case)
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertNotIn("Traceback", result.stderr)
                self.assertFalse(self.store.exists())

    def test_a_changed_path_with_a_newline_is_kept_unchanged(self):
        self.assertEqual(self.cli(self.write(self.undo(changed_paths=["odd\nname.py"]))).returncode, 0)
        (row,) = c.load_store(self.store)[1]["failures"].values()
        self.assertEqual(json.loads(row["error"])["step"]["changed_paths"], ["odd\nname.py"])

    def test_unknown_schema_version_and_bad_shapes_are_rejected_without_importing(self):
        value = self.undo()
        cases = [dict(value, schema_version=2), dict(value, schema_version="1"), dict(value, schema_version=True),
                 {"step": value["step"]}, dict(value, extra=1), {"schema_version": 1, "steps": [value["step"]]},
                 self.undo(command=[]), self.undo(changed_paths="src/client.py"), self.undo(agent=""),
                 self.undo(command=["coldrewind", "redo", "8"]), self.undo(command=["coldrewind", "undo", "0"]),
                 self.undo(command=["coldrewind", "undo", "7", "x"]), self.undo(agent="Claude Code"),
                 self.undo(timestamp="2026-11-21T14:03:11+00:60"), self.undo(changed_paths=["a\x00b"]),
                 self.undo(reversibility="maybe"), self.undo(timestamp="yesterday"), self.undo(step_id=0)]
        for case in cases:
            with self.subTest(case=case):
                result = self.cli(self.write(case))
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertNotIn("Traceback", result.stderr)
                self.assertFalse(self.store.exists())


if __name__ == "__main__":
    unittest.main()
