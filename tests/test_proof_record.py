import argparse
import copy
import json
import subprocess
import unittest

import coldfix as c
import lesson_delivery
import test_cli
import test_lesson_delivery

PROOF = dict(
    schema_version=1,
    lesson="coldfix:lesson:a:b",
    source_evidence=["coldfix:failure:one"],
    what_went_wrong="The value was invalid.",
    fix="Write the expected value.",
    rule="Check the value before accepting changes.",
    replay=dict(receipt="coldfix:replay:r", expected_failure="value", check_sha256="a" * 64,
                before=dict(commit="1" * 40, tree="2" * 40, exit=1, status="FAIL"),
                after=dict(commit="3" * 40, tree="4" * 40, exit=0, status="PASS")))

BROKEN = [
    ("source failure refs", "source_evidence", []),
    ("source failure refs", "source_evidence", [" "]),
    ("what_went_wrong", "what_went_wrong", ""),
    ("fix", "fix", "  "),
    ("rule", "rule", ""),
    ("replay receipt", "replay", None),
    ("replay receipt", "replay.receipt", ""),
    ("expected failure", "replay.expected_failure", " "),
    ("check digest", "replay.check_sha256", ""),
    ("fail-before", "replay.before", None),
    ("fail-before", "replay.before.status", "PASS"),
    ("fail-before", "replay.before.commit", ""),
    ("fail-before", "replay.before.tree", ""),
    ("pass-after", "replay.after", None),
    ("pass-after", "replay.after.status", "FAIL"),
    ("pass-after", "replay.after.commit", ""),
    ("pass-after", "replay.after.tree", ""),
]


def broken(path, value):
    proof = copy.deepcopy(PROOF)
    *parents, key = path.split(".")
    holder = proof
    for name in parents:
        holder = holder[name]
    holder[key] = value
    return proof


class ProofRecordValidatorTests(unittest.TestCase):
    def test_complete_record_passes(self):
        self.assertIsNone(c.proof_record_gap(PROOF))

    def test_missing_or_blank_part_is_named(self):
        for part, path, value in BROKEN:
            with self.subTest(path=path, value=value):
                self.assertIn(part, c.proof_record_gap(broken(path, value)))

    def test_removed_key_is_named(self):
        for part, path, _ in BROKEN:
            proof = broken(path, None)
            *parents, key = path.split(".")
            holder = proof
            for name in parents:
                holder = holder[name] if holder.get(name) is not None else {}
            holder.pop(key, None)
            with self.subTest(path=path):
                self.assertIn(part, c.proof_record_gap(proof))


class ProofRecordExportTests(unittest.TestCase):
    setUp = test_cli.CliTests.setUp
    run_cli = test_cli.CliTests.run_cli
    event = test_cli.CliTests.event
    public_inputs = test_cli.CliTests.public_inputs
    prepare = test_lesson_delivery.AutoDeliveryTests.prepare

    def proven(self):
        args, inputs, _, _, repo, _ = self.prepare()
        self.run_cli(*args, *inputs, stdin="")
        root, store = c.load_store(self.store)
        lesson, = [row for row in store["lessons"].values() if row["verification"] == "proven"]
        return args, inputs, repo, root, store, lesson

    def export_args(self, inputs, output):
        gate, repo, denylist = inputs[2], inputs[4], inputs[6]
        return argparse.Namespace(lesson=None, format="coldlocker", output=str(output), public=True,
                                  denylist=denylist, git_repo=[], release_check=gate, release_check_repo=repo)

    def test_repository_delivery_rejects_a_latest_proof_without_repository_identity(self):
        args, inputs, repo, root, store, lesson = self.proven()
        receipt = store["receipts"][lesson["receipt"]]
        self.assertTrue(receipt["repo_root"])
        before, after = receipt["before"]["commit"], receipt["after"]["commit"]
        shallow = self.cwd / "shallow"
        subprocess.run(["git", "clone", "--quiet", "--depth", "1", "file://" + str(repo), str(shallow)], check=True)
        subprocess.run(["git", "-C", str(shallow), "fetch", "--quiet", "--depth", "1", "origin", before], check=True)
        replayed = json.loads(self.run_cli("replay", "--lesson", lesson["id"], "--repo", str(shallow),
                                           "--before", before, "--after", after).stdout)
        self.assertEqual(replayed["verification"], "proven")
        root, store = c.load_store(self.store)
        self.assertNotIn("repo_root", store["receipts"][replayed["receipt_id"]])
        self.assertEqual(store["lessons"][replayed["id"]]["scope"], "repo:" + receipt["repo_root"])
        delivery = argparse.Namespace(agentlocker=args[args.index("--agentlocker") + 1], target=str(shallow),
                                      exports=str(self.cwd / "shallow-exports"), denylist=inputs[-1],
                                      release_check=inputs[2], release_check_repo=inputs[4])
        request = dict(replay=dict(id=replayed["id"], status="PASS", verification="proven"))
        delivered, pending = lesson_delivery.deliver(delivery, root, store, [request])
        self.assertEqual(delivered, [])
        self.assertEqual([row["reason"] for row in pending], ["target binding gate incomplete"])
        self.assertFalse((shallow / ".coldlocker" / "rules").exists())

    def test_repository_delivery_rejects_an_unrelated_repository_at_the_proven_path(self):
        args, inputs, repo, root, store, lesson = self.proven()
        self.assertTrue(store["receipts"][lesson["receipt"]]["repo_root"])
        def git(*command):
            return subprocess.run(["git", "-C", str(repo), *command], capture_output=True, text=True, check=True).stdout.strip()
        git("checkout", "--quiet", "--orphan", "unrelated")
        git("commit", "--quiet", "--allow-empty", "-m", "Unrelated history")
        for branch in git("for-each-ref", "--format=%(refname:short)", "refs/heads").split():
            if branch != "unrelated":
                git("branch", "-D", branch)
        git("reflog", "expire", "--expire=now", "--all")
        git("gc", "--quiet", "--prune=now")
        delivery = argparse.Namespace(agentlocker=args[args.index("--agentlocker") + 1], target=str(repo),
                                      exports=str(self.cwd / "unrelated-exports"), denylist=inputs[-1],
                                      release_check=inputs[2], release_check_repo=inputs[4])
        request = dict(replay=dict(id=lesson["id"], status="PASS", verification="proven"))
        delivered, pending = lesson_delivery.deliver(delivery, root, store, [request])
        self.assertEqual(delivered, [])
        self.assertEqual([row["reason"] for row in pending], ["target binding gate incomplete"])

    def test_repository_delivery_binds_by_proven_root_not_path(self):
        args, inputs, repo, root, store, lesson = self.proven()
        clone = self.cwd / "separate-clone"
        subprocess.run(["git", "clone", "--quiet", str(repo), str(clone)], check=True)
        request = dict(replay=dict(id=lesson["id"], status="PASS", verification="proven"))
        def deliver(target, records):
            delivery = argparse.Namespace(agentlocker=args[args.index("--agentlocker") + 1], target=str(target),
                                          exports=str(self.cwd / ("exports-" + target.name)), denylist=inputs[-1],
                                          release_check=inputs[2], release_check_repo=inputs[4])
            return lesson_delivery.deliver(delivery, root, records, [request])
        _, pending = deliver(clone, store)
        self.assertNotIn("target binding gate incomplete", [row["reason"] for row in pending])
        unidentified = copy.deepcopy(store)
        del unidentified["receipts"][lesson["receipt"]]["repo_root"]
        delivered, pending = deliver(repo, unidentified)
        self.assertEqual(delivered, [])
        self.assertEqual([row["reason"] for row in pending], ["target binding gate incomplete"])

    def test_export_and_delivery_fail_closed_when_a_part_is_missing(self):
        self.public_inputs("pass")
        args, inputs, repo, root, store, lesson = self.proven()
        inputs = self.public_inputs("pass")
        cases = [("source failure refs", lambda r, p: r.update(evidence=[])),
                 ("what_went_wrong", lambda r, p: r.update(failure=" ")),
                 ("fix", lambda r, p: r.update(fix="")),
                 ("rule", lambda r, p: r.update(rule="")),
                 ("expected failure", lambda r, p: r.update(expected_failure="")),
                 ("fail-before", lambda r, p: p["before"].update(status="PASS")),
                 ("pass-after", lambda r, p: p["after"].update(status="FAIL"))]
        for part, damage in cases:
            with self.subTest(part=part):
                damaged = copy.deepcopy(store)
                row, receipt = damaged["lessons"][lesson["id"]], damaged["receipts"][lesson["receipt"]]
                damage(row, receipt)
                output = self.cwd / ("out-" + part.replace(" ", "-") + ".md")
                export = self.export_args(inputs, output)
                export.lesson = lesson["id"]
                with self.assertRaises(c.ColdfixError) as raised:
                    c.export_lessons(export, root, damaged)
                self.assertFalse(output.exists())
                exports = self.cwd / ("exports-" + part.replace(" ", "-"))
                delivery = argparse.Namespace(agentlocker=args[args.index("--agentlocker") + 1], target=str(repo),
                                              exports=str(exports), denylist=inputs[-1], release_check=inputs[2],
                                              release_check_repo=inputs[4])
                request = dict(replay=dict(id=lesson["id"], status="PASS", verification="proven"))
                delivered, pending = lesson_delivery.deliver(delivery, root, damaged, [request])
                self.assertEqual(delivered, [])
                self.assertEqual(len(pending), 1)
                self.assertFalse(list(exports.glob("*.md")) if exports.exists() else [])
                self.assertFalse((repo / ".coldlocker").exists())
        export = self.export_args(inputs, self.cwd / "good.md")
        export.lesson = lesson["id"]
        self.assertEqual(c.export_lessons(export, root, store), dict(exported=True, lesson_count=1))
        payload = json.loads((self.cwd / "good.md").read_text().split("```json\n", 1)[1].split("\n```", 1)[0])
        self.assertIsNone(c.proof_record_gap(payload))
