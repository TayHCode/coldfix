import json
import os
from pathlib import Path
import re
from unittest.mock import patch

from test_incident_admission import AdmissionCase


RELEASE_CHECK = os.environ.get("COLDFIX_RELEASE_CHECK")


def review_receipt(head="a" * 40):
    return dict(repo="example/fixture-app", pr=7, sha=head, model="gpt-6.1-terra", verdict="FAIL",
                summary="The arithmetic is wrong.", findings=["test_add_sums exposes subtraction where add must sum its operands."])


class ReviewImportTests(AdmissionCase):
    def setUp(self):
        super().setUp()
        fleet = patch.dict(os.environ, {"COLDFIX_FLEET_CLI": "acme"})
        fleet.start()
        self.addCleanup(fleet.stop)

    def test_cold_review_import_is_off_without_the_fleet_setting(self):
        source = self.cwd / "review.json"
        source.write_text(json.dumps(review_receipt()))
        env = {k: v for k, v in os.environ.items() if k != "COLDFIX_FLEET_CLI"}
        result = self.cli("ingest", "--format", "cold-review", "--input", str(source), code=2, env=env)
        self.assertIn("review receipt import is off: set COLDFIX_FLEET_CLI", result.stderr)

    def test_cold_review_import_keeps_identity_and_deduplicates(self):
        receipt = review_receipt()
        receipt["findings"] *= 2
        source = self.cwd / "review.json"
        source.write_text(json.dumps(receipt))
        result = json.loads(self.cli("ingest", "--format", "cold-review", "--input", str(source)).stdout)
        self.assertEqual(result["imported_records"], 1)
        row, = self.records("failures")
        self.assertEqual((row["kind"], row["agent"]), ("review", "gpt-6.1-terra"))
        self.assertEqual(json.loads(row["error"]), dict(finding=receipt["findings"][0], head="a" * 40))
        self.assertIn("#cold-review-" + "a" * 40 + "-", row["source_ref"])
        result = json.loads(self.cli("ingest", "--format", "cold-review", "--input", str(source)).stdout)
        self.assertEqual((result["imported_records"], result["skipped_duplicates"]), (0, 1))
        receipt["findings"].append("Keep zero operands unchanged.")
        source.write_text(json.dumps(receipt))
        self.assertEqual(json.loads(self.cli("ingest", "--format", "cold-review", "--input", str(source)).stdout)["imported_records"], 1)
        receipt["findings"].reverse()
        source.write_text(json.dumps(receipt))
        result = json.loads(self.cli("ingest", "--format", "cold-review", "--input", str(source)).stdout)
        self.assertEqual((result["imported_records"], result["skipped_duplicates"]), (0, 2))

    def test_matching_findings_from_different_reviewers_create_one_recurring_draft(self):
        source = self.cwd / "review.json"
        for index, model in enumerate(("gpt-6.1-terra", "gpt-6-luna")):
            receipt = review_receipt(("a" if index == 0 else "b") * 40)
            receipt.update(pr=7 + index, model=model)
            source.write_text(json.dumps(receipt))
            result = json.loads(self.cli("ingest-and-propose", "--format", "cold-review", "--input", str(source)).stdout)
        self.assertEqual((result["groups"], result["proposals"], result["delivered"], result["pending"]), (1, 1, [], 1))
        self.assertEqual({row["agent"] for row in self.records("failures")}, {"gpt-6.1-terra", "gpt-6-luna"})
        proposal, = self.records("proposals")
        self.assertEqual(len(proposal["evidence"]), 2)

    def test_invalid_receipts_write_no_failures_and_nonfailures_create_none(self):
        source = self.cwd / "review.json"
        for key, value in (("sha", "bad"), ("pr", True), ("findings", ["valid", 4]),
                           ("verdict", "PASS"), ("repo", "../repo"), ("model", "")):
            with self.subTest(field=key):
                receipt = dict(review_receipt(), **{key: value})
                source.write_text(json.dumps(receipt))
                self.cli("ingest", "--format", "cold-review", "--input", str(source), code=2)
                self.assertEqual(self.records("failures"), [])
        for verdict in ("PASS", "INCOMPLETE"):
            source.write_text(json.dumps(dict(review_receipt(), verdict=verdict, findings=[])))
            self.assertEqual(json.loads(self.cli("ingest", "--format", "cold-review", "--input", str(source)).stdout)["imported_records"], 0)


def release_stage(self):
    source = self.cwd / "staging"
    (source / "tests").mkdir(parents=True)
    files = {"README.md": "# Lesson staging\n## Limitations\nLocal proof fragments.\npython -m unittest discover -s tests -q\n",
             "CONTRIBUTING.md": "Review the lesson proof.\n", "SECURITY.md": "Keep source logs private.\n",
             "LICENSE": re.sub(r"(?m)^Copyright[^\n]*", "Copyright (c) 2026 Stage Contributors",
                               (Path(__file__).resolve().parents[1] / "LICENSE").read_text()),
             ".github/ISSUE_TEMPLATE/bug.md": "Describe the regression.\n",
             "tests/test_fragment.py": "from pathlib import Path\nimport unittest\nclass FragmentTests(unittest.TestCase):\n"
                 " def test_proof(self):\n  candidate, = Path('exports').glob('*.md')\n"
                 "  self.assertIn('Verification: proven', candidate.read_text())\n"}
    for name, body in files.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    command = ["python", "-m", "unittest", "discover", "-s", "tests", "-q"]
    (source / "release-check.json").write_text(json.dumps(dict(
        quickstart=[dict(file="README.md", line=4, argv=command)], checks=[dict(role="tests", argv=command)])))
    self.git(source, "init", "-q")
    self.git(source, "add", ".")
    self.git(source, "-c", "user.name=Stage Author", "-c", "user.email=stage-author", "commit", "-qm", "Staging checks")
    denylist = self.cwd / "terms.json"
    denylist.write_text('["private-fixture-term"]')
    return ["--public", "--release-check", RELEASE_CHECK, "--release-check-repo", str(source), "--denylist", str(denylist)]
