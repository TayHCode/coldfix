"""Local synthetic Coldpass run results. No provider, network, or model calls."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import coldfix as c
import collect_sources
import coldpass_sources as sources

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "coldpass_sibling_import"
SIBLING = "pkg/numdiff.py uses bare absolute imports of sibling modules instead of relative imports."


def run_row(run_id, outcome, *, owner=None, evidence="partly_checked", **fields):
    row = dict(run_id=run_id, date="2026-10-20", repo="example/project", model="fixture-model",
               commit="a" * 40, outcome=outcome, purpose="verify the project", stuck_step="npm test",
               what_failed="Expected count 2, received 1", error_excerpt=None, suggested_fix=None,
               failure_origin=None, evidence_strength=evidence,
               fault=None if owner is None else dict(owner=owner))
    row.update(fields)
    return row


CASES = {
    "pass": run_row("ok", "pass"),
    "harness-outcome": run_row("h1", "harness_error"),
    "harness-owner": run_row("h2", "blocked", owner="test"),
    "setup-outcome": run_row("s1", "needs_credentials", what_failed="PRIVATE-CANARY token missing"),
    "setup-owner": run_row("s2", "blocked", owner="prerequisite"),
    "product-blocked": run_row("p1", "blocked", owner="repo"),
    "product-owner": run_row("p2", "unverified", owner="repo", evidence="readme_checked"),
    "product-unchecked": run_row("p3", "blocked", owner="repo", evidence="not_checked"),
    "inconclusive-slow": run_row("i1", "too_slow"),
    "inconclusive-weak": run_row("i2", "blocked", evidence="not_checked"),
}

CLASS_TEXT = {
    "sibling-import": SIBLING,
    "missing-file": "The templates directory is missing from the installed package.",
    "readme-mismatch": "README Quick Start calls Graph() without the required graph_id argument.",
    "undeclared-dependency": "The CLI imports playwright, which is not among installed dependencies.",
    "stale-test": "Update tests/test_core.py to match the new rounding.",
    "uncaught-error-exit": "The command exits with code 1 and prints an uncaught KeyError.",
    "not-repo": "The checker could not be checked in this guest.",
    None: "Expected count 2, received 1",
}


def commit(repo, message):
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Example", "-c", "user.email=example@example.invalid",
                    "commit", "--quiet", "-m", message], check=True)
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()


class ColdpassSourcesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = self.root / ".coldfix"
        self.runs = self.root / "runs.jsonl"

    def write(self, rows):
        self.runs.write_bytes(b"".join(json.dumps(row, ensure_ascii=False).encode() + b"\n" for row in rows))

    def cli(self, *args, code=0):
        result = subprocess.run([sys.executable, sources.__file__, "--input", str(self.runs),
                                 "--store", str(self.store), *args], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, code, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("PRIVATE-CANARY", result.stdout + result.stderr)
        self.assertNotIn(str(self.root), result.stdout + result.stderr)
        return result

    def stored(self):
        return list(c.load_store(self.store)[1]["failures"].values())

    def coldfix(self, *args):
        result = subprocess.run([sys.executable, c.__file__, "--store", str(self.store), *args],
                                cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_checked_unknown_is_not_a_repo_failure(self):
        rows, summary = sources.collect(json.dumps(run_row("unknown", "blocked")).encode())
        self.assertEqual(rows, [])
        self.assertEqual(summary["buckets"], {"inconclusive": 1})

    def test_recheck_pairs_require_owned_failure_later_pass_and_new_commit(self):
        red = run_row("red", "blocked", owner="repo")
        green = run_row("green", "pass", date="2026-10-21", commit="b" * 40)
        for change in ({}, {"repo": "other/project"}, {"commit": "a" * 40},
                       {"date": "2026-10-19"}, {"commit": None}, {"outcome": "blocked"}):
            with self.subTest(change=change):
                self.write([red, dict(green, **change)])
                records, summary = sources.collect(self.runs.read_bytes())
                if change:
                    self.assertEqual(summary["rechecks"], [])
                else:
                    self.assertEqual(summary["rechecks"], [dict(
                        failure_id=records[0]["id"], repo="example/project",
                        before="a" * 40, after="b" * 40, run_id="red", pass_run_id="green",
                        occurred="2026-10-20", passed="2026-10-21")])

    def test_recheck_drafts_without_manifest_but_cannot_claim_replay_ready(self):
        import learn_daily
        red = run_row("red", "blocked", owner="repo")
        green = run_row("green", "pass", date="2026-10-21", commit="b" * 40)
        self.write([red, green])
        self.cli()
        store = c.load_store(self.store)[1]
        pairs = json.loads((self.store / sources.RECHECKS).read_text())
        rows = learn_daily.waiting(store, {"drafts": {}}, [], pairs)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["evidence"], [pairs[0]["failure_id"]])
        self.assertFalse(rows[0]["readiness"]["ready"])
        self.assertFalse(rows[0]["replayable"])
        self.assertEqual(rows[0]["recheck_pair"]["after"], "b" * 40)
        self.assertEqual(learn_daily.waiting(store, {"drafts": {}}, []), [])

    def test_each_case_lands_in_its_bucket(self):
        expected = {"pass": "pass", "harness-outcome": "harness", "harness-owner": "harness",
                    "setup-outcome": "setup", "setup-owner": "setup", "product-blocked": "product",
                    "product-owner": "product", "product-unchecked": "product",
                    "inconclusive-slow": "inconclusive", "inconclusive-weak": "inconclusive"}
        for name, row in CASES.items():
            self.assertEqual(sources.classify(sources.normalize(row)), expected[name], name)

    def test_keys_or_hardware_setup_does_not_block_product_collection(self):
        self.write([run_row("setup", "needs_keys_or_hardware"), CASES["product-blocked"]])
        config = self.root / "sources.json"
        config.write_text(json.dumps({"coldpass": {"format": "coldpass-runs",
                                                   "command": ["cat", str(self.runs)]}}))
        result = collect_sources.collect(self.store, config)
        self.assertEqual(result["coldpass"], dict(imported=1, deduped=0, rejected=0, unavailable=0))
        self.assertEqual([row["repo"] for row in self.stored()], ["example/project"])
        records, receipt = sources.collect(self.runs.read_bytes())
        self.assertEqual(len(records), 1)
        self.assertEqual(receipt["buckets"], {"product": 1, "setup": 1})
        self.assertEqual(receipt["noise"], {"setup:needs_keys_or_hardware": 1})

    def test_failure_class_comes_from_the_run_evidence(self):
        for expected, text in CLASS_TEXT.items():
            with self.subTest(expected):
                run = sources.normalize(run_row("x", "blocked", owner="repo", what_failed=None, suggested_fix=text))
                self.assertEqual(sources.failure_class(run), expected)
        boilerplate = run_row("x", "blocked", owner="repo", suggested_fix=sources.BOILERPLATE)
        self.assertIsNone(sources.failure_class(sources.normalize(boilerplate)))

    def test_a_passing_mention_of_credentials_keys_or_the_checker_is_still_product_evidence(self):
        for text in ("The CLI crashes when an API key is invalid.",
                     'product invocation failed: {"returncode": 1, "needs_credentials": false}',
                     "Command failed on /tmp/run/sample.py-checker-0a1b and exits 2.",
                     "Python worker startup and tool call fail in checker replay.",
                     "It exits 1 and prints JSON to stderr, which the checker treats as a failure.",
                     "The CLI crashes when a pseudo-terminal is enabled.",
                     "Login requires authentication but the CLI crashes on a bad token.",
                     "The checker subprocess shows the CLI exits 1."):
            with self.subTest(text):
                run = sources.normalize(run_row("x", "blocked", owner="repo", what_failed=text))
                self.assertIsNone(sources.failure_class(run))
        for text in ("No LLM API key in this guest.", "The checker does not provide a pseudo-terminal.",
                     "The checker runs CLI argv without a pseudo-terminal.",
                     "Checker subprocess working directory differs from probe cwd.",
                     "Expected output file could not be checked: no such file.",
                     "The library is import-only.", "No fix needed for the product in this run."):
            with self.subTest(text):
                run = sources.normalize(run_row("x", "blocked", owner="repo", what_failed=text))
                self.assertEqual(sources.failure_class(run), "not-repo")

    def test_a_named_class_wins_over_a_passing_mention_of_credentials_or_the_checker(self):
        for extra in (" Generation would also need provider credentials.", " The checker could not replay it."):
            with self.subTest(extra):
                text = CLASS_TEXT["readme-mismatch"] + extra
                run = sources.normalize(run_row("x", "blocked", owner="repo", what_failed=text))
                self.assertEqual(sources.failure_class(run), "readme-mismatch")

    def test_only_product_failures_are_ingested_and_noise_is_counted(self):
        self.write([*CASES.values(), run_row("n1", "blocked", owner="repo", what_failed=CLASS_TEXT["not-repo"])])
        summary = json.loads(self.cli().stdout)
        self.assertEqual(summary["buckets"], {"harness": 2, "inconclusive": 2, "pass": 1, "product": 4, "setup": 2})
        self.assertEqual((summary["records"], summary["imported"], summary["skipped"]), (3, 3, 0))
        self.assertEqual(summary["noise"]["setup:needs_credentials"], 1)
        self.assertEqual(summary["noise"]["product:not-repo"], 1)
        self.assertEqual(summary["classes"], {"unclassified": {"runs": 3, "repos": ["example/project"]}})
        rows = self.stored()
        self.assertEqual(sorted(row["source_ref"] for row in rows),
                         ["coldpass:run:p1:unclassified", "coldpass:run:p2:unclassified", "coldpass:run:p3:unclassified"])
        for row in rows:
            self.assertEqual((row["kind"], row["repo"], row["agent"], row["occurred"], row["command"]),
                             ("test", "example/project", "fixture-model", "2026-10-20", "coldpass unclassified"))
            metadata = json.loads(row["error"])
            self.assertEqual((metadata["type"], metadata["diagnostic"], metadata["commit"]),
                             ("coldpass", "Expected count 2, received 1", "a" * 40))

    def test_same_class_groups_across_repos_and_unclassified_never_does(self):
        rows = [run_row("a", "blocked", owner="repo", repo="one/x", what_failed=SIBLING),
                run_row("b", "blocked", owner="repo", repo="two/y", what_failed="Use package-relative imports in core.py."),
                run_row("c", "blocked", owner="repo", repo="one/x"),
                run_row("d", "blocked", owner="repo", repo="two/y")]
        records, receipt = sources.collect(b"".join(json.dumps(row).encode() + b"\n" for row in rows))
        signatures = {r["source_ref"].split(":")[2]: c.signature(r) for r in records}
        self.assertEqual(signatures["a"], signatures["b"])
        self.assertNotEqual(signatures["c"], signatures["d"])
        self.assertNotIn(signatures["c"], (signatures["a"], signatures["d"]))
        self.assertEqual(receipt["classes"]["sibling-import"], {"runs": 2, "repos": ["one/x", "two/y"]})

    def test_rerun_is_idempotent(self):
        self.write(CASES.values())
        self.cli()
        before = {row["id"]: row for row in self.stored()}
        summary = json.loads(self.cli().stdout)
        self.assertEqual((summary["imported"], summary["skipped"]), (0, 3))
        self.assertEqual({row["id"]: row for row in self.stored()}, before)

    def test_unicode_separators_survive_collect_and_ingest(self):
        diagnostic = "line\u2028two\u2029three\u0085four"
        self.write([run_row("u1", "blocked", owner="repo", what_failed=diagnostic)])
        self.assertIn(b"\xe2\x80\xa8", self.runs.read_bytes())
        self.cli()
        self.assertEqual([json.loads(row["error"])["diagnostic"] for row in self.stored()], [diagnostic])

    def test_input_is_never_modified(self):
        self.write(CASES.values())
        stat = self.runs.stat()
        before = (self.runs.read_bytes(), stat.st_mtime_ns, stat.st_mode)
        self.cli()
        self.cli("--dry-run")
        after = self.runs.stat()
        self.assertEqual((self.runs.read_bytes(), after.st_mtime_ns, after.st_mode), before)

    def test_dry_run_writes_nothing(self):
        self.write(CASES.values())
        summary = json.loads(self.cli("--dry-run").stdout)
        self.assertTrue(summary["dry_run"])
        self.assertEqual(summary["records"], 3)
        self.assertNotIn("imported", summary)
        self.assertFalse(self.store.exists())

    def test_store_modes_and_input_hash(self):
        self.write(CASES.values())
        summary = json.loads(self.cli().stdout)
        self.assertEqual(summary["input_sha256"], hashlib.sha256(self.runs.read_bytes()).hexdigest())
        self.assertEqual(self.store.stat().st_mode & 0o777, 0o700)
        for path in self.store.rglob("*"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600, path.name)

    def test_invalid_input_publishes_nothing(self):
        bad = {
            "unknown outcome": [run_row("a", "mystery")],
            "missing field": [{k: v for k, v in run_row("a", "blocked").items() if k != "fault"}],
            "missing commit": [{k: v for k, v in run_row("a", "blocked").items() if k != "commit"}],
            "bad type": [run_row("a", "blocked", what_failed=5)],
            "bad date": [run_row("a", "blocked", date="yesterday")],
            "duplicate run id": [run_row("a", "blocked", owner="repo"), run_row("a", "blocked", owner="repo")],
        }
        for name, rows in bad.items():
            with self.subTest(name):
                self.write(rows)
                self.cli(code=2)
                self.assertFalse(self.store.exists())
        self.runs.write_bytes(b'{"run_id": \n')
        self.cli(code=2)
        self.assertFalse(self.store.exists())

    def test_store_must_be_named_coldfix(self):
        self.write(CASES.values())
        result = subprocess.run([sys.executable, sources.__file__, "--input", str(self.runs),
                                 "--store", str(self.root / "elsewhere")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertFalse((self.root / "elsewhere").exists())

    def test_hourly_collector_reads_the_ledger(self):
        self.write(CASES.values())
        config = self.root / "sources.json"
        config.write_text(json.dumps({"coldpass": {"format": "coldpass-runs", "command": ["cat", str(self.runs)]}}))
        self.store.mkdir(mode=0o700)
        for imported, deduped in ((3, 0), (0, 3)):
            result = collect_sources.collect(self.store, config)["coldpass"]
            self.assertEqual((result["imported"], result["deduped"], result["unavailable"]), (imported, deduped, 0))
        self.assertEqual(len(self.stored()), 3)

    def test_hourly_collector_rejects_an_invalid_row_and_still_imports_the_rest(self):
        self.write([CASES["product-blocked"], run_row("nul", "harness_error", stuck_step="npm\x00test"),
                    CASES["product-owner"], CASES["product-owner"]])
        config = self.root / "sources.json"
        config.write_text(json.dumps({"coldpass": {"format": "coldpass-runs", "command": ["cat", str(self.runs)]}}))
        self.store.mkdir(mode=0o700)
        result = collect_sources.collect(self.store, config)["coldpass"]
        self.assertEqual(result, dict(imported=2, deduped=0, rejected=2, unavailable=0,
                                      rejection_reasons={"invalid Coldpass row": 2}))
        self.assertEqual(len(self.stored()), 2)
        self.cli(code=2)

    def test_sibling_import_class_is_proven_by_replay_and_exported(self):
        self.write([run_row("r1", "blocked", owner="repo", repo="fixture-org/alpha-solver", what_failed=SIBLING),
                    run_row("r2", "unverified", owner="repo", repo="fixture-org/beta-client", suggested_fix="Use package-relative imports.")])
        self.cli()
        group = {c.signature(row) for row in self.stored()}
        self.assertEqual(len(group), 1)
        repo = self.root / "repo"
        (repo / "pkg").mkdir(parents=True)
        (repo / "pkg" / "__init__.py").write_text("")
        shutil.copy(FIXTURES / "helpers.py", repo / "pkg" / "helpers.py")
        shutil.copy(FIXTURES / "bad_total.py", repo / "pkg" / "total.py")
        subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
        before = commit(repo, "bare sibling import")
        shutil.copy(FIXTURES / "good_total.py", repo / "pkg" / "total.py")
        after = commit(repo, "relative import")
        shutil.copy(FIXTURES / "check.py", self.root / "check.py")
        lesson = self.coldfix("lesson", "--group", group.pop(), "--grouping", "normalized", "--kind", "lint", "--level", "lint",
                              "--title", "Import modules inside a package relatively",
                              "--rule", "A package module imports its own package's modules relatively, never by bare name.",
                              "--failure", "A package module imported a sibling module by bare name.",
                              "--fix", "Use from .module import name.", "--check-file", "check.py",
                              "--check-command", "python3 {check}", "--expected-failure", "bare-internal-import")["lesson_id"]
        replay = self.coldfix("replay", "--lesson", lesson, "--repo", str(repo), "--before", before, "--after", after,
                              "--status", "active")
        self.assertEqual((replay["status"], replay["verification"]), ("PASS", "proven"))
        (self.root / "exports").mkdir()
        self.assertTrue(self.coldfix("export", "--lesson", lesson, "--output", "exports/sibling-import.md")["exported"])
        record = (self.root / "exports" / "sibling-import.md").read_text()
        self.assertIn('"expected_failure": "bare-internal-import"', record)
        self.assertIn(before, record)
        self.assertIn(after, record)


class SiblingImportFixtureTests(unittest.TestCase):
    def check(self, total, files=(), installed=()):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as site:
            for name, text in dict(installed).items():
                (Path(site) / name).write_text(text)
            package = Path(folder) / "pkg"
            package.mkdir()
            (package / "__init__.py").write_text("")
            shutil.copy(FIXTURES / "helpers.py", package / "helpers.py")
            shutil.copy(FIXTURES / total, package / "total.py")
            for name, text in dict(files).items():
                (package / name).parent.mkdir(parents=True, exist_ok=True)
                (package / name).write_text(text)
            result = subprocess.run([sys.executable, FIXTURES / "check.py"], cwd=folder, capture_output=True, text=True,
                                    env={**os.environ, "PYTHONPATH": site})
        return result.returncode, json.loads(result.stdout), result.stderr

    def test_bad_fixture_fails_with_the_import_line_and_good_fixture_passes(self):
        code, out, err = self.check("bad_total.py")
        self.assertEqual((code, out), (1, {"status": "FAIL", "failure": "bare-internal-import"}))
        self.assertIn("pkg/total.py:1", err)
        self.assertEqual(self.check("good_total.py")[:2], (0, {"status": "PASS"}))

    def test_a_sibling_named_like_a_standard_library_module_fails_only_when_the_standard_library_lacks_the_name(self):
        for shadowed in ("json", "typing"):
            with self.subTest(shadowed):
                code, out, err = self.check("good_total.py", {shadowed + ".py": "def helper(): pass\n",
                                                              "report.py": f"from {shadowed} import helper\n"})
                self.assertEqual((code, out), (1, {"status": "FAIL", "failure": "bare-internal-import"}))
                self.assertIn("pkg/report.py:1", err)
        stdlib = self.check("good_total.py", {"json.py": "def helper(): pass\n",
                                              "report.py": "import json\nfrom json import dumps, decoder\n"})
        self.assertEqual(stdlib[:2], (0, {"status": "PASS"}))

    def test_an_unparsable_module_is_incomplete_but_a_found_bare_import_still_fails(self):
        self.assertEqual(self.check("good_total.py", {"broken.py": "def broken(:\n"})[:2], (2, {"status": "INCOMPLETE"}))
        self.assertEqual(self.check("bad_total.py", {"broken.py": "def broken(:\n"})[:2],
                         (1, {"status": "FAIL", "failure": "bare-internal-import"}))

    def test_a_top_level_subpackage_imported_by_bare_name_from_a_nested_module_fails(self):
        code, out, err = self.check("good_total.py", {"utils/__init__.py": "def deprecated(): pass\n",
                                                      "sub/__init__.py": "", "sub/details.py": "from utils import deprecated\n"})
        self.assertEqual((code, out), (1, {"status": "FAIL", "failure": "bare-internal-import"}))
        self.assertIn("pkg/sub/details.py:1", err)

    def test_a_deeply_nested_or_test_module_does_not_shadow_a_third_party_import(self):
        files = {"tests/requests.py": "", "compat/__init__.py": "", "compat/requests.py": "",
                 "client.py": "import requests\n", "tests/test_client.py": "from helpers import double\n"}
        self.assertEqual(self.check("good_total.py", files)[:2], (0, {"status": "PASS"}))

    def test_an_installed_module_named_like_a_sibling_is_imported_as_python_resolves_it(self):
        files = {"extlib.py": "def other(): pass\n", "client.py": "import extlib\nfrom extlib import get\n"}
        installed = {"extlib.py": "def get(): pass\n"}
        self.assertEqual(self.check("good_total.py", files, installed)[:2], (0, {"status": "PASS"}))
        self.assertEqual(self.check("good_total.py", files)[:2], (1, {"status": "FAIL", "failure": "bare-internal-import"}))
        files["client.py"] = "from extlib import other\n"
        self.assertEqual(self.check("good_total.py", files, installed)[:2],
                         (1, {"status": "FAIL", "failure": "bare-internal-import"}))

    def test_a_tree_without_a_package_is_incomplete(self):
        with tempfile.TemporaryDirectory() as folder:
            result = subprocess.run([sys.executable, FIXTURES / "check.py"], cwd=folder, capture_output=True, text=True)
        self.assertEqual((result.returncode, json.loads(result.stdout)), (2, {"status": "INCOMPLETE"}))


if __name__ == "__main__":
    unittest.main()
