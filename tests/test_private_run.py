"""Synthetic retained inputs, exercised through the public CLI."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import coldfix as c


CLI = Path(c.__file__).resolve()


class PrivateRunTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.cwd = Path(self.temporary.name)
        self.seed = self.cwd / "seed"
        self.output = self.cwd / "output"
        self.source = self.cwd / "events.jsonl"
        self.manifest = self.cwd / "manifest.json"
        event = dict(command="check", error="seed failure", repo="example", agent="worker",
                     kind="test", source_ref="sample:one", occurred="2025-01-01T12:00:00Z")
        self.source.write_text("\n".join(json.dumps(dict(event, source_ref=ref))
                                        for ref in ("sample:one", "sample:two")))
        self.cli(self.seed, "ingest", "--input", str(self.source))
        _, seed = c.load_store(self.seed)
        self.check = self.cwd / "check.py"
        self.marker = self.cwd / "executed"
        self.check.write_text("from pathlib import Path\nPath(" + repr(str(self.marker)) + ").touch()\n")
        result = self.cli(self.seed, "lesson", "--group", c.signature(next(iter(seed["failures"].values()))),
                          "--kind", "test", "--level", "test", "--title", "Check value",
                          "--rule", "Verify the expected value.", "--check-command", "python3 {check}",
                          "--check-file", str(self.check), "--expected-failure", "value")
        self.lesson = json.loads(result.stdout)["id"]
        self.source.write_text(json.dumps(dict(event, command="runtime", error="private-canary-detail",
                                              kind="stuck", source_ref="sample:thread")) + "\n")
        self.data = dict(version=1, lesson_store="seed", sources=[dict(format="jsonl", input="events.jsonl")])
        self.write_manifest()

    def write_manifest(self):
        self.manifest.write_text(json.dumps(self.data))

    def cli(self, root, *args, code=0):
        result = subprocess.run([sys.executable, str(CLI), "--store", str(root), *args],
                                cwd=self.cwd, text=True, capture_output=True)
        self.assertEqual(result.returncode, code, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return result

    def run_cli(self, code=2):
        return self.cli(self.output, "run", "--input", str(self.manifest), code=code)

    def attempts(self):
        return sorted((self.output / "runs").glob("run-*"))

    def hashes(self, root):
        return {str(p.relative_to(root)): c.digest(p.read_text()) for p in root.rglob("*") if p.is_file()}

    def test_run_retains_lessons_ingests_groups_measures_and_does_not_execute(self):
        before = self.hashes(self.seed)
        source_before = self.source.read_bytes()
        result = self.run_cli()
        self.assertEqual(json.loads(result.stdout), dict(events=3, groups=2, lessons=1, receipts=0,
                         outcomes=1, model_calls=0, status="INCOMPLETE"))
        self.assertEqual(result.stderr, "")
        self.assertNotIn("private-canary", result.stdout)
        self.assertNotIn(str(self.cwd), result.stdout)
        self.assertFalse(self.marker.exists())
        attempt, = self.attempts()
        _, store = c.load_store(attempt)
        lesson, = c.latest_lessons(store).values()
        self.assertEqual((lesson["status"], lesson["verification"]), ("proposed", "unproven"))
        self.assertEqual(lesson["id"], self.lesson)
        outcome, = store["outcomes"].values()
        self.assertEqual(outcome["recurrences"], dict(before=None, after=None))
        self.assertIsNone(outcome["activation"])
        event, = [r for r in store["failures"].values() if r["source_ref"] == "sample:thread"]
        self.assertEqual((event["occurred"], event["kind"]), ("2025-01-01T12:00:00Z", "stuck"))
        inputs = json.loads((attempt / "inputs.json").read_text())
        self.assertEqual(inputs["sources"][0]["sha256"], c.digest(self.source.read_text()))
        marker = json.loads((attempt / "run.json").read_text())
        self.assertEqual(marker["inputs_sha256"], c.digest(c.encoded(inputs)))
        self.assertEqual(marker["summary"], json.loads(result.stdout))
        self.assertEqual(self.hashes(self.seed), before)
        self.assertEqual(self.source.read_bytes(), source_before)
        for p in [self.output, *self.output.rglob("*")]:
            self.assertEqual(p.stat().st_mode & 0o777, 0o700 if p.is_dir() else 0o600)

    def test_reruns_and_concurrent_runs_keep_independent_counts_and_history(self):
        self.run_cli()
        original, = self.attempts()
        before = self.hashes(original)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.run_cli(), range(2)))
        self.assertEqual(len(self.attempts()), 3)
        for result in results:
            self.assertEqual(json.loads(result.stdout)["events"], 3)
        self.assertEqual(self.hashes(original), before)
        for attempt in self.attempts():
            _, store = c.load_store(attempt)
            self.assertEqual(len(store["outcomes"]), 1)

    def test_validates_every_source_and_measurement_before_output(self):
        bad = self.cwd / "bad.jsonl"
        bad.write_text('{"secret-canary-field": "private-canary-detail"}\n')
        self.data["sources"].append(dict(format="jsonl", input="bad.jsonl"))
        self.write_manifest()
        result = self.run_cli()
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "coldfix: invalid private run input; no run written\n")
        self.assertFalse(self.output.exists())
        self.data["sources"].pop()
        bad.write_text('{"version":1,"activations":[{}]}')
        self.data["measurement_input"] = "bad.jsonl"
        self.write_manifest()
        self.run_cli()
        self.assertFalse(self.output.exists())

    def test_labels_for_imported_events_survive_reruns(self):
        event = json.loads(self.source.read_text())
        self.source.write_text("\n".join(json.dumps(dict(event, source_ref=ref))
                                        for ref in ("sample:first", "sample:second")))
        self.run_cli()
        original, = self.attempts()
        _, first = c.load_store(original)
        ids = sorted(r["id"] for r in first["failures"].values()
                     if r["source_ref"] in ("sample:first", "sample:second"))
        self.assertEqual(len(ids), 2)
        pair = dict(left=ids[0], right=ids[1], same=True)
        (self.cwd / "labels.json").write_text(json.dumps(dict(version=1, pairs=[pair])))
        self.data["measurement_input"] = "labels.json"
        self.write_manifest()
        self.run_cli()
        rerun, = [p for p in self.attempts() if p != original]
        _, second = c.load_store(rerun)
        report, = second["measurements"].values()
        self.assertEqual(report["dedup"]["status"], "PASS")
        self.assertEqual(report["input"]["pairs"], [pair])
        for score in report["dedup"]["by_grouping"].values():
            self.assertEqual((score["pairs"], score["false_merges"], score["missed_merges"]), (1, 0, 0))
        self.assertEqual(set(first["failures"]), set(second["failures"]))

    def test_rejects_ambiguous_manifest_empty_sources_and_missing_lessons(self):
        cases = [dict(version=True), dict(window_days=True), dict(window_days=0), dict(window_days="1"),
                 dict(sources=[]), dict(sources=[dict(format="jsonl", input="-")]),
                 dict(sources=self.data["sources"] * 2), dict(unexpected="private-canary-detail"),
                 dict(lesson_store="missing"), dict(sources=[dict(format="other", input="events.jsonl")])]
        original = dict(self.data)
        for changes in cases:
            with self.subTest(fields=list(changes)):
                self.data = dict(original, **changes)
                self.write_manifest()
                self.run_cli()
                self.assertFalse(self.output.exists())
        self.manifest.write_text('{"version":1,"version":1}')
        self.run_cli()
        self.assertFalse(self.output.exists())

    def test_rejects_symlinks_and_nonprivate_output_without_changing_existing_files(self):
        link = self.cwd / "link.jsonl"
        link.symlink_to(self.source)
        self.data["sources"][0]["input"] = "link.jsonl"
        self.write_manifest()
        self.run_cli()
        self.assertFalse(self.output.exists())
        self.data["sources"][0]["input"] = "events.jsonl"
        self.write_manifest()
        self.output.mkdir(mode=0o755)
        self.output.chmod(0o755)
        sentinel = self.output / "unfamiliar"
        sentinel.write_text("preserve")
        self.run_cli()
        self.assertEqual(sentinel.read_text(), "preserve")
        self.assertFalse((self.output / "runs").exists())
        self.output.chmod(0o700)
        (self.output / "runs").symlink_to(self.seed)
        before = self.hashes(self.seed)
        self.run_cli()
        self.assertEqual(self.hashes(self.seed), before)

    def test_agentfleet_evidence_uses_existing_adapter_and_rejects_incomplete_collection(self):
        evidence = dict(status="PASS", window=dict(since="2025-01-01", until="2025-01-02", days=1),
                        repos_scanned=1, errors=[], **{key: [] for key in c.AGENTFLEET_SECTIONS})
        evidence["corrections"] = [dict(repo="example", date="2025-01-01", session="example-thread",
                                            text="private-canary-detail")]
        self.source.write_text(json.dumps(evidence))
        self.data["sources"][0]["format"] = "agentfleet"
        self.write_manifest()
        self.run_cli()
        _, store = c.load_store(self.attempts()[0])
        correction, = [r for r in store["failures"].values() if r["kind"] == "correction"]
        self.assertEqual(correction["source_ref"], str(self.source) + "#/corrections/0")
        self.assertEqual(correction["occurred"], "2025-01-01")
        evidence["status"] = "INCOMPLETE"
        self.source.write_text(json.dumps(evidence))
        self.run_cli()
        self.assertEqual(len(self.attempts()), 1)

    def test_interrupted_write_leaves_no_completion_marker_and_retry_succeeds(self):
        from private_run import run
        from argparse import Namespace
        with patch("private_run.m.measure", side_effect=OSError("synthetic interruption")):
            with self.assertRaises(OSError):
                run(Namespace(store=str(self.output), input=str(self.manifest)))
        partial, = self.attempts()
        self.assertFalse((partial / "run.json").exists())
        before = self.hashes(partial)
        self.run_cli()
        self.assertEqual(len(self.attempts()), 2)
        self.assertEqual(self.hashes(partial), before)
        self.assertEqual(sum((p / "run.json").exists() for p in self.attempts()), 1)

    def test_measurement_input_is_retained_and_failure_status_reaches_cli(self):
        measurement = dict(version=1, privacy=[dict(case_id="synthetic-leak", blocked=False,
                           leaks=1, evidence_ref="sample:privacy")])
        path = self.cwd / "measurement.json"
        path.write_text(json.dumps(measurement))
        self.data.update(measurement_input="measurement.json", window_days=7)
        self.write_manifest()
        result = self.run_cli(code=1)
        self.assertEqual(json.loads(result.stdout)["status"], "FAIL")
        attempt, = self.attempts()
        _, store = c.load_store(attempt)
        report, = store["measurements"].values()
        self.assertEqual(report["window_days"], 7)
        self.assertEqual(report["input"]["privacy"], measurement["privacy"])
        inputs = json.loads((attempt / "inputs.json").read_text())
        self.assertEqual(inputs["measurement_input"]["sha256"], c.digest(path.read_text()))

    def test_keeps_replay_proof_without_inventing_activation(self):
        repo = self.cwd / "history"
        repo.mkdir()

        def git(*args):
            return subprocess.run(["git", "-C", str(repo), *args], check=True,
                                  text=True, capture_output=True).stdout.strip()

        git("init", "--quiet")
        revisions = []
        for value in ("bad", "good"):
            (repo / "value").write_text(value)
            git("add", "value")
            git("-c", "user.name=Example", "-c", "user.email=example", "-c", "commit.gpgsign=false",
                "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m", value)
            revisions.append(git("rev-parse", "HEAD"))
        self.check.write_text('from pathlib import Path\nimport sys\n'
                             'ok = Path("value").read_text() == "good"\n'
                             'print(\'{"status":"PASS"}\' if ok else \'{"status":"FAIL","failure":"value"}\')\n'
                             'sys.exit(0 if ok else 1)\n')
        revised = json.loads(self.cli(self.seed, "lesson", "--revise", self.lesson,
                                     "--check-file", str(self.check)).stdout)["id"]
        proven = json.loads(self.cli(self.seed, "replay", "--lesson", revised, "--repo", str(repo),
                                    "--before", revisions[0], "--after", revisions[1], "--status", "active").stdout)
        seed_before = self.hashes(self.seed)
        self.run_cli()
        _, store = c.load_store(self.attempts()[0])
        tip, = c.latest_lessons(store).values()
        self.assertEqual((tip["id"], tip["verification"], tip["status"]), (proven["id"], "proven", "active"))
        outcome, = store["outcomes"].values()
        self.assertEqual(outcome["replay_result"]["status"], "PASS")
        self.assertIsNone(outcome["activation"])
        self.assertEqual(outcome["status"], "INCOMPLETE")
        self.assertEqual(self.hashes(self.seed), seed_before)


if __name__ == "__main__":
    unittest.main()
