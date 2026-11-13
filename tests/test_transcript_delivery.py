import io
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import coldfix as c
import transcript_delivery as d

L1 = """---
id: L1
slug: docs-sync
title: Keep docs in sync
scope: global
level: doc
status: active
repos: a, b
source: owner-request
briefing: Docs change with the code.
verification: proven
---
# L1 Keep docs in sync

Docs change with the code.

## Reproduce
x

## Fix
y

## Verify
z
"""
BATCH = "2026-11-06T22:50:14.555618+00:00"
L1_LINE = "- **L1 docs-sync** (replayed): Docs change with the code."
STALE_BRIEFING = "Details: `lessons/<id>-<slug>.md`.\nalways on: L1\n"


FAKE_TOOL = r'''
import os, re, sys
from pathlib import Path
folder = Path(os.environ["FLEET_LESSONS_DIR"])
lessons = {p.name: p.read_text(encoding="utf-8") for p in sorted(folder.glob("L*-*.md"))}
meta = {n: dict(re.findall(r"^(\w+): ?(.*)$", t.split("\n---\n")[0], re.M)) for n, t in lessons.items()}
if sys.argv[1] == "build":
    (folder / "INDEX.md").write_text("".join(f"| [{m['id']}]({n}) | {m.get('slug')} | {m.get('scope')} |\n" for n, m in meta.items()))
    (folder / "briefing.md").write_text(f"Details: `{folder}/<id>-<slug>.md`.\nalways on: " + ", ".join(
        m["id"] for m in meta.values() if m.get("scope") == "global" and m.get("status") == "active") + "\n")
    sys.exit(0)
bad = [n for n, t in lessons.items() if not re.search(r"^## Verify\n\S", t, re.M)]
print("\n".join(n + ": empty section Verify" for n in bad))
sys.exit(1 if bad else 0)
'''


def lead(title, rule, replayed="I will check the requested device first.", fixed="m3", **proof):
    return dict(status="proven", batch=BATCH, reason="",
                source_ref="transcript:/x/Codex/2026/09/2026-09-30_codex-29368abb.md#messages=m1,m2,m3",
                lesson=dict(title=title, rule=rule, what_went_wrong="The agent used the wrong path.",
                            how_to_check="Compare the action with the request.", source="owner",
                            wrong_message="m2", fixed_message=fixed, duplicate_of="", id="x", verdict="lesson",
                            kind="conduct"),
                proof=dict(dict(before=0.63, replay=0.09, fixed=0.2, controls=20, control_failures=0, general=0.85,
                                replay_applies=0.9, fixed_applies=0.9, replayed=replayed), **proof))


def finding_lead(title, rule, finding_class="design"):
    return dict(status="needs_approval", batch=BATCH, reason=finding_class + " finding needs owner approval",
                source_ref="transcript:/x/Codex/2026/09/2026-09-30_codex-29368abb.md#messages=m1,m2,m3",
                lesson=dict(title=title, rule=rule, what_went_wrong="The agent left the retry loop unbounded.",
                            how_to_check="Read the loop for a stop condition.", source="reviewer",
                            wrong_message="m2", fixed_message="", duplicate_of="", id="x", verdict="lesson",
                            kind="code", finding_class=finding_class),
                proof=dict(excerpt="I wrapped the call in while True and retry on every error."))


class Fixture:
    def __init__(self, directory):
        base = Path(directory)
        self.folder = base / "coldfix-lessons"
        self.folder.mkdir()
        (self.folder / "L1-docs-sync.md").write_text(L1)
        (self.folder / "briefing.md").write_text(STALE_BRIEFING)
        self.tool = base / "lessons.py"
        self.tool.write_text(FAKE_TOOL)
        self.rules = base / "rules"
        self.rules.mkdir()
        self.root = base / "store"
        self.root.mkdir()
        self.asked, self.replies, self.fail_ask, self.omit = [], {}, False, set()

    def ledger(self, leads):
        (self.root / d.LEDGER).write_text(json.dumps(dict(
            leads=leads, batches=[dict(started=BATCH, model="gpt-6-luna")])))

    def ask(self, model, prompt, shape, work):
        self.asked.append(prompt)
        if self.fail_ask:
            raise subprocess.CalledProcessError(1, "codex")
        new = json.loads(prompt[prompt.index("{"):])["new_lessons"]
        return dict(lessons=[{"id": row["id"], "summary": "The agent took the wrong path and was corrected.",
                              "scope": "agent", "duplicate_of": "", **self.replies.get(row["id"], {})} for row in new if row["id"] not in self.omit])

    def deliver(self, model="gpt-6-luna"):
        with mock.patch.object(d.learn_daily, "cheap_models", return_value={"gpt-6-luna", "claude-haiku-5-5"}), \
                mock.patch.object(d, "RULES", str(self.rules)):
            return d.deliver(self.root, self.folder, ask=self.ask, tool=str(self.tool),
                             route=lambda role: dict(provider="codex", model=model, effort="default"))

    def files(self):
        return {"lessons/" + p.name for p in self.folder.iterdir()}

    def briefing(self, *lines):
        return ("<!-- generated by Coldfix transcript_delivery.py from active replayed and owner-approved judged lessons; edit the lesson files, not this -->\n"
                "## Lessons (always on)\n\nMistakes agents repeated across 2+ repos. Each line names its evidence: replayed "
                f"means a replay showed the fix works. A judged line is a model-judged transcript lesson the owner approved; it reaches only sessions in the repos the owner named. An approved line is a design, security or maintainability finding no replay can show; the owner approved it from its evidence for the same repos. Details and how to reproduce, fix and verify each: `{self.folder}/<id>-<slug>.md`.\n\n"
                + "".join(line + "\n" for line in lines))

    def file(self, name):
        return (self.folder / name).read_text(encoding="utf-8")


class TranscriptDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.f = Fixture(self.directory.name)

    def test_delivery_requires_a_configured_folder_and_lessons_tool(self):
        with self.assertRaises(d.c.ColdfixError):
            d.deliver(self.f.root, self.f.folder / "missing", tool=str(self.f.tool))
        with mock.patch.object(d, "TOOL", ""), self.assertRaises(d.c.ColdfixError):
            d.deliver(self.f.root, self.f.folder)

    def test_delivers_proven_lessons_with_ledger_numbers_and_skips_duplicates(self):
        self.f.ledger(dict(
            a=lead("Use the requested connection path", "Check the target matches the request."),
            b=lead("Keep docs current", "Docs change with the code."),
            c=dict(lead("Rejected", "r"), status="rejected"),
            e=dict(status="duplicate", reason="transcript:a", batch=BATCH, source_ref="transcript:/x/y.md#messages=q"),
        ))
        self.f.replies = dict(b=dict(duplicate_of="L1"))
        result = self.f.deliver()
        self.assertEqual((result["added"], result["duplicates"]), (["L2"], ["b"]))
        self.assertEqual(self.f.files(), {"lessons/INDEX.md", "lessons/L1-docs-sync.md",
                                               "lessons/L2-use-the-requested-connection-path.md", "lessons/briefing.md"})
        body = self.f.file("L2-use-the-requested-connection-path.md")
        for expected in ("scope: transcript", "source: coldfix-transcript:a", "## How this was judged", "from a judged transcript lesson.",
                         "The agent took the wrong path and was corrected.",
                         "transcript lead `a`, conversation `Codex/2026-09-30_codex-29368abb`, mistaken message `m2`",
                         "Correction: the later agent message `m3` did it right (Jev breaks_rule 0.2)",
                         "breaks_rule on the real mistaken message: 0.63",
                         "Jev breaks_rule on the replay: 0.09. Replay: \"I will check the requested device first.\"",
                         "0 of 20 clean messages flagged", "general_lesson 0.85", "leads folded into this one: `e`",
                         "gpt-6-luna named no duplicate among L1", "Judged 2026-11-06",
                         "Jev jev-latest", "veto L2"):
            self.assertIn(expected, body)
        index = self.f.file("INDEX.md")
        self.assertIn("| [L2](L2-use-the-requested-connection-path.md) | use-the-requested-connection-path | transcript |",
                      index)
        self.assertEqual(self.f.file("briefing.md"), self.f.briefing(L1_LINE))
        self.assertEqual(json.loads((self.f.root / d.DELIVERIES).read_text())["leads"]["b"]["duplicate_of"], "L1")
        self.assertIn("- lead `b` duplicates L1", d.week(self.f.root))
        again = self.f.deliver()
        self.assertEqual((again["added"], len(self.f.asked)), ([], 2))

    def test_explain_prints_a_delivered_transcript_lessons_evidence_receipt_scope_and_history(self):
        self.f.ledger(dict(a=lead("Use the requested connection path", "Check the target matches the request."),
                           e=dict(status="duplicate", reason="transcript:a", batch=BATCH, source_ref="transcript:/x/y.md#messages=q"),
                           g=dict(status="duplicate", reason="coldlocker:lessons/L2-use-the-requested-connection-path",
                                  batch=BATCH, source_ref="transcript:/x/g.md#messages=q"),
                           h=dict(status="duplicate", reason="", batch=BATCH, source_ref="transcript:/x/h.md#messages=q")))
        self.assertEqual(self.f.deliver()["added"], ["L2"])
        deliveries = json.loads((self.f.root / d.DELIVERIES).read_text())
        deliveries["leads"]["h"] = dict(status="duplicate", duplicate_of="L2", reason="duplicate: L2", checked=BATCH)
        (self.f.root / d.DELIVERIES).write_text(json.dumps(deliveries))
        d.veto(self.f.root, "L2")

        def explain(name, code=0):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(c.main(["--store", str(self.f.root), "explain", name]), code)
            return out.getvalue() and json.loads(out.getvalue())
        for name in ("L2", "transcript:a", "a"):
            shown = explain(name)
            self.assertEqual((shown["lesson"]["id"], shown["lesson"]["status"], shown["lesson"]["delivered_as"], shown["lesson"]["delivery"],
                              shown["scope"]), ("transcript:a", "proven", "L2", "vetoed", "transcript"))
            self.assertEqual(shown["evidence"], [
                dict(id="transcript:a", source_ref="transcript:/x/Codex/2026/09/2026-09-30_codex-29368abb.md#messages=m1,m2,m3"),
                dict(id="transcript:e", source_ref="transcript:/x/y.md#messages=q"),
                dict(id="transcript:g", source_ref="transcript:/x/g.md#messages=q"),
                dict(id="transcript:h", source_ref="transcript:/x/h.md#messages=q")])
            receipt, = shown["receipts"]
            self.assertEqual((receipt["before"], receipt["replay"], receipt["controls"], receipt["general"]), (0.63, 0.09, 20, 0.85))
            self.assertNotIn("replayed", receipt)
            self.assertEqual([(row["event"], row.get("lesson"), row.get("reason")) for row in shown["history"]],
                             [("judged", None, None), ("delivered", "L2", ""), ("vetoed", "L2", "vetoed by the owner")])
            self.assertEqual(shown["history"][0]["at"], BATCH)
        explain("L9", code=2)

    def test_rejects_l34_content_fact_and_l19_restatement_but_delivers_new_behavior(self):
        (self.f.rules / "delegation.md").write_text("# Delegation\nGive each agent one bounded task.\n")
        self.f.ledger(dict(
            a=lead("Do not claim Paul observed Jesus earthly ministry",
                   "Do not present a biblical figure as having personally witnessed events unless the text supports that claim. "
                   "If a user identifies a factual error, correct the claim directly in the revised material."),
            b=lead("Assign one task to each agent", "Assign one bounded task to each agent."),
            c=lead("Check receipt ownership", "Verify that a receipt belongs to the current worktree before reuse.")))
        self.f.replies = dict(a=dict(scope="content"), b=dict(duplicate_of="coldlocker:rules/delegation"))
        result = self.f.deliver()
        self.assertEqual(result["added"], ["L2"])
        saved = json.loads((self.f.root / d.DELIVERIES).read_text())["leads"]
        self.assertEqual(saved["a"]["reason"], "off-topic: domain/content fact")
        self.assertEqual(saved["b"]["reason"], "duplicate: coldlocker:rules/delegation")
        self.assertIn("Give each agent one bounded task.", self.f.asked[0])
        self.assertIn("agent behavior", self.f.asked[0])
        self.assertIn("L2-check-receipt-ownership.md", saved["c"]["file"])

    def test_existing_deliveries_are_rechecked_and_removed_by_normal_delivery(self):
        self.f.ledger(dict(a=lead("Content", "Paul encountered the risen Jesus."),
                           b=lead("Duplicate", "Docs change with the code."),
                           c=lead("New behavior", "Check receipt ownership.")))
        self.f.deliver()
        path = self.f.root / d.DELIVERIES
        deliveries = json.loads(path.read_text())
        for entry in deliveries["leads"].values():
            entry.pop("gate", None)
        path.write_text(json.dumps(deliveries))
        self.f.replies = dict(a=dict(scope="content"), b=dict(duplicate_of="L1"))
        result = self.f.deliver()
        self.assertEqual(result["removed"], ["L2", "L3"])
        self.assertEqual(result["added"], [])
        self.assertEqual(self.f.files() & {"lessons/L2-content.md", "lessons/L3-duplicate.md"}, set())
        self.assertIn("lessons/L4-new-behavior.md", self.f.files())
        self.assertEqual(self.f.deliver()["removed"], [])

    def test_judged_lessons_carry_the_judge_that_proved_them(self):
        stamp = dict(model="jev-test", threshold=0.7, prompt_sha256="ab" * 32)
        self.f.ledger(dict(a=lead("First", "Check receipt ownership.", judge=stamp),
                           b=lead("Second", "Verify the connection.", control_failures=1)))
        self.assertEqual(self.f.deliver()["added"], ["L2", "L3"])
        stamped, unstamped = self.f.file("L2-first.md"), self.f.file("L3-second.md")
        head = stamped.split("\n---\n")[0]
        self.assertTrue(head.endswith("verification: judged\njudge_model: jev-test\njudge_threshold: 0.7\n"
                                      "judge_prompt_sha256: " + "ab" * 32 + "\njudge_controls: 20\njudge_control_failures: 0"))
        self.assertIn("Jev jev-test (breaks_rule, general_lesson)", stamped)
        original = d.UNSTAMPED_JUDGE
        for line in ("verification: judged", f"judge_model: {original['model']}", f"judge_threshold: {original['threshold']}",
                     f"judge_prompt_sha256: {original['prompt_sha256']}", "judge_controls: 20", "judge_control_failures: 1"):
            self.assertIn("\n" + line + "\n", unstamped.split("\n---\n")[0] + "\n")
        self.assertNotIn("proven (Jev", stamped + unstamped)

    def test_a_lesson_delivered_as_proven_is_relabeled_judged_by_the_next_delivery(self):
        self.f.ledger(dict(a=lead("First", "Check receipt ownership.")))
        self.f.deliver()
        path = self.f.folder / "L2-first.md"
        head, _, body = path.read_text().partition("\n---\n")
        body = body.replace("Check receipt ownership.", "Check receipt ownership first.")
        old = re.sub(r"^verification: judged(\n.*)*", "verification: proven (Jev transcript replay, lead a)", head, flags=re.M)
        path.write_text(old + "\n---\n" + body)
        result = self.f.deliver()
        self.assertEqual((result["added"], result["removed"], result.get("error")), ([], [], None))
        relabeled = path.read_text()
        self.assertEqual(relabeled.partition("\n---\n")[2], body)
        self.assertIn("\nverification: judged\njudge_model: ", relabeled)
        self.assertNotIn("verification: proven", relabeled)
        asked = len(self.f.asked)
        stamp = path.stat().st_mtime_ns
        self.assertEqual(self.f.deliver(), dict(added=[], removed=[], duplicates=[]))
        self.assertEqual((len(self.f.asked), path.stat().st_mtime_ns), (asked, stamp))

    def test_delivery_gate_fails_closed_and_retries_invalid_classification(self):
        self.f.ledger(dict(a=lead("New", "Check receipt ownership.")))
        for reply in (dict(scope=""), dict(duplicate_of="unknown")):
            self.f.replies = dict(a=reply)
            result = self.f.deliver()
            self.assertEqual(result["added"], [])
            self.assertIn("invalid", result["error"])
        self.f.replies = {}
        self.assertEqual(self.f.deliver()["added"], ["L2"])

    def test_same_batch_compares_with_accepted_lessons_and_never_itself(self):
        self.f.ledger(dict(a=lead("First", "Check receipt ownership."),
                           b=lead("Restatement", "Validate the worktree that owns the receipt.")))
        self.f.replies = dict(b=dict(duplicate_of="L2"))
        result = self.f.deliver()
        self.assertEqual((result["added"], result["duplicates"]), (["L2"], ["b"]))
        known = json.loads(self.f.asked[1][self.f.asked[1].index("{"):])["known_lessons"]
        self.assertIn(self.f.file("L2-first.md"), [r["rule"] for r in known])

    def test_incomplete_existing_check_blocks_later_duplicates_until_retry(self):
        self.f.ledger(dict(a=lead("First", "Check receipt ownership."),
                           b=lead("Second", "Check receipt ownership.")))
        self.f.deliver()
        path = self.f.root / d.DELIVERIES
        saved = json.loads(path.read_text())
        for entry in saved["leads"].values():
            entry.pop("gate", None)
        path.write_text(json.dumps(saved))
        self.f.omit = {"a"}
        self.f.deliver()
        saved = json.loads(path.read_text())
        self.assertNotIn("gate", saved["leads"]["b"])
        self.f.omit = set()
        self.f.replies = dict(b=dict(duplicate_of="L2"))
        self.assertEqual(self.f.deliver()["removed"], ["L3"])

    def test_changed_delivered_content_is_classified_again(self):
        self.f.ledger(dict(a=lead("First", "Check receipt ownership.")))
        self.f.deliver()
        path = self.f.folder / "L2-first.md"
        path.write_text(path.read_text().replace("Check receipt ownership.", "Paul encountered the risen Jesus."))
        self.f.replies = dict(a=dict(scope="content"))
        self.assertEqual(self.f.deliver()["removed"], ["L2"])
        candidate = json.loads(self.f.asked[-1][self.f.asked[-1].index("{"):])["new_lessons"][0]
        self.assertIn("Paul encountered the risen Jesus.", candidate["rule"])

    def test_explicit_veto_is_removed_even_if_an_earlier_audit_is_incomplete(self):
        self.f.ledger(dict(a=lead("First", "Check receipt ownership."),
                           b=lead("Second", "Check a connection.")))
        self.f.deliver()
        path = self.f.root / d.DELIVERIES
        saved = json.loads(path.read_text())
        saved["leads"]["a"].pop("gate", None)
        path.write_text(json.dumps(saved))
        d.veto(self.f.root, "L3")
        self.f.omit = {"a"}
        result = self.f.deliver()
        self.assertEqual(result.get("error"), "summary missing for lead a")
        self.assertEqual(result["removed"], ["L3"])
        self.assertNotIn("lessons/L3-second.md", self.f.files())

    def test_a_changed_later_lesson_is_compared_again_with_earlier_ones(self):
        self.f.ledger(dict(a=lead("First", "Check receipt ownership."),
                           b=lead("Second", "Verify the connection.")))
        self.f.deliver()
        path = self.f.folder / "L3-second.md"
        path.write_text(path.read_text().replace("Verify the connection.", "Check receipt ownership."))
        self.f.replies = dict(b=dict(duplicate_of="L2"))
        self.assertEqual(self.f.deliver()["removed"], ["L3"])
        self.assertIn("lessons/L2-first.md", self.f.files())

    def test_a_passed_lesson_stays_when_the_gate_reruns_with_a_changed_receipt(self):
        self.f.ledger(dict(a=lead("First", "Check receipt ownership."),
                           b=lead("Second", "Verify the connection.")))
        self.f.deliver()
        calls = len(self.f.asked)
        earlier = self.f.folder / "L2-first.md"
        earlier.write_text(earlier.read_text().replace("Check receipt ownership.", "Verify the connection."))
        (self.f.rules / "shared-policy.md").write_text("Verify claims with direct evidence.\n")
        later = self.f.folder / "L3-second.md"
        later.write_text(later.read_text().replace("\nlevel: doc\n", "\nlevel: conduct\n"))
        self.f.replies = dict(a=dict(duplicate_of="coldlocker:rules/shared-policy"),
                              b=dict(duplicate_of="L2"))
        result = self.f.deliver()
        self.assertEqual((result["removed"], result.get("error")), (["L2"], None))
        self.assertEqual(len(self.f.asked), calls + 1)
        self.assertIn("lessons/L3-second.md", self.f.files())
        self.assertEqual(self.f.deliver(), dict(added=[], removed=[], duplicates=[]))
        self.assertEqual(len(self.f.asked), calls + 1)

    def test_lessons_passed_under_the_old_corpus_receipt_are_not_judged_again(self):
        self.f.ledger(dict(a=lead("First", "Check receipt ownership.")))
        self.f.deliver()
        path = self.f.root / d.DELIVERIES
        saved = json.loads(path.read_text())
        with mock.patch.object(d, "RULES", str(self.f.rules)):
            known = d.known_lessons(self.f.folder, skip=("L2-first.md",))
        legacy = d.hashlib.sha256(d.c.encoded(dict(content=self.f.file("L2-first.md"), known=known)).encode()).hexdigest()
        saved["leads"]["a"]["gate"] = dict(version=d.SCOPE_VERSION, sha256=legacy)
        path.write_text(json.dumps(saved))
        calls = len(self.f.asked)
        self.f.replies = dict(a=dict(duplicate_of="L1"))
        self.assertEqual(self.f.deliver(), dict(added=[], removed=[], duplicates=[]))
        self.assertEqual(len(self.f.asked), calls)
        gate = json.loads(path.read_text())["leads"]["a"]["gate"]
        self.assertEqual(gate, d.receipt(self.f.file("L2-first.md")))

    def test_old_corpus_receipts_survive_the_runs_own_relabel(self):
        self.f.ledger(dict(a=lead("First", "Check receipt ownership."), b=lead("Second", "Verify the connection.")))
        self.f.deliver()
        path = self.f.root / d.DELIVERIES
        saved = json.loads(path.read_text())
        with mock.patch.object(d, "RULES", str(self.f.rules)):
            known = d.known_lessons(self.f.folder, skip=("L2-first.md", "L3-second.md"))
        for key, name, title in (("a", "L2-first.md", "First"), ("b", "L3-second.md", "Second")):
            lesson = self.f.folder / name
            head, _, body = lesson.read_text().partition("\n---\n")
            old = re.sub(r"^verification: judged(\n.*)*", "verification: proven (Jev transcript replay)", head, flags=re.M)
            lesson.write_text(old + "\n---\n" + body)
            content = lesson.read_text()
            legacy = d.hashlib.sha256(d.c.encoded(dict(content=content, known=known)).encode()).hexdigest()
            saved["leads"][key]["gate"] = dict(version=d.SCOPE_VERSION, sha256=legacy)
            known = known + [dict(id=saved["leads"][key]["lesson"], title=title, rule=content)]
        path.write_text(json.dumps(saved))
        calls = len(self.f.asked)
        self.f.replies = dict(a=dict(duplicate_of="L1"), b=dict(duplicate_of="L2"))
        self.assertEqual(self.f.deliver(), dict(added=[], removed=[], duplicates=[]))
        self.assertEqual(len(self.f.asked), calls)
        self.assertIn("\nverification: judged\n", self.f.file("L3-second.md"))

    def test_a_rule_edited_since_its_old_corpus_receipt_is_checked_again(self):
        self.f.ledger(dict(a=lead("First", "Check receipt ownership.")))
        self.f.deliver()
        path = self.f.root / d.DELIVERIES
        saved = json.loads(path.read_text())
        saved["leads"]["a"]["gate"] = dict(version=d.SCOPE_VERSION, sha256="0" * 64)
        path.write_text(json.dumps(saved))
        lesson = self.f.folder / "L2-first.md"
        lesson.write_text(lesson.read_text().replace("Check receipt ownership.", "Docs change with the code."))
        self.f.replies = dict(a=dict(duplicate_of="L1"))
        self.assertEqual(self.f.deliver()["removed"], ["L2"])

    def test_scope_check_receives_the_current_rule_without_proof_narrative(self):
        self.f.ledger(dict(a=lead("Verify tracking", "Verify event data before diagnosing a tracking failure.")))
        self.f.deliver()
        path = self.f.root / d.DELIVERIES
        saved = json.loads(path.read_text())
        saved["leads"]["a"].pop("gate", None)
        path.write_text(json.dumps(saved))
        self.f.deliver()
        candidate = json.loads(self.f.asked[-1][self.f.asked[-1].index("{"):])["new_lessons"][0]
        self.assertEqual(candidate["rule"], "Verify event data before diagnosing a tracking failure.")

    def test_old_automated_scope_rejections_are_rechecked_and_restored_with_their_ids(self):
        self.f.ledger(dict(a=lead("Repair uncertainty", "Ask for evidence instead of guessing when advice risks injury."),
                           b=lead("Verify tracking", "Verify event data before diagnosing a tracking failure."),
                           c=lead("Paul", "Paul encountered the risen Jesus."),
                           d=lead("Manual veto", "Check a connection.")))
        self.f.deliver()
        path = self.f.root / d.DELIVERIES
        saved = json.loads(path.read_text())
        for entry in saved["leads"].values():
            entry.pop("gate", None)
        path.write_text(json.dumps(saved))
        self.f.replies = {k: dict(scope="content") for k in ("a", "b", "c")}
        self.assertEqual(self.f.deliver()["removed"], ["L2", "L3", "L4"])
        saved = json.loads(path.read_text())
        for entry in saved["leads"].values():
            entry.pop("scope_version", None)
        path.write_text(json.dumps(saved))
        d.veto(self.f.root, "L5")
        self.f.replies = dict(c=dict(scope="content"))
        result = self.f.deliver()
        self.assertEqual(result["added"], ["L2", "L3"])
        self.assertEqual(result["removed"], ["L5"])
        self.assertIn("lessons/L2-repair-uncertainty.md", self.f.files())
        self.assertIn("lessons/L3-verify-tracking.md", self.f.files())
        self.assertNotIn("lessons/L4-paul.md", self.f.files())
        saved = json.loads(path.read_text())["leads"]
        self.assertEqual(saved["a"]["previous"]["reason"], "off-topic: domain/content fact")
        calls = len(self.f.asked)
        self.assertEqual(self.f.deliver()["added"], [])
        self.assertEqual(len(self.f.asked), calls)

    def test_reconsidered_scope_rejection_still_has_to_pass_duplicate_check(self):
        self.f.ledger(dict(a=lead("Verify tracking", "Check the event data.")))
        self.f.deliver()
        path = self.f.root / d.DELIVERIES
        saved = json.loads(path.read_text())
        saved["leads"]["a"].pop("gate", None)
        path.write_text(json.dumps(saved))
        self.f.replies = dict(a=dict(scope="content"))
        self.f.deliver()
        saved = json.loads(path.read_text())
        saved["leads"]["a"].pop("scope_version", None)
        path.write_text(json.dumps(saved))
        self.f.replies = dict(a=dict(scope="agent", duplicate_of="L1"))
        result = self.f.deliver()
        self.assertEqual(result["added"], [])
        saved = json.loads(path.read_text())["leads"]["a"]
        self.assertEqual(saved["reason"], "duplicate: L1")
        self.assertNotIn("lessons/L2-verify-tracking.md", self.f.files())

    def test_restoration_recovers_edited_rule_and_proof_from_the_removed_file(self):
        self.f.ledger(dict(a=lead("First", "Check receipt ownership.")))
        self.f.deliver()
        path = self.f.folder / "L2-first.md"
        edited = path.read_text().replace("Check receipt ownership.", "State uncertainty before giving risky advice.")
        edited += "\nAdditional verification evidence retained.\n"
        original_bytes = edited.encode("utf-8").replace(b"\n", b"\r\n")
        path.write_bytes(original_bytes)
        self.f.replies = dict(a=dict(scope="content"))
        self.assertEqual(self.f.deliver()["removed"], ["L2"])
        ledger = self.f.root / d.DELIVERIES
        saved = json.loads(ledger.read_text())
        saved["leads"]["a"].pop("scope_version", None)
        ledger.write_text(json.dumps(saved))
        self.f.replies = {}
        self.assertEqual(self.f.deliver()["added"], ["L2"])
        self.assertEqual(self.f.file("L2-first.md"), edited)
        self.assertEqual((self.f.folder / "L2-first.md").read_bytes(), original_bytes)
        candidate = json.loads(self.f.asked[-1][self.f.asked[-1].index("{"):])["new_lessons"][0]
        self.assertEqual(candidate["rule"], "State uncertainty before giving risky advice.")

    def test_a_restored_lesson_with_old_proven_wording_is_published_judged(self):
        self.f.ledger(dict(a=lead("First", "Check receipt ownership.")))
        self.f.deliver()
        self.f.replies = dict(a=dict(scope="content"))
        (self.f.folder / "L2-first.md").write_text(self.f.file("L2-first.md").replace(
            "Check receipt ownership.", "Check receipt ownership before editing."))
        self.assertEqual(self.f.deliver()["removed"], ["L2"])
        removed = self.f.root / d.REMOVED / "L2-first.md"
        legacy = removed.read_text()
        for old, new in d.JUDGED_WORDING:
            legacy = legacy.replace(new, old)
        self.assertIn("from a proven transcript lesson.", legacy)
        removed.write_text(legacy)
        ledger = self.f.root / d.DELIVERIES
        saved = json.loads(ledger.read_text())
        saved["leads"]["a"].pop("scope_version", None)
        ledger.write_text(json.dumps(saved))
        self.f.replies = {}
        self.assertEqual(self.f.deliver()["added"], ["L2"])
        self.assertIn("from a judged transcript lesson.", self.f.file("L2-first.md"))
        self.assertNotRegex(self.f.file("L2-first.md"), r"(?i)\bproven\b")

    def test_reconsidered_undelivered_duplicate_is_reported_in_the_weekly_summary(self):
        self.f.ledger(dict(a=lead("Check docs", "Docs change with the code.")))
        self.f.replies = dict(a=dict(scope="content"))
        self.assertEqual(self.f.deliver()["added"], [])
        path = self.f.root / d.DELIVERIES
        saved = json.loads(path.read_text())
        saved["leads"]["a"].pop("scope_version", None)
        path.write_text(json.dumps(saved))
        self.f.replies = dict(a=dict(scope="agent", duplicate_of="L1"))
        self.f.deliver()
        entry = json.loads(path.read_text())["leads"]["a"]
        self.assertEqual(entry["status"], "duplicate")
        self.assertEqual(entry["duplicate_of"], "L1")
        self.assertIn("- lead `a` duplicates L1", d.week(self.f.root))

    def test_scope_reconsideration_keeps_a_combined_content_and_duplicate_rejection(self):
        self.f.ledger(dict(a=lead("Verify tracking", "Inspect event data before diagnosing a tracking failure."),
                           b=lead("Keep docs current", "Docs change with the code.")))
        self.f.replies = dict(a=dict(scope="content"), b=dict(scope="content", duplicate_of="L1"))
        self.assertEqual(self.f.deliver()["added"], [])
        path = self.f.root / d.DELIVERIES
        saved = json.loads(path.read_text())
        for entry in saved["leads"].values():
            entry.pop("scope_version", None)
        path.write_text(json.dumps(saved))
        self.f.replies = {}
        self.assertEqual(self.f.deliver()["added"], ["L2"])
        entry = json.loads(path.read_text())["leads"]["b"]
        self.assertEqual(entry["status"], "duplicate")
        self.assertEqual(entry["duplicate_of"], "L1")
        self.assertEqual(len(self.f.asked), 3)

    def test_scope_recheck_tolerates_edited_heading_and_provenance(self):
        self.f.ledger(dict(a=lead("First", "Check receipt ownership.")))
        self.f.deliver()
        path = self.f.folder / "L2-first.md"
        changed = path.read_text().replace("# L2 First", "# Edited lesson heading")
        changed = changed.replace("Delivered automatically by Coldfix", "Delivery attribution updated by owner")
        path.write_text(changed)
        result = self.f.deliver()
        self.assertIsNone(result.get("error"))
        candidate = json.loads(self.f.asked[-1][self.f.asked[-1].index("{"):])["new_lessons"][0]
        self.assertEqual(candidate["rule"], "Check receipt ownership.")

    def test_a_lesson_proven_before_the_rule_application_check_is_not_delivered(self):
        old = lead("Old proof", "Rule.")
        del old["proof"]["replay_applies"]
        self.f.ledger(dict(a=old))
        self.assertEqual((self.f.deliver()["added"], "lessons/L2-first.md" in self.f.files()), ([], False))

    def test_veto_removes_the_lesson_and_never_re_adds_it(self):
        self.f.ledger(dict(a=lead("First", "Rule one."), b=lead("Second", "Rule two.")))
        self.assertEqual(self.f.deliver()["added"], ["L2", "L3"])
        d.veto(self.f.root, "L2")
        self.assertEqual(self.f.deliver()["removed"], ["L2"])
        self.assertNotIn("lessons/L2-first.md", self.f.files())
        self.assertNotIn("L2-first", self.f.file("INDEX.md"))
        (self.f.folder / "L3-second.md").unlink()
        self.assertEqual(self.f.deliver()["added"], [])
        self.f.ledger(dict(a=lead("First", "Rule one."), b=lead("Second", "Rule two."), c=lead("Third", "Rule three.")))
        self.assertEqual(self.f.deliver()["added"], ["L4"])
        self.assertEqual(self.f.files() & {"lessons/L2-first.md", "lessons/L3-second.md"}, set())
        week = d.week(self.f.root)
        self.assertIn("- L4 Third (`L4-third.md`). Veto: run `scripts/learning-service veto L4` on the Coldfix host", week)
        self.assertIn("- L2 First (vetoed by the owner)", week)
        self.assertIn("- L3 Second (removed from the lessons folder)", week)
        with self.assertRaises(d.c.ColdfixError):
            d.veto(self.f.root, "L99")

    def test_private_replay_and_summary_text_are_withheld(self):
        self.f.ledger(dict(a=lead("First", "Rule one.", replayed="Connecting to 192.168.1.175 now.", fixed="")))
        self.f.replies = dict(a=dict(summary="It reached 192.168.1.175."))
        self.f.deliver()
        body = self.f.file("L2-first.md")
        self.assertNotIn("192.168", body)
        self.assertIn("(replay text withheld: it contains private text)", body)
        self.assertIn("(summary withheld: it contained private text)", body)
        self.assertIn("no later agent message that does it right", body)

    def test_refuses_pricier_summary_models(self):
        self.f.ledger(dict(a=lead("First", "Rule one.")))
        self.assertIn("Haiku or Luna", self.f.deliver(model="claude-opus-5-5")["error"])
        self.assertEqual((self.f.files(), self.f.asked), ({"lessons/L1-docs-sync.md", "lessons/briefing.md"}, []))

    def test_veto_is_removed_even_when_the_summary_call_fails(self):
        self.f.ledger(dict(a=lead("First", "Rule one.")))
        self.f.deliver()
        d.veto(self.f.root, "L2")
        self.f.ledger(dict(a=lead("First", "Rule one."), b=lead("Second", "Rule two.")))
        self.f.fail_ask = True
        result = self.f.deliver()
        self.assertEqual((result["removed"], result["added"], result["error"]), (["L2"], [], "summary failed: CalledProcessError"))
        self.assertNotIn("lessons/L2-first.md", self.f.files())
        self.f.fail_ask = False
        self.assertEqual(self.f.deliver()["added"], ["L3"])

    def test_a_lead_the_summary_omits_is_reported_and_delivered_later(self):
        self.f.ledger(dict(a=lead("First", "Rule one."), b=lead("Second", "Rule two.")))
        self.f.omit = {"a"}
        result = self.f.deliver()
        self.assertEqual((result["added"], result["error"]), ([], "summary missing for lead a"))
        self.assertIn('"clean_messages_flagged": 0', self.f.asked[0])
        self.f.omit = set()
        result = self.f.deliver()
        self.assertEqual((result["added"], result.get("error")), (["L2", "L3"], None))

    def test_a_veto_is_reported_removed_only_after_its_removal_is_written(self):
        self.f.ledger(dict(a=lead("First", "Rule one.")))
        self.f.deliver()
        d.veto(self.f.root, "L2")
        (self.f.folder / "L9-broken.md").write_text("---\nid: L9\n---\n# L9 Broken\n")
        with self.assertRaises(d.c.ColdfixError):
            self.f.deliver()
        self.assertIn("lessons/L2-first.md", self.f.files())
        week = d.week(self.f.root)
        self.assertNotIn("Removed this week", week)
        self.assertIn("Vetoed, removal not written yet (retried every hour):\n- L2 First", week)
        (self.f.folder / "L9-broken.md").unlink()
        self.assertEqual(self.f.deliver()["removed"], ["L2"])
        week = d.week(self.f.root)
        self.assertIn("Removed this week:\n- L2 First (vetoed by the owner)", week)
        self.assertNotIn("removal not written", week)

    def test_lost_delivery_records_are_rebuilt_from_the_folder(self):
        self.f.ledger(dict(a=lead("First", "Rule one.")))
        self.f.deliver()
        (self.f.root / d.DELIVERIES).unlink()
        self.assertEqual(self.f.deliver()["added"], [])
        self.assertIn("- L2 First (`L2-first.md`)", d.week(self.f.root))
        d.veto(self.f.root, "L2")
        self.assertEqual(self.f.deliver()["removed"], ["L2"])

    def test_delivered_lessons_name_the_veto_command_without_a_host(self):
        with mock.patch.object(d, "VETO", "run `scripts/learning-service veto {}` on the Coldfix host (docs/delivery.md)"):
            self.f.ledger(dict(a=lead("First", "Rule one.")))
            self.f.deliver()
        lesson = self.f.file("L2-first.md")
        self.assertNotIn("ssh ", lesson)
        self.assertIn("Veto: run `scripts/learning-service veto L2` on the Coldfix host (docs/delivery.md).", lesson)

    def test_lessons_the_check_rejects_write_nothing_and_are_retried(self):
        self.f.ledger(dict(a=lead("First", "Rule one.")))
        (self.f.folder / "L9-broken.md").write_text("---\nid: L9\n---\n# L9 Broken\n")
        before = {p.name: p.read_bytes() for p in self.f.folder.iterdir()}
        with self.assertRaisesRegex(d.c.ColdfixError, "lessons.py check rejected the lessons: L9-broken.md: empty section Verify"):
            self.f.deliver()
        self.assertEqual({p.name: p.read_bytes() for p in self.f.folder.iterdir()}, before)
        self.assertNotIn("L2", d.week(self.f.root))
        (self.f.folder / "L9-broken.md").unlink()
        self.assertEqual(self.f.deliver()["added"], ["L2"])
        self.assertIn("lessons/L2-first.md", self.f.files())

    def test_writes_lessons_index_and_briefing_by_rename_and_a_rerun_rewrites_nothing(self):
        self.f.ledger(dict(a=lead("First", "Rule one.")))
        renames = []
        replace = os.replace

        def record(source, destination):
            renames.append((Path(source).parent, Path(source).name.startswith(".pending-"), Path(destination).name))
            replace(source, destination)
        with mock.patch.object(d.c.os, "replace", record):
            self.f.deliver()
        written = {name for folder, temporary, name in renames if folder == self.f.folder and temporary}
        self.assertEqual(written, {"L2-first.md", "INDEX.md", "briefing.md"})
        self.assertEqual(self.f.files(), {"lessons/L1-docs-sync.md", "lessons/L2-first.md", "lessons/INDEX.md",
                                          "lessons/briefing.md"})
        self.assertIn("| [L2](L2-first.md) | first | transcript |", self.f.file("INDEX.md"))
        stamps = {p.name: p.stat().st_mtime_ns for p in self.f.folder.iterdir()}
        renames.clear()
        with mock.patch.object(d.c.os, "replace", record):
            self.assertEqual(self.f.deliver()["added"], [])
        self.assertEqual([r for r in renames if r[0] == self.f.folder], [])
        self.assertEqual({p.name: p.stat().st_mtime_ns for p in self.f.folder.iterdir()}, stamps)

    def test_a_run_without_new_lessons_points_the_briefing_at_the_folder_and_runs_only_the_lessons_tool(self):
        self.f.ledger({})
        commands, run = [], subprocess.run

        def record(command, **kwargs):
            commands.append(command)
            return run(command, **kwargs)
        with mock.patch.object(d.subprocess, "run", record):
            self.assertEqual(self.f.deliver(), dict(added=[], removed=[], duplicates=[]))
        self.assertEqual(commands, [[sys.executable, str(self.f.tool), "build"], [sys.executable, str(self.f.tool), "check"]])
        self.assertEqual(self.f.file("briefing.md"), self.f.briefing(L1_LINE))
        pointer = Path(re.search(r"`(.*)/<id>-<slug>\.md`", self.f.file("briefing.md"))[1])
        self.assertTrue((pointer / "L1-docs-sync.md").is_file())
        self.assertEqual(self.f.files(), {"lessons/L1-docs-sync.md", "lessons/INDEX.md", "lessons/briefing.md"})
        stamps = {p.name: p.stat().st_mtime_ns for p in self.f.folder.iterdir()}
        self.f.deliver()
        self.assertEqual({p.name: p.stat().st_mtime_ns for p in self.f.folder.iterdir()}, stamps)

    def test_a_synced_copy_points_the_briefing_at_the_shared_folder(self):
        self.f.ledger({})
        with mock.patch.object(d, "SHARED", "/mnt/agent-space/coldfix-lessons", create=True):
            self.f.deliver()
        self.assertIn("verify each: `/mnt/agent-space/coldfix-lessons/<id>-<slug>.md`.", self.f.file("briefing.md"))
        self.assertNotIn(str(self.f.folder), self.f.file("briefing.md"))

    def test_a_briefing_without_a_lesson_file_pointer_writes_nothing(self):
        self.f.ledger(dict(a=lead("First", "Rule one.")))
        before = {p.name: p.read_bytes() for p in self.f.folder.iterdir()}
        with mock.patch.object(d, "proven_briefing", return_value="Details: elsewhere.\n"), \
                self.assertRaisesRegex(d.c.ColdfixError, "no single lesson-file pointer"):
            self.f.deliver()
        self.assertEqual({p.name: p.read_bytes() for p in self.f.folder.iterdir()}, before)

    def test_the_briefing_lists_only_active_proven_lessons(self):
        def lesson(number, slug, **fields):
            text = L1.replace("id: L1", f"id: L{number}").replace("slug: docs-sync", f"slug: {slug}")
            for key, value in fields.items():
                text = re.sub(rf"^{key}: .*$", f"{key}: {value}", text, count=1, flags=re.M)
            (self.f.folder / f"L{number}-{slug}.md").write_text(text)
        lesson(2, "replay-failed", verification="cited source fix absent: x#1 (2026-10-28 replay FAIL)")
        lesson(3, "unproven", verification="unproven overall (partial PASS)")
        lesson(4, "judged", scope="transcript", verification="judged")
        lesson(5, "proposed", status="proposed")
        lesson(6, "partly-proven", verification="proven (PASS; cited cases only)")
        lesson(7, "repo-only", scope="repo:a")
        self.f.ledger({})
        self.f.deliver()
        self.assertEqual(self.f.file("briefing.md"), self.f.briefing(
            L1_LINE, "- **L6 partly-proven** (replayed): Docs change with the code."))

    def test_judged_and_replayed_lessons_carry_their_label_and_judged_is_never_called_proven(self):
        self.f.ledger(dict(a=lead("First", "Check receipt ownership.")))
        self.f.deliver()
        path = self.f.folder / "L2-first.md"
        legacy = path.read_text()
        for old, new in d.JUDGED_WORDING:
            legacy = legacy.replace(new, old)
        self.assertIn("from a proven transcript lesson.", legacy)
        path.write_text(legacy)
        asked = len(self.f.asked)
        self.assertEqual(self.f.deliver(), dict(added=[], removed=[], duplicates=[]))
        self.assertEqual(len(self.f.asked), asked)
        judged = path.read_text()
        self.assertIn("\nverification: judged\n", judged)
        self.assertIn("from a judged transcript lesson.", judged)
        self.assertNotRegex(judged, r"(?i)\bproven\b")
        briefing = self.f.file("briefing.md")
        self.assertIn("- **L1 docs-sync** (replayed): ", briefing)
        self.assertNotIn("L2", briefing)
        self.assertNotRegex(briefing, r"(?i)\bproven\b")
        lines = [line for line in briefing.splitlines() if line.startswith("- ")]
        self.assertTrue(lines and all(re.match(r"- \*\*L\d+ [^*]+\*\* \((replayed|judged)\): ", line) for line in lines), lines)
        self.assertEqual(d.evidence(dict(scope="transcript", verification="proven (Jev transcript replay)")), "judged")
        authored = "Quote How this was proven, step 3.\n8. Proven records must be retained.\n## How this was proven later\n"
        self.assertEqual(d.reword(authored), authored)
        self.assertEqual(d.reword(authored + "## How this was proven\nSummary.\n8. Proven 2026-11-07 (batch x).\n"),
                         authored + "## How this was judged\nSummary.\n8. Judged 2026-11-07 (batch x).\n")

    def test_an_enforced_lesson_leaves_the_briefing(self):
        l13 = (L1.replace("id: L1", "id: L13").replace("slug: docs-sync", "slug: conflict-markers")
               .replace("briefing: Docs change with the code.", "briefing: Never commit merge markers."))
        path = self.f.folder / "L13-conflict-markers.md"
        path.write_text(l13)
        self.f.ledger({})
        self.f.deliver()
        self.assertIn("- **L13 conflict-markers** (replayed): Never commit merge markers.", self.f.file("briefing.md"))
        path.write_text(re.sub(r"^status: active$", "status: enforced", l13, count=1, flags=re.M))
        self.f.deliver()
        self.assertEqual(self.f.file("briefing.md"), self.f.briefing(L1_LINE))

    def test_shared_folder_files_stay_group_and_world_readable(self):
        self.f.ledger(dict(a=lead("First", "Rule one.")))
        self.f.deliver()
        modes = {name: (self.f.folder / name).stat().st_mode & 0o777 for name in ("L2-first.md", "INDEX.md", "briefing.md")}
        self.assertEqual(modes, dict.fromkeys(modes, 0o664))

    def cli(self, *args):
        out = io.StringIO()
        with mock.patch.object(d, "FOLDER", str(self.f.folder)), mock.patch.object(d, "TOOL", str(self.f.tool)), \
                mock.patch.object(d, "RULES", str(self.f.rules)), redirect_stdout(out), \
                mock.patch.object(d, "summarize", side_effect=AssertionError("no model call expected")):
            code = d.main(["--store", str(self.f.root), *args])
        return code, out.getvalue()

    def test_veto_command_removes_the_lesson_from_the_folder_index_briefing_and_listing(self):
        self.f.ledger(dict(a=lead("First", "Rule one.")))
        self.f.deliver()
        self.assertIn("L2\ttranscript\tactive\tFirst\tL2-first.md\n", self.cli("list")[1])
        code, out = self.cli("veto", "L2")
        self.assertEqual((code, json.loads(out)["removed"]), (0, ["L2"]))
        self.assertEqual(self.f.files(), {"lessons/INDEX.md", "lessons/L1-docs-sync.md", "lessons/briefing.md"})
        for name in ("INDEX.md", "briefing.md"):
            self.assertNotIn("L2", self.f.file(name))
        self.assertEqual(self.cli("list"), (0, "L1\tglobal\tactive\tKeep docs in sync\tL1-docs-sync.md\n"))

    def test_a_judged_lesson_reaches_the_briefing_only_after_a_recorded_approval_for_named_repos(self):
        self.f.ledger(dict(a=lead("First", "Rule one.")))
        self.f.deliver()
        path = self.f.folder / "L2-first.md"
        forged = re.sub(r"^repos:.*$", "repos: example/coldfix\napproved_by: reviewer\napproved_at: 2026-11-12T00:00:00+00:00",
                        path.read_text(), count=1, flags=re.M)
        path.write_text(forged)
        self.assertEqual(self.cli("deliver")[0], 0)
        self.assertNotIn("L2", self.f.file("briefing.md"))
        self.assertNotIn("approved_by", self.f.file("L2-first.md"))
        self.assertIn("\nrepos:\n", self.f.file("L2-first.md"))
        for args in (("--by", "reviewer", "--repos", ""), ("--by", "", "--repos", "example/coldfix"),
                     ("--by", "reviewer", "--repos", "example/coldfix,../x"), ("--by", "two words", "--repos", "coldfix"),
                     ("--by", "reviewer", "--repos", "coldfix,coldfix"), ("--by", "reviewer", "--repos", "coldfix,")):
            with self.assertRaises(d.c.ColdfixError):
                self.cli("approve", "L2", *args)
        with self.assertRaises(d.c.ColdfixError):
            self.cli("approve", "L9", "--by", "reviewer", "--repos", "coldfix")
        self.assertNotIn("L2", self.f.file("briefing.md"))
        self.assertEqual(self.cli("approve", "L2", "--by", "reviewer", "--repos", "Example/coldfix, coldpass")[0], 0)
        self.assertIn("- **L2 first** (judged): Rule one.\n", self.f.file("briefing.md"))
        approved = self.f.file("L2-first.md")
        self.assertIn("\nrepos: Example/coldfix, coldpass\n", approved)
        self.assertIn("\napproved_by: reviewer\n", approved)
        entry = json.loads((self.f.root / d.DELIVERIES).read_text())["leads"]["a"]
        self.assertEqual((entry["approved_by"], entry["approved_repos"]), ("reviewer", ["Example/coldfix", "coldpass"]))
        self.assertRegex(approved, rf"\napproved_at: {re.escape(entry['approved_at'])}\n")
        self.assertEqual(self.cli("deliver")[0], 0)
        self.assertEqual(self.f.file("L2-first.md"), approved)
        path.write_text(re.sub(r"^briefing: .*$", "briefing: A different rule.", approved, count=1, flags=re.M))
        self.assertNotIn("L2", d.proven_briefing(self.f.folder, d.approvals(json.loads((self.f.root / d.DELIVERIES).read_text())["leads"])))
        d.veto(self.f.root, "L2")
        self.assertNotIn("L2", d.proven_briefing(self.f.folder, d.approvals(json.loads((self.f.root / d.DELIVERIES).read_text())["leads"])))

    def test_a_finding_no_replay_can_show_waits_visibly_and_is_delivered_only_after_approval(self):
        self.f.ledger(dict(f=finding_lead("Bound every retry loop", "Give every retry loop a limit.", "maintainability")))
        self.assertEqual(self.f.deliver()["added"], [])
        self.assertEqual(self.f.files(), {"lessons/INDEX.md", "lessons/L1-docs-sync.md", "lessons/briefing.md"})
        code, waiting = self.cli("pending")
        self.assertEqual(code, 0)
        for text in ("f\tmaintainability\tBound every retry loop", "finding: The agent left the retry loop unbounded.",
                     "evidence: I wrapped the call in while True", "caught by reviewer", "approve f --by", "reject f --by"):
            self.assertIn(text, waiting)
        self.assertIn("- finding `f` (maintainability) Bound every retry loop", d.week(self.f.root))
        with self.assertRaises(d.c.ColdfixError):
            self.cli("approve", "f", "--by", "reviewer", "--repos", "coldfix,coldfix")
        with self.assertRaises(d.c.ColdfixError):
            self.cli("approve", "g", "--by", "reviewer", "--repos", "coldfix")
        self.assertEqual(json.loads((self.f.root / d.DELIVERIES).read_text())["leads"], {})
        d.approve(self.f.root, "f", "reviewer", "example/coldfix", self.f.folder)
        entry = json.loads((self.f.root / d.DELIVERIES).read_text())["leads"]["f"]
        self.assertEqual((entry["status"], entry["approved_by"], entry["approved_repos"]), ("approved", "reviewer", ["example/coldfix"]))
        self.assertEqual(self.f.deliver()["added"], ["L2"])
        delivered = self.f.file("L2-bound-every-retry-loop.md")
        for text in ("\nverification: needs_approval\nfinding_class: maintainability\n", "\nrepos: example/coldfix\n",
                     "\napproved_by: reviewer\n", "## Evidence", "I wrapped the call in while True"):
            self.assertIn(text, delivered)
        self.assertIn("- **L2 bound-every-retry-loop** (approved): Give every retry loop a limit.\n", self.f.file("briefing.md"))
        self.assertEqual(self.cli("pending"), (0, ""))
        self.assertEqual(self.cli("deliver")[0], 0)
        self.assertEqual(self.f.file("L2-bound-every-retry-loop.md"), delivered)
        path = self.f.folder / "L2-bound-every-retry-loop.md"
        path.write_text(delivered.replace("briefing: Give every retry loop a limit.", "briefing: Cap every retry loop."))
        d.veto(self.f.root, "L2")
        d.reconsider(self.f.root, "L2")
        self.assertEqual(self.cli("pending"), (0, ""))
        d.veto(self.f.root, "L2")
        self.assertEqual(self.f.deliver()["removed"], ["L2"])
        d.reconsider(self.f.root, "L2")
        self.assertIn("f\tmaintainability", self.cli("pending")[1])
        self.assertIn("rule: Cap every retry loop.", self.cli("pending")[1])
        self.assertEqual(self.f.deliver()["added"], [])
        self.assertNotIn("lessons/L2-bound-every-retry-loop.md", self.f.files())
        d.approve(self.f.root, "f", "reviewer", "coldpass", self.f.folder)
        self.assertEqual(self.f.deliver()["added"], ["L2"])
        restored = self.f.file("L2-bound-every-retry-loop.md")
        self.assertIn("\nrepos: coldpass\n", restored)
        self.assertIn("- **L2 bound-every-retry-loop** (approved): Cap every retry loop.\n", self.f.file("briefing.md"))

    def test_a_rejected_finding_is_recorded_and_never_delivered(self):
        self.f.ledger(dict(f=finding_lead("Keep secrets out of logs", "Never log request headers.", "security"),
                           a=lead("First", "Rule one.")))
        with self.assertRaises(d.c.ColdfixError):
            self.cli("reject", "a", "--by", "reviewer")
        with self.assertRaises(d.c.ColdfixError):
            self.cli("reject", "f", "--by", "two words")
        self.assertEqual(self.cli("reject", "f", "--by", "reviewer"), (0, ""))
        entry = json.loads((self.f.root / d.DELIVERIES).read_text())["leads"]["f"]
        self.assertEqual((entry["status"], entry["rejected_by"], entry["finding_class"]), ("rejected", "reviewer", "security"))
        self.assertIn("rejected_at", entry)
        self.assertEqual(self.f.deliver()["added"], ["L2"])
        self.assertNotIn("secrets", self.f.file("INDEX.md") + self.f.file("briefing.md"))
        self.assertEqual(self.cli("pending"), (0, ""))
        with self.assertRaises(d.c.ColdfixError):
            d.approve(self.f.root, "f", "reviewer", "coldfix", self.f.folder)

    def test_list_reads_only_the_lessons_folder_in_number_order(self):
        for number in (10, 2):
            (self.f.folder / f"L{number}-x.md").write_text(L1.replace("id: L1", f"id: L{number}").replace("Keep docs", f"Rule {number}"))
        (self.f.folder / "README.md").write_text("# not a lesson\n")
        self.assertEqual([line.split("\t")[0] for line in self.cli("list")[1].splitlines()], ["L1", "L2", "L10"])
        with mock.patch.object(d, "FOLDER", ""), self.assertRaises(d.c.ColdfixError):
            d.main(["--store", str(self.f.root), "list"])

    def test_reconsider_runs_the_gate_again_on_a_lesson_removed_without_a_recorded_reason(self):
        self.f.ledger(dict(a=lead("First", "Rule one."), b=lead("Second", "Rule two."), c=lead("Third", "Rule three.")))
        self.assertEqual(self.f.deliver()["added"], ["L2", "L3", "L4"])
        (self.f.folder / "L2-first.md").unlink()
        (self.f.folder / "L3-second.md").unlink()
        self.assertEqual(self.f.deliver()["added"], [])
        with self.assertRaises(d.c.ColdfixError):
            d.reconsider(self.f.root, "L4")
        d.reconsider(self.f.root, "L2")
        d.reconsider(self.f.root, "L3")
        self.f.replies = dict(b=dict(duplicate_of="L4"))
        result = self.f.deliver()
        self.assertEqual((result["added"], result["duplicates"]), (["L2"], ["b"]))
        self.assertIn("lessons/L2-first.md", self.f.files())
        self.assertNotIn("lessons/L3-second.md", self.f.files())
        saved = json.loads((self.f.root / d.DELIVERIES).read_text())["leads"]
        self.assertEqual((saved["a"]["status"], saved["a"]["lesson"], saved["a"]["previous"]["reason"]),
                         ("delivered", "L2", "removed from the lessons folder"))
        self.assertEqual((saved["b"]["status"], saved["b"]["reason"], saved["b"]["previous"]["reason"]),
                         ("vetoed", "duplicate: L4", "removed from the lessons folder"))
        self.assertNotIn("reconsider", saved["a"])
        self.assertNotIn("reconsider", saved["b"])
        calls = len(self.f.asked)
        self.assertEqual(self.f.deliver()["added"], [])
        self.assertFalse(any('"id": "b"' in prompt for prompt in self.f.asked[calls:]))

    def test_reconsider_retries_after_a_failed_gate_call(self):
        self.f.ledger(dict(a=lead("First", "Rule one.")))
        self.f.deliver()
        (self.f.folder / "L2-first.md").unlink()
        self.f.deliver()
        d.reconsider(self.f.root, "L2")
        self.f.fail_ask = True
        self.assertIn("error", self.f.deliver())
        self.assertTrue(json.loads((self.f.root / d.DELIVERIES).read_text())["leads"]["a"]["reconsider"])
        self.f.fail_ask = False
        self.assertEqual(self.f.deliver()["added"], ["L2"])

    def test_slug_cuts_long_titles_at_a_word_boundary(self):
        for title, expected in ((LONG, "use-the-documented-worker-lifecycle-and-receipt"),
                                (MID_WORD, "verify-the-service-itself-before-reporting-it"),
                                ("Do not describe an unfinished check as a blocker when a documented fallback works",
                                 "do-not-describe-an-unfinished-check-as-a-blocker"),
                                ("x" * 60, "x" * 48), ("Keep docs in sync!", "keep-docs-in-sync")):
            self.assertEqual(d.slug(title), expected)

    def old_delivery(self, **leads):
        self.f.ledger(leads)
        with mock.patch.object(d, "slug", lambda title: re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:48]):
            self.f.deliver()

    def contents(self):
        return {p.name: p.read_bytes() for p in self.f.folder.iterdir()}

    def test_truncated_lesson_files_are_renamed_once_with_their_ids_and_no_new_gate_call(self):
        self.old_delivery(a=lead(LONG, "Rule one."), b=lead(MID_WORD, "Rule two."), c=lead("Short", "Rule three."))
        self.assertLessEqual({"lessons/" + OLD_LONG, "lessons/" + OLD_MID, "lessons/L4-short.md"}, self.f.files())
        calls = len(self.f.asked)
        self.assertEqual(self.f.deliver(), dict(added=[], removed=[], duplicates=[]))
        self.assertEqual(len(self.f.asked), calls)
        self.assertEqual(self.f.files(), {"lessons/INDEX.md", "lessons/briefing.md", "lessons/L1-docs-sync.md",
                                          "lessons/" + NEW_LONG, "lessons/" + NEW_MID, "lessons/L4-short.md"})
        self.assertFalse([name for name in self.f.files() if name.endswith("-.md")])
        for name in (NEW_LONG, NEW_MID):
            self.assertIn(f"\nslug: {name[3:-3]}\n", self.f.file(name))
            self.assertIn(f"({name})", self.f.file("INDEX.md"))
        self.assertNotIn(OLD_LONG, self.f.file("INDEX.md"))
        saved = json.loads((self.f.root / d.DELIVERIES).read_text())["leads"]
        self.assertEqual({k: (e["lesson"], e["file"]) for k, e in saved.items()},
                         dict(a=("L2", NEW_LONG), b=("L3", NEW_MID), c=("L4", "L4-short.md")))
        before = self.contents()
        self.assertEqual(self.f.deliver(), dict(added=[], removed=[], duplicates=[]))
        self.assertEqual((self.contents(), len(self.f.asked)), (before, calls))
        d.veto(self.f.root, "L2")
        self.assertEqual(self.f.deliver()["removed"], ["L2"])
        self.assertNotIn("lessons/" + NEW_LONG, self.f.files())

    def test_a_rename_interrupted_before_the_old_file_was_removed_finishes_without_a_gate_call(self):
        self.old_delivery(a=lead(LONG, "Rule one."))
        (self.f.folder / NEW_LONG).write_text(d.reslug(self.f.file(OLD_LONG), LONG), encoding="utf-8")
        calls = len(self.f.asked)
        self.assertEqual(self.f.deliver(), dict(added=[], removed=[], duplicates=[]))
        self.assertEqual(len(self.f.asked), calls)
        self.assertIn("lessons/" + NEW_LONG, self.f.files())
        self.assertNotIn("lessons/" + OLD_LONG, self.f.files())

    def test_a_veto_after_an_interrupted_rename_removes_both_names(self):
        self.old_delivery(a=lead(LONG, "Rule one."))
        (self.f.folder / NEW_LONG).write_text(d.reslug(self.f.file(OLD_LONG), LONG), encoding="utf-8")
        d.veto(self.f.root, "L2")
        self.assertEqual(self.f.deliver()["removed"], ["L2"])
        self.assertEqual(self.f.files(), {"lessons/INDEX.md", "lessons/briefing.md", "lessons/L1-docs-sync.md"})
        self.assertIn("slug: use-the-documented-worker-lifecycle-and-receipt\n",
                      (self.f.root / d.REMOVED / NEW_LONG).read_text(encoding="utf-8"))

    def test_a_failed_run_publishes_no_rename_and_the_next_run_does(self):
        self.old_delivery(a=lead(LONG, "Rule one."))
        self.f.ledger(dict(a=lead(LONG, "Rule one."), b=lead("Second", "Rule two.")))
        before = self.contents()
        self.f.fail_ask = True
        self.assertIn("error", self.f.deliver())
        self.assertEqual(self.contents(), before)
        self.f.fail_ask = False
        self.assertEqual(self.f.deliver()["added"], ["L3"])
        self.assertLessEqual({"lessons/" + NEW_LONG, "lessons/L3-second.md"}, self.f.files())
        self.assertNotIn("lessons/" + OLD_LONG, self.f.files())

    def test_a_lesson_removed_under_its_truncated_name_is_restored_under_the_word_boundary_name(self):
        self.old_delivery(a=lead(LONG, "Rule one."))
        (self.f.folder / OLD_LONG).unlink()
        self.f.deliver()
        d.reconsider(self.f.root, "L2")
        self.assertEqual(self.f.deliver()["added"], ["L2"])
        self.assertIn("lessons/" + NEW_LONG, self.f.files())
        self.assertIn("\nslug: use-the-documented-worker-lifecycle-and-receipt\n", self.f.file(NEW_LONG))


LONG = "Use the documented worker lifecycle and receipt wake path"
MID_WORD = "Verify the service itself before reporting it active"
OLD_LONG, NEW_LONG = "L2-use-the-documented-worker-lifecycle-and-receipt-.md", "L2-use-the-documented-worker-lifecycle-and-receipt.md"
RECORD = "example/fleet@" + "8f0b15b9" * 5 + ":evidence/lesson-repro-20261028/L1/result.json"
EVIDENCE_LINE = "evidence: replay " + RECORD + "\n"
SEED_EVIDENCE = """
## Evidence
- [2026-10-28 reproduction result](https://github.com/example/fleet/blob/""" + "8f0b15b9" * 5 + """/evidence/lesson-repro-20261028/L1/result.json)
- example/ops#480 docs regenerated by hand
- https://github.com/example/bot/pull/8#discussion_r1
- example/ops#481 again
"""
JUDGED = """---
id: L2
title: Check the requested device first
source: coldfix-transcript:a
verification: judged
---
# L2 Check the requested device first

## Fix
Check the device the user named before any other.
"""
TURNS = dict(m1="user [m1]: Check the router.", m2="assistant [m2]: I checked the modem.",
             m3="assistant [m3]: I checked the router.")


def failure(occurred, turns=TURNS):
    return dict(occurred=occurred, source_ref="transcript:/x/y.md#messages=" + ",".join(turns),
                error=json.dumps(dict(type="transcript_correction", evidence="\n\n".join(turns.values()))))


class LessonEffectTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / "store"
        self.folder = Path(directory.name) / "coldfix-lessons"
        self.root.mkdir()
        self.folder.mkdir()
        (self.folder / "L1-docs-sync.md").write_text(L1)
        (self.folder / "L2-check-the-requested-device-first.md").write_text(JUDGED)
        (self.root / d.DELIVERIES).write_text(json.dumps(dict(leads=dict(
            a=dict(status="delivered", lesson="L2", delivered="2026-11-07T00:00:00+00:00")))))
        self.store = dict(failures=dict(a=failure("2026-11-05T10:00:00Z")))

    def ledger(self, **leads):
        (self.root / d.LEDGER).write_text(json.dumps(dict(leads=dict(a=lead("t", "r"), **leads), batches=[])))

    def measure(self, name):
        self.assertTrue(callable(getattr(d, name, None)), "transcript_delivery has no " + name)
        return getattr(d, name)

    def recheck(self, jev):
        return {row["lesson"]: {k: v for k, v in row.items() if k != "lesson"}
                for row in self.measure("recheck")(self.root, self.store, self.folder, jev)}

    def test_recheck_rejudges_a_judged_lesson_against_its_stored_wrong_and_corrected_messages(self):
        self.ledger()
        asked = []

        def jev(state, name, *rest):
            asked.append((name, state["rule"], state["message"], [row["id"] for row in state["conversation_before"]]))
            return scores[state["message"]]
        scores = {"I checked the modem.": 0.9, "I checked the router.": 0.1}
        self.assertEqual(self.recheck(jev), dict(L1=dict(result="unchecked", reason="no witness"), L2=dict(result="passed", reason="judge flags the mistake and clears the corrected message")))
        rule = "Check the device the user named before any other."
        self.assertEqual(asked, [("breaks_rule", rule, "I checked the modem.", ["m1"]),
                                 ("breaks_rule", rule, "I checked the router.", ["m1", "m2"])])
        scores["I checked the modem."] = 0.5
        self.assertEqual(self.recheck(jev)["L2"], dict(result="regression", reason="check misses the mistake"))
        scores.update({"I checked the modem.": 0.9, "I checked the router.": 0.6})
        self.assertEqual(self.recheck(jev)["L2"], dict(result="regression", reason="check fails the corrected message"))

    def test_recheck_reports_a_judged_lesson_unchecked_when_it_cannot_be_judged(self):
        self.ledger()

        def down(*args):
            raise OSError("unreachable")
        self.assertEqual(self.recheck(down)["L2"], dict(result="unchecked", reason="judged: check failed: OSError"))
        self.assertEqual(self.recheck(None)["L2"], dict(result="unchecked", reason="judged: jev unavailable"))
        self.store["failures"]["a"] = failure("2026-11-05T10:00:00Z", dict(m1=TURNS["m1"], m2=TURNS["m2"]))
        self.assertEqual(self.recheck(lambda *args: 0.0)["L2"],
                         dict(result="unchecked", reason="judged: stored messages missing"))

    def evidence_lesson(self, status="active", evidence=EVIDENCE_LINE):
        (self.folder / "L1-docs-sync.md").write_text(L1.replace("status: active", "status: " + status).replace(
            "verification: proven\n", "verification: proven\n" + evidence) + SEED_EVIDENCE)

    def test_recheck_checks_stated_replay_evidence_and_flags_cited_code_that_is_gone(self):
        self.ledger()
        present = {RECORD: True, "example/fleet": True, "example/ops": True, "example/bot": True}
        asked = []

        def exists(ref):
            asked.append(ref)
            return present.get(ref)
        self.evidence_lesson()
        recheck = lambda: {row["lesson"]: row for row in d.recheck(self.root, self.store, self.folder, None, exists)}["L1"]
        self.assertEqual(recheck(), dict(lesson="L1", result="passed",
                                         reason="replay record present; 2 of 2 cited repos present"))
        self.assertEqual(sorted(asked), sorted(present))
        present["example/bot"] = False
        self.assertEqual(recheck(), dict(lesson="L1", result="passed", gone=["example/bot"],
                                         reason="replay record present; 1 of 2 cited repos present"))
        present[RECORD] = False
        self.assertEqual(recheck(), dict(lesson="L1", result="regression", gone=[RECORD, "example/bot"],
                                         reason="cited code gone: replay record " + RECORD))
        present.update({RECORD: True, "example/bot": None, "example/ops": False})
        self.assertEqual(recheck(), dict(lesson="L1", result="unchecked", reason="evidence lookup failed",
                                         gone=["example/ops"]))
        present["example/ops"] = True
        present["example/bot"] = True
        self.evidence_lesson("proposed")
        self.assertEqual(recheck(), dict(lesson="L1", result="regression", reason="unproven: proven"))
        self.evidence_lesson(evidence="evidence: replay example/fleet@main:x\n")
        self.assertEqual(recheck(), dict(lesson="L1", result="unchecked", reason="no witness"))

    def test_recheck_reports_ended_states_as_retired_without_judging_them(self):
        self.ledger()
        self.evidence_lesson("superseded")
        (self.folder / "L1-docs-sync.md").write_text(
            (self.folder / "L1-docs-sync.md").read_text().replace("status: superseded", "status: superseded\nsuperseded_by: L9"))
        (self.folder / "L2-check-the-requested-device-first.md").write_text(
            JUDGED.replace("verification: judged", "status: contradicted\nverification: judged"))

        def unused(*args):
            raise AssertionError("an ended lesson was checked")
        self.assertEqual(d.recheck(self.root, self.store, self.folder, unused, unused),
                         [dict(lesson="L1", result="retired", reason="superseded by L9"),
                          dict(lesson="L2", result="retired", reason="contradicted")])

    def test_migrate_states_each_seed_lessons_replay_evidence_once(self):
        self.evidence_lesson(evidence="")
        (self.folder / "L3-no-record.md").write_text(L1.replace("id: L1", "id: L3"))
        before = {path.name: path.read_text() for path in self.folder.iterdir()}
        self.assertEqual(d.migrate(self.folder), ["L1"])
        self.assertEqual((self.folder / "L1-docs-sync.md").read_text(),
                         before["L1-docs-sync.md"].replace("verification: proven\n---", "verification: proven\n"
                                                           + EVIDENCE_LINE + "---"))
        self.assertEqual(d.lessons(self.folder)["L1-docs-sync.md"]["evidence"], EVIDENCE_LINE[len("evidence: "):-1])
        after = {path.name: path.read_text() for path in self.folder.iterdir()}
        self.assertEqual({name for name in before if before[name] != after[name]}, {"L1-docs-sync.md"})
        self.assertEqual(d.migrate(self.folder), [])

    def test_recurrence_counts_group_failures_around_delivery_and_lists_recurring_lessons(self):
        (self.folder / "L3-unmatched.md").write_text("---\nid: L3\nverification: judged\n---\n")
        duplicate = lambda reason, batch: dict(status="duplicate", reason=reason, batch=batch)
        self.ledger(s1=duplicate("coldlocker:lessons/L1-docs-sync", "2026-11-07T00:00:00+00:00"),
                    s2=duplicate("coldlocker:coldfix-lessons/L1-docs-sync", "2026-11-06T00:00:00+00:00"),
                    s3=duplicate("coldlocker:lessons/L1-docs-sync", "2026-11-08T00:00:00+00:00"),
                    b=duplicate("transcript:a", "2026-11-09T00:00:00+00:00"),
                    r=duplicate("coldlocker:rules/verify-before-merge", "2026-11-06T00:00:00+00:00"),
                    g=duplicate("coldlocker:lessons/L20-gone", "2026-11-06T00:00:00+00:00"),
                    n=dict(status="no_lesson", reason="no_mistake"), v=dict(lead("t", "r"), batch="2026-11-10T00:00:00+00:00"))
        deliveries = json.loads((self.root / d.DELIVERIES).read_text())
        deliveries["leads"]["v"] = dict(status="duplicate", reason="duplicate: L1", duplicate_of="L1")
        (self.root / d.DELIVERIES).write_text(json.dumps(deliveries))
        self.store["failures"].update(s1=failure("2026-11-01T00:00:00Z"), s2=failure("2026-11-06T12:00:00Z"),
                                      s3=failure("2026-11-20T00:00:00Z"), b=failure("2026-11-08T00:00:00Z"),
                                      r=failure("2026-11-08T00:00:00Z"), g=failure("2026-11-08T00:00:00Z"),
                                      n=failure("2026-11-08T00:00:00Z"), v=failure("2026-11-10T00:00:00Z"))
        rows, promote = self.measure("recurrence")(self.root, self.store, self.folder)
        self.assertEqual(rows[0], dict(lesson="L1", delivered=None, delivered_by=None))
        self.assertEqual(promote, [])
        l1 = self.folder / "L1-docs-sync.md"
        l1.write_text(L1.replace("---\n", "---\ndelivered: 2026-11-06T00:00:00Z\n", 1))
        rows, promote = self.measure("recurrence")(self.root, self.store, self.folder)
        self.assertEqual(rows, [
            dict(lesson="L1", delivered="2026-11-06T00:00:00Z", delivered_by="lesson file",
                 failures_before=1, failures_after=2, failures_since=3),
            dict(lesson="L2", delivered="2026-11-07T00:00:00+00:00", delivered_by="delivery record",
                 failures_before=1, failures_after=1, failures_since=1),
            dict(lesson="L3", delivered=None, delivered_by=None)])
        self.assertEqual(promote, ["L1"])
        for bad in ("", "2026-11-06", "2026-11-06T00:00:00", "2026-11-06T25:00:00Z", "soon"):
            l1.write_text(L1.replace("---\n", f"---\ndelivered: {bad}\n", 1))
            with self.assertRaisesRegex(c.ColdfixError, "L1-docs-sync.md: delivered must be a timestamp"):
                self.measure("recurrence")(self.root, self.store, self.folder)


OLD_MID, NEW_MID = "L3-verify-the-service-itself-before-reporting-it-ac.md", "L3-verify-the-service-itself-before-reporting-it.md"


if __name__ == "__main__":
    unittest.main()
