import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import coldrecall as cr
import test_coldrecall as fixture

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "bench" / "coldrecall_speed.py"


class SpeedBenchmarkTests(fixture.ImageCase):
    DIRECTORY_NAME = "source"

    def bench(self, *extra):
        workdir = Path(tempfile.mkdtemp(dir=self.repo.parent))
        report = workdir / "report.json"
        result = fixture.run(sys.executable, str(BENCH), "--repo", str(self.repo), "--workdir", str(workdir / "work"),
                             "--sizes", "1,3", "--queries", "5", "--cold-starts", "2", "--runs", "2",
                             "--out", str(report), *extra, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(report.read_text()), workdir

    def test_runs_on_tiny_fixture_and_reports_every_measure(self):
        report, workdir = self.bench()
        self.assertEqual(report["config"]["repo_records"], 1)
        self.assertEqual(len(report["runs"]), 2)
        summary = report["summary"]
        for size in (1, 3):
            self.assertEqual(summary[f"ingest.n{size}.ingested"]["median"], size)
            self.assertEqual(summary[f"ingest.n{size}.witnesses_passing"]["median"], size)
            self.assertEqual(summary[f"recall.n{size}.facts_per_hit"]["median"], size)
            for key in (f"ingest.n{size}.cold_records_per_s", f"recall.n{size}.hit_p50_ms", f"recall.n{size}.miss_p95_ms"):
                self.assertGreater(summary[key]["median"], 0)
        keys = ["cold_start.python_floor_ms_median", "cold_start.lookup_hit_ms_median"]
        if os.access("/usr/bin/time", os.X_OK):
            keys += ["cold_start.hook_hit_rss_mb", "ingest.n3.compile_peak_rss_mb"]
        for key in keys:
            self.assertGreater(summary[key]["median"], 0)
        if "cold_start.hook_hit_rss_mb" in summary:
            self.assertLess(summary["cold_start.python_floor_rss_mb"]["median"], summary["cold_start.lookup_hit_rss_mb"]["median"])
        for stats in summary.values():
            self.assertLessEqual(stats["min"], stats["median"])
            self.assertLessEqual(stats["median"], stats["max"])
        self.assertEqual(list((workdir / "work").iterdir()), [])

    def test_leaves_the_source_repository_untouched(self):
        before = self.git("status", "--short", "--ignored")
        self.bench()
        self.assertEqual(self.git("status", "--short", "--ignored"), before)
        self.assertFalse((self.repo / cr.DIRECTORY).exists())

    def test_accepts_a_relative_workdir_from_another_directory(self):
        result = fixture.run(sys.executable, str(BENCH), "--repo", str(self.repo), "--workdir", "work",
                             "--sizes", "1", "--queries", "3", "--cold-starts", "1", "--runs", "1",
                             "--out", "report.json", cwd=self.repo.parent)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((self.repo.parent / "report.json").read_text())["config"]["sizes"], [1])

    def test_never_deletes_what_it_does_not_own(self):
        workdir = Path(tempfile.mkdtemp(dir=self.repo.parent))
        kept = workdir / "scratch"
        kept.mkdir()
        (kept / "keep.txt").write_text("mine")
        result = fixture.run(sys.executable, str(BENCH), "--repo", str(self.repo), "--workdir", str(workdir),
                             "--sizes", "1", "--queries", "3", "--cold-starts", "1", "--runs", "1",
                             "--out", str(workdir / "report.json"), cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((kept / "keep.txt").read_text(), "mine")

    def test_refuses_to_report_hits_when_no_fact_is_anchored(self):
        self.install(fixture.rule_file("0" * 40, "1" * 40))
        workdir = Path(tempfile.mkdtemp(dir=self.repo.parent))
        result = fixture.run(sys.executable, str(BENCH), "--repo", str(self.repo), "--workdir", str(workdir),
                             "--sizes", "1", "--queries", "3", "--cold-starts", "1", "--runs", "1",
                             "--out", str(workdir / "report.json"), cwd=ROOT)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no fact is anchored", result.stderr)
        self.assertFalse((workdir / "report.json").exists())


if __name__ == "__main__":
    unittest.main()
