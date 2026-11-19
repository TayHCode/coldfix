import json
import io
import os
import platform
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import coldfix as c
import failure_ingest as ingest
import learn_candidates
import learn_daily
import learn_replay
import learn_report


ROOT = Path(__file__).resolve().parents[1]
RUN = json.loads((ROOT / "tests" / "fixtures" / "failed-ci-run.json").read_text())
TERRA = dict(provider="codex", model="drafter-model", effort="high")
SOL = dict(provider="codex", model="reviewer-model", effort="high")


class LearnDailyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Path(temporary.name) / ".coldfix"
        self.store.mkdir(mode=0o700)
        self.run_id = 1000
        self.routes, self.calls, self.cheap = [], [], set()
        environment = patch.dict(learn_daily.os.environ, FLEET_MODELS_FILE=str(self.store.parent / "missing.yaml"))
        environment.start()
        self.addCleanup(environment.stop)

    def recurring(self, message):
        for index in range(3):
            self.run_id += 1
            row = dict(RUN, head_sha=str(index) * 40, run_id=self.run_id, log_tail=f"AssertionError: {message}")
            ingest.import_records(self.store, [ingest.record(ingest.ci_event(row))])

    def route(self, *args):
        self.routes.append(args)
        return TERRA if args == ("bulk",) else SOL

    def ask(self, model, prompt, shape, work):
        self.calls.append(model["model"])
        cases = json.loads(prompt[prompt.index("\n") + 1:])
        if shape is learn_daily.DRAFTS:
            return dict(drafts=[dict(fingerprint=case["fingerprint"], kind="test", cause="c", prevention="p",
                                     title="Keep " + case["headline"],
                                     rule="Run the tests for " + case["headline"] + ".", check_command="python3 -m unittest")
                                for case in cases])
        return dict(reviews=[dict(fingerprint=draft["fingerprint"], verdict="PASS" if index == 0 else "FAIL",
                                  reason="checked") for index, draft in enumerate(cases["drafts"])])

    def daily(self, day="2026-11-06", ask=None):
        with patch.object(learn_daily, "cheap_models", lambda: self.cheap, create=True):
            return learn_daily.run(self.store, day, route=self.route, ask=ask or self.ask)

    def ledger(self):
        return json.loads((self.store / learn_daily.LEDGER).read_text())

    def test_no_candidates_makes_no_model_call(self):
        self.assertEqual(self.daily(), dict(day="2026-11-06", candidates=0, calls=0, drafted=0, passed=0))
        self.assertEqual((self.routes, self.calls), ([], []))

    def test_one_draft_call_and_one_review_call_by_a_different_model_capped_per_day(self):
        for name in "abcdefg":
            self.recurring(name)
        self.assertEqual(self.daily(), dict(day="2026-11-06", candidates=6, calls=2, drafted=6, passed=1))
        self.assertEqual(self.routes, [("bulk",), ("bulk-codex",)])
        self.assertEqual(self.calls, ["drafter-model", "reviewer-model"])
        drafts = self.ledger()["drafts"]
        self.assertEqual(sorted(row["verdict"] for row in drafts.values()), ["FAIL"] * 5 + ["PASS"])
        self.assertEqual(self.daily(), dict(day="2026-11-06", skipped="model calls already made today"))
        self.assertEqual(self.daily("2026-11-07"), dict(day="2026-11-07", candidates=1, calls=2, drafted=1, passed=1))
        self.assertEqual(self.daily("2026-11-08"), dict(day="2026-11-08", candidates=0, calls=0, drafted=0, passed=0))
        self.assertEqual(len(self.calls), 4)

    def test_fixed_pairs_are_drafted_with_recurring_failures_and_the_drafter_sees_the_fix(self):
        for name in "abcd":
            self.recurring(name)
        refs = [f"https://github.com/example/project/pull/{number}#discussion_r{number}" for number in (1, 2)]
        body = ("**<sub><sub>![P2 Badge](https://img.shields.io/badge/P2-yellow?style=flat)</sub></sub>  Keep the value"
                " positive**\n\n" + "x" * 400 + "\nThe total raises an error.\n")
        for ref, detail in zip(refs, ("Totals go negative.", "Exports drop rows.")):
            review = c.event_record(dict(kind="review", command="PR review finding", error=body + detail, repo="example/project",
                                         agent="codex-review", source_ref=ref, occurred="2026-10-20T12:00:00Z"), "github")
            c.save(self.store, "failures", review)
        (self.store / "backlog-pairs.json").write_text(json.dumps(dict(results=[
            dict(repo="example/project", finding_url=refs[0], before="a" * 40, after="b" * 40),
            dict(repo="example/project", finding_url=refs[1], before="a" * 40, after=None)])))
        prompts = []
        def ask(model, prompt, shape, work):
            prompts.append(prompt)
            return self.ask(model, prompt, shape, work)
        with patch.object(learn_daily, "compare", return_value="-VALUE = -1\n+VALUE = 1\n", create=True) as compare:
            self.assertEqual(self.daily(ask=ask)["candidates"], 5)
        compare.assert_called_once_with("example/project", "a" * 40, "b" * 40)
        cases = json.loads(prompts[0][prompts[0].index("\n") + 1:])
        [fixed] = [case for case in cases if case["headline"] == "Keep the value positive"]
        self.assertEqual(fixed["fix"], "-VALUE = -1\n+VALUE = 1\n")
        self.assertEqual(fixed["excerpts"], [dict(command="PR review finding", error=body + "Totals go negative.")])
        self.assertEqual(sum("fix" in case for case in cases), 1)

    def test_review_pairs_passing_the_flagged_lines_precondition_are_drafted_first_and_failing_ones_not_at_all(self):
        refs = {}
        for name, text in (("inline", "Totals go negative."), ("eligible", "Exports drop rows."),
                           ("ineligible", "Imports skip headers.")):
            ref = f"https://github.com/example/project/pull/1#cold-review-{'a' * 40}-{c.digest(name)}"
            if name == "inline":
                ref = "https://github.com/example/project/pull/2#discussion_r2"
            review = c.event_record(dict(kind="review", command="review", error=text, repo="example/project",
                                         agent="reviewer", source_ref=ref, occurred="2026-10-20T12:00:00Z"), "cold-review")
            c.save(self.store, "failures", review)
            refs[name] = ref
        (self.store / "backlog-pairs.json").write_text(json.dumps(dict(results=[
            dict(repo="example/project", finding_url=refs["inline"], before="a" * 40, after="b" * 40)])))
        (self.store / learn_replay.REVIEW_PAIRS).write_text(json.dumps({
            "e": dict(repo="example/project", finding_url=refs["eligible"], before="a" * 40, after="c" * 40, flagged_only=True),
            "i": dict(repo="example/project", finding_url=refs["ineligible"], before="a" * 40, after="d" * 40, flagged_only=False)}))
        store = c.load_store(self.store)[1]
        rows = learn_daily.waiting(store, dict(drafts={}), learn_daily.fix_pairs(self.store))
        self.assertEqual([store["failures"][row["evidence"][0]]["source_ref"] for row in rows if row["replayable"]],
                         [refs["eligible"], refs["inline"]])

    def test_a_later_run_on_the_same_day_drafts_only_new_candidates_within_the_cap(self):
        drafted = []
        def ask(model, prompt, shape, work):
            if shape is learn_daily.DRAFTS:
                drafted.append(sorted(case["headline"] for case in json.loads(prompt[prompt.index("\n") + 1:])))
            return self.ask(model, prompt, shape, work)
        self.recurring("a")
        self.assertEqual(self.daily("2026-11-05", ask=ask)["passed"], 1)
        self.assertEqual(self.daily(ask=ask), dict(day="2026-11-06", candidates=0, calls=0, drafted=0, passed=0))
        self.recurring("b")
        self.assertEqual(self.daily(ask=ask), dict(day="2026-11-06", candidates=1, calls=2, drafted=1, passed=1))
        self.assertEqual(len(drafted), 2)
        self.assertNotEqual(drafted[0], drafted[1])
        self.assertEqual(len(drafted[1]), 1)
        self.recurring("c")
        self.assertEqual(self.daily(ask=ask), dict(day="2026-11-06", skipped="model calls already made today"))
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(self.daily("2026-11-07", ask=ask)["calls"], 2)
        self.assertEqual(learn_report.report(self.store, datetime(2026, 11, 7, 12, tzinfo=timezone.utc))["model_calls"], 6)

    def replayable(self, name):
        self.run_id += 1
        row = dict(RUN, head_sha=format(self.run_id, "040x"), run_id=self.run_id,
                   log_tail=f"FAIL: test_{name} (tests.test_{name}.Tests.test_{name})\nAssertionError: {name}")
        ingest.import_records(self.store, [ingest.record(ingest.ci_event(row))])

    def test_a_pytest_failure_with_a_named_test_is_a_replayable_candidate(self):
        row = dict(RUN, head_sha="a" * 40, log_tail="FAILED tests/test_a.py::Tests::test_a - AssertionError: a\n1 failed in 0.1s")
        ingest.import_records(self.store, [ingest.record(ingest.ci_event(row))])
        waiting = learn_candidates.candidates(c.load_store(self.store)[1])
        self.assertEqual([row["replayable"] for row in waiting], [True])

    def transcript_correction(self, name):
        evidence = f"user [1]: No, use the {name} helper.\n\nassistant [2]: Switched to the {name} helper."
        record = c.event_record(dict(kind="correction", repo="example/project", command="transcript correction",
                                     error=json.dumps(dict(type="transcript_correction", score=0.9, evidence=evidence)),
                                     agent="codex", source_ref="transcript:fixture-" + name,
                                     occurred="2026-11-05T12:00:00Z"), "transcripts")
        c.save(self.store, "failures", record)

    def test_cheap_routes_draft_every_replayable_candidate_and_no_transcript_correction(self):
        self.cheap = {"drafter-model", "reviewer-model"}
        names = [chr(ord("a") + index) for index in range(10)]
        for name in names:
            self.replayable(name)
            self.transcript_correction(name)
        waiting = learn_candidates.candidates(c.load_store(self.store)[1])
        self.assertEqual(sorted((row["replayable"], row["transcript"]) for row in waiting), [(False, True)] * 10 + [(True, False)] * 10)
        drafted = []
        def ask(model, prompt, shape, work):
            if shape is learn_daily.DRAFTS:
                drafted.extend(case["headline"] for case in json.loads(prompt[prompt.index("\n") + 1:]))
            return self.ask(model, prompt, shape, work)
        self.assertEqual(self.daily(ask=ask), dict(day="2026-11-06", candidates=10, calls=4, drafted=10, passed=2))
        self.assertEqual(sorted(drafted), sorted(f"FAIL: test_{name} (tests.test_{name}.Tests.test_{name})" for name in names))
        self.assertEqual(self.calls, ["drafter-model", "reviewer-model"] * 2)
        self.assertEqual(len(self.ledger()["drafts"]), 10)
        self.assertEqual(learn_report.report(self.store, datetime(2026, 11, 6, 12, tzinfo=timezone.utc))["candidates_waiting"], 0)
        self.assertEqual(self.daily(), dict(day="2026-11-06", candidates=0, calls=0, drafted=0, passed=0))

    def test_cheap_routes_make_every_call_the_waiting_candidates_need_on_cheap_models_only(self):
        self.cheap = {"drafter-model", "reviewer-model"}
        for index in range(13):
            self.recurring("case" + chr(ord("a") + index))
        self.assertEqual(self.daily(), dict(day="2026-11-06", candidates=13, calls=6, drafted=13, passed=3))
        self.assertEqual(self.calls, ["drafter-model", "reviewer-model"] * 3)
        self.assertEqual(len(self.ledger()["drafts"]), 13)

    def test_a_failed_later_chunk_keeps_the_drafts_of_earlier_chunks(self):
        self.cheap = {"drafter-model", "reviewer-model"}
        for index in range(7):
            self.recurring("case" + chr(ord("a") + index))
        def ask(model, prompt, shape, work):
            if len(self.calls) == 2:
                self.calls.append("broken")
                raise subprocess.CalledProcessError(1, "codex")
            return self.ask(model, prompt, shape, work)
        summary = self.daily(ask=ask)
        self.assertEqual((summary["calls"], summary["drafted"], summary.get("error")), (3, 6, "CalledProcessError"))
        self.assertEqual(len(self.ledger()["drafts"]), 6)
        self.assertEqual(self.daily("2026-11-07"), dict(day="2026-11-07", candidates=1, calls=2, drafted=1, passed=1))

    def test_haiku_and_luna_draft_every_run_without_a_daily_cap_or_redrafting(self):
        self.cheap = {"drafter-model", "reviewer-model"}
        self.recurring("a")
        self.assertEqual(self.daily(), dict(day="2026-11-06", candidates=1, calls=2, drafted=1, passed=1))
        self.recurring("b")
        self.assertEqual(self.daily(), dict(day="2026-11-06", candidates=1, calls=2, drafted=1, passed=1))
        self.assertEqual(self.daily(), dict(day="2026-11-06", candidates=0, calls=0, drafted=0, passed=0))
        self.assertEqual(len(self.ledger()["drafts"]), 2)
        self.assertEqual(self.calls, ["drafter-model", "reviewer-model"] * 2)

    def test_a_pricier_model_in_either_role_keeps_the_daily_cap(self):
        self.cheap = {"reviewer-model"}
        self.recurring("a")
        self.assertEqual(self.daily()["calls"], 2)
        self.recurring("b")
        self.assertEqual(self.daily(), dict(day="2026-11-06", skipped="model calls already made today"))
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.ledger()["runs"][-1], dict(day="2026-11-06", skipped="model calls already made today"))

    def test_model_command_drafts_and_reviews_without_cold_claude_or_models_yaml(self):
        for name in "ab":
            self.recurring(name)
        home, empty = self.store.parent / "home", self.store.parent / "empty-path"
        empty.mkdir()
        old_default = home / "projects" / "coldlocker" / "models.yaml"
        old_default.parent.mkdir(parents=True)
        old_default.write_text("bulk: {provider: claudeAgent, model: claude-haiku-5-5, effort: default}\n")
        seen, fake = self.store.parent / "seen.jsonl", self.store.parent / "fake-model"
        fake.write_text("#!" + sys.executable + "\n"
                        "import json, sys\n"
                        "role, shape, prompt = sys.argv[1], json.loads(sys.argv[2]), sys.stdin.read()\n"
                        "body = json.loads(prompt[prompt.index(chr(10)) + 1:])\n"
                        "open(" + repr(str(seen)) + ", 'a').write(json.dumps([role, shape['required']]) + chr(10))\n"
                        "if role == 'drafter':\n"
                        "    print(json.dumps(dict(drafts=[dict(fingerprint=case['fingerprint'], kind='test', cause='c', prevention='p', title='Keep ' + case['headline'],\n"
                        "        rule='Run the tests for ' + case['headline'] + '.', check_command='python3 -m unittest') for case in body])))\n"
                        "else:\n"
                        "    print(json.dumps(dict(reviews=[dict(fingerprint=d['fingerprint'], verdict='PASS', reason='checked') for d in body['drafts']])))\n")
        fake.chmod(0o700)
        environment = {key: value for key, value in learn_daily.os.environ.items() if key != "FLEET_MODELS_FILE"}
        environment.update(HOME=str(home), PATH=str(empty), COLDFIX_MODEL_CMD=str(fake))
        with patch.dict(learn_daily.os.environ, environment, clear=True):
            self.assertEqual(learn_daily.routing(), ({}, set()))
            self.assertEqual(learn_daily.run(self.store, "2026-11-08"),
                             dict(day="2026-11-08", candidates=2, calls=2, drafted=2, passed=2))
            self.assertEqual(learn_daily.run(self.store, "2026-11-08"), dict(day="2026-11-08", candidates=0, calls=0, drafted=0, passed=0))
        self.assertEqual([json.loads(line) for line in seen.read_text().splitlines()], [["drafter", ["drafts"]], ["reviewer", ["reviews"]]])
        self.assertEqual({(row["drafter"], row["reviewer"], row["verdict"]) for row in self.ledger()["drafts"].values()},
                         {("COLDFIX_MODEL_CMD drafter", "COLDFIX_MODEL_CMD reviewer", "PASS")})

    def test_route_accepts_only_the_light_bulk_models_named_in_models_yaml(self):
        models = self.store.parent / "models.yaml"
        models.write_text("# bulk: {provider: x, model: commented, effort: default}\n"
                          "ci: {provider: codex, model: pricey, effort: high}\n"
                          "bulk: {provider: claudeAgent, model: small-claude, effort: default}\n"
                          "bulk-codex: {provider: codex, model: small-codex, effort: default}\n"
                          "fallbacks:\n  bulk: [bulk-codex]\n"
                          "tiers:\n  builder: [ci]\n  light: [bulk, bulk-codex]\n")
        def resolve(model, provider="codex"):
            reply = subprocess.CompletedProcess([], 0, stdout=json.dumps(dict(provider=provider, model=model)))
            with patch("learn_daily.subprocess.run", return_value=reply):
                return learn_daily.route("bulk")
        with patch.dict(learn_daily.os.environ, FLEET_MODELS_FILE=str(models), COLDFIX_FLEET_CLI="acme"):
            try:
                self.assertEqual(resolve("small-claude", "claudeAgent")["model"], "small-claude")
            except c.ColdfixError as error:
                self.fail("the models.yaml bulk model was refused: " + str(error))
            original = models.read_text()
            models.write_text(original.replace("model: small-claude", "model: pricey"))
            with self.assertRaises(c.ColdfixError):
                resolve("pricey", "claudeAgent")
            models.write_text(original.replace("model: small-claude", "model: shared") +
                              "coordinator: {model: shared, provider: codex, effort: high}\n")
            with self.assertRaises(c.ColdfixError):
                resolve("shared", "claudeAgent")
            models.write_text(original.replace("model: small-claude", "model: shared") +
                              "coordinator:\n  provider: codex\n  model: shared\n")
            with self.assertRaises(c.ColdfixError):
                resolve("shared", "claudeAgent")
            models.write_text(original.replace("model: small-claude", "model: shared") +
                              "coordinator: {model: &top shared, provider: codex}\n")
            with self.assertRaises(c.ColdfixError):
                resolve("shared", "claudeAgent")
            models.write_text(original.replace("light: [bulk,", "light: [*alias, bulk,"))
            with self.assertRaises(c.ColdfixError):
                resolve("small-claude", "claudeAgent")
            models.write_text(original.replace("model: small-claude", "model: shared") +
                              "coordinator: {model: shared, provider: codex, model: other, effort: high}\n")
            with self.assertRaises(c.ColdfixError):
                resolve("shared", "claudeAgent")
            models.write_text(original.replace("model: small-claude", "model: shared") +
                              "coordinator: {model: shared, provider: codex, effort: high}\n")
            with self.assertRaises(c.ColdfixError):
                resolve("shared", "claudeAgent")
            models.write_text(original + "broken: {provider: codex\n")
            with self.assertRaises(c.ColdfixError):
                resolve("small-claude", "claudeAgent")
            models.write_text(original)
            models.write_text(models.read_text().replace("  light: [bulk, bulk-codex]\n", ""))
            with self.assertRaises(c.ColdfixError):
                resolve("small-codex")

    def test_claude_route_returns_the_structured_reply(self):
        work = self.store.parent
        reply = json.dumps(dict(is_error=False, structured_output=dict(drafts=[])))
        model = dict(provider="claudeAgent", model="claude-haiku-5-5", effort="default")
        with patch("learn_daily.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout=reply)) as run:
            try:
                answer = learn_daily.ask(model, "prompt", learn_daily.DRAFTS, work)
            except OSError as error:
                self.fail("the Claude route was not used: " + str(error))
        self.assertEqual(answer, dict(drafts=[]))
        command = run.call_args.args[0]
        self.assertEqual(command[:4], ["claude", "-p", "--model", "claude-haiku-5-5"])
        self.assertEqual(json.loads(command[command.index("--json-schema") + 1]), learn_daily.DRAFTS)
        failed = json.dumps(dict(is_error=True, result="Not logged in"))
        with patch("learn_daily.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout=failed)):
            with self.assertRaisesRegex(c.ColdfixError, "no structured output"):
                learn_daily.ask(model, "prompt", learn_daily.DRAFTS, work)

    def test_balanced_routes_on_one_model_fall_back_to_the_primary_bulk_models(self):
        self.recurring("a")
        self.cheap = {"drafter-model", "reviewer-model"}
        self.route = lambda *args: SOL
        models = self.store.parent / "models.yaml"
        models.write_text("bulk: {provider: claudeAgent, model: drafter-model, effort: default}\n"
                          "bulk-codex: {provider: codex, model: reviewer-model, effort: default}\n"
                          "tiers:\n  light: [bulk, bulk-codex]\n")
        def availability(blocked, luna_blocked=False):
            reply = json.dumps([dict(provider="claudeAgent", model="Provider/Drafter_Model" if blocked else None, blocked=blocked),
                                dict(provider="codex", model="pricey", blocked=True),
                                dict(provider="codex", model="reviewer-model", blocked=luna_blocked)])
            return patch("learn_daily.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout=reply))
        with patch.dict(learn_daily.os.environ, FLEET_MODELS_FILE=str(models), COLDFIX_FLEET_CLI="acme"):
            with availability(True, luna_blocked=True) as observed:
                self.assertEqual(self.daily()["error"], "ColdfixError: Luna is unavailable")
            observed.assert_called_once()
            self.assertEqual(self.calls, [])
            with availability(True):
                self.assertEqual(self.daily(), dict(day="2026-11-06", candidates=1, calls=2, drafted=1, passed=1,
                                                    fallbacks=["Claude unavailable; drafting and reviewing on reviewer-model"]))
            self.assertEqual(self.calls, ["reviewer-model"] * 2)
            self.recurring("b")
            with availability(False):
                self.assertEqual(self.daily(), dict(day="2026-11-06", candidates=1, calls=2, drafted=1, passed=1))
            self.recurring("c")
            self.route = lambda *args: TERRA if args == ("bulk-codex",) else SOL
            self.assertEqual(self.daily()["calls"], 2)
        self.assertEqual(self.calls, ["reviewer-model"] * 2 + ["drafter-model", "reviewer-model"] * 2)

    def haiku_and_luna(self):
        haiku = dict(provider="claudeAgent", model="claude-haiku-5-5", effort="default")
        luna = dict(provider="codex", model="gpt-6-luna", effort="default")
        models = self.store.parent / "models.yaml"
        models.write_text("bulk: {provider: claudeAgent, model: claude-haiku-5-5, effort: default}\n"
                          "bulk-codex: {provider: codex, model: gpt-6-luna, effort: default}\n"
                          "tiers:\n  light: [bulk, bulk-codex]\n")
        self.cheap = {"claude-haiku-5-5", "gpt-6-luna"}
        self.route = lambda *args: haiku if args == ("bulk",) else luna
        return patch.dict(learn_daily.os.environ, FLEET_MODELS_FILE=str(models))

    def test_a_missing_or_malformed_claude_reply_is_retried_on_luna(self):
        for index, (broken, reason) in enumerate((({}, "no drafts in the reply"), (dict(drafts=[{}]), "malformed drafts from Claude"))):
            def bad_claude(model, *args):
                if model["provider"] == "claudeAgent":
                    self.calls.append(model["model"])
                    return broken
                return self.ask(model, *args)
            with self.subTest(reply=broken), self.haiku_and_luna():
                self.recurring("ab"[index])
                self.calls = []
                summary = self.daily(f"2026-11-0{6 + index}", ask=bad_claude)
                self.assertEqual((summary["calls"], summary["passed"], summary["fallbacks"]), (4, 1, [
                    f"drafter claude-haiku-5-5 failed (ColdfixError: {reason}); retried on gpt-6-luna",
                    "reviewer claude-haiku-5-5 failed (ColdfixError: no reviews in the reply); retried on gpt-6-luna"]))
                self.assertEqual(self.calls, ["claude-haiku-5-5", "gpt-6-luna", "claude-haiku-5-5", "gpt-6-luna"])

    def test_backlog_drafts_moved_to_luna_are_reviewed_by_haiku_and_a_refusal_keeps_its_reason(self):
        import ci_batch

        def claude_drafts_badly(model, prompt, shape, work):
            if model["provider"] == "claudeAgent" and shape is learn_daily.DRAFTS:
                self.calls.append(model["model"])
                return {}
            return self.ask(model, prompt, shape, work)

        def claude_down(model, prompt, shape, work):
            if model["provider"] == "claudeAgent":
                self.calls.append(model["model"])
                return {}
            return self.ask(model, prompt, shape, work)

        with self.haiku_and_luna():
            self.recurring("a")
            summary = self.daily(ask=ci_batch.cheap_only(claude_drafts_badly))
            self.assertEqual((summary.get("error"), summary["calls"], summary["passed"]), (None, 3, 1))
            self.assertEqual(self.calls, ["claude-haiku-5-5", "gpt-6-luna", "claude-haiku-5-5"])
            self.assertEqual({(row["drafter"], row["reviewer"]) for row in self.ledger()["drafts"].values()},
                             {("gpt-6-luna", "claude-haiku-5-5")})
            self.recurring("b")
            refused = self.daily("2026-11-07", ask=ci_batch.cheap_only(claude_down))
        self.assertEqual(refused["error"], "ColdfixError: backlog reviewer must differ from the drafter after fallback")
        self.assertEqual(refused["fallbacks"], ["drafter claude-haiku-5-5 failed (ColdfixError: no drafts in the reply); retried on gpt-6-luna",
                                                "reviewer claude-haiku-5-5 failed (ColdfixError: no reviews in the reply); retried on gpt-6-luna"])

    def test_a_failed_claude_call_runs_on_luna_and_a_failed_luna_call_raises_the_stall_alert(self):
        import test_learn_stall as stall

        def claude_down(model, *args):
            if model["provider"] == "claudeAgent":
                self.calls.append(model["model"])
                raise subprocess.CalledProcessError(1, "claude", stderr="usage limit reached")
            return self.ask(model, *args)

        def all_down(model, *args):
            self.calls.append(model["model"])
            raise subprocess.CalledProcessError(1, model["provider"])

        def alerts(*summaries):
            lines = [stall.entry(30, "init.scope", "Started session")]
            lines += [stall.entry(len(summaries) - index, stall.DAILY, row) for index, row in enumerate(summaries)]
            return subprocess.run([sys.executable, str(stall.SCRIPT), "--state", str(self.store.parent / f"{len(self.calls)}.json")],
                                  input="\n".join(lines) + "\n", text=True, capture_output=True, check=True).stdout

        self.recurring("a")
        with self.haiku_and_luna():
            fell_back = self.daily(ask=claude_down)
            self.assertEqual(fell_back, dict(day="2026-11-06", candidates=1, calls=4, drafted=1, passed=1, fallbacks=[
                "drafter claude-haiku-5-5 failed (CalledProcessError); retried on gpt-6-luna",
                "reviewer claude-haiku-5-5 failed (CalledProcessError); retried on gpt-6-luna"]))
            self.assertEqual(self.calls, ["claude-haiku-5-5", "gpt-6-luna", "claude-haiku-5-5", "gpt-6-luna"])
            self.assertEqual({row["drafter"] for row in self.ledger()["drafts"].values()}, {"gpt-6-luna"})
            self.assertNotIn("learn_daily failed", alerts(fell_back, fell_back, fell_back))
            self.recurring("b")
            failed = [self.daily(day, ask=all_down) for day in ("2026-11-07", "2026-11-08", "2026-11-09")]
        self.assertEqual([row["error"] for row in failed], ["CalledProcessError"] * 3)
        self.assertEqual(self.calls[4:], ["claude-haiku-5-5", "gpt-6-luna"] * 3)
        notes = alerts(fell_back, *failed)
        self.assertIn(f"STALLED coldfix on {platform.node()} ", notes)
        self.assertIn("learn_daily failed in 3 daily runs in a row", notes)
        self.assertIn("0 lessons proven in 24 h", notes)

    def test_reviewer_matching_the_drafter_is_refused_before_any_model_call(self):
        self.recurring("a")
        self.route = lambda *args: TERRA
        self.assertEqual(self.daily()["error"], "ColdfixError: models.yaml lacks the bulk roles")
        self.assertEqual(self.calls, [])

    def test_real_route_rejects_a_model_above_the_drafting_budget(self):
        result = subprocess.CompletedProcess([], 0, stdout=json.dumps(dict(provider="codex", model="gpt-6.1-sol")))
        with patch("learn_daily.subprocess.run", return_value=result), patch.dict(os.environ, COLDFIX_FLEET_CLI="acme"):
            with self.assertRaisesRegex(c.ColdfixError, "light bulk model"):
                learn_daily.route("ci")

    def test_failed_draft_call_is_recorded_and_retried_the_next_day(self):
        self.recurring("a")
        def broken(*args):
            self.calls.append("broken")
            raise subprocess.CalledProcessError(1, "codex")
        summary = self.daily(ask=broken)
        self.assertEqual((summary["calls"], summary["error"]), (1, "CalledProcessError"))
        self.assertEqual(self.ledger()["drafts"], {})
        self.assertEqual(self.daily("2026-11-07")["passed"], 1)

    def test_a_run_killed_after_a_model_call_still_uses_up_the_day(self):
        self.recurring("a")
        def killed(*args):
            self.calls.append("killed")
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.daily(ask=killed)
        self.assertEqual(self.daily(), dict(day="2026-11-06", skipped="model calls already made today"))
        self.assertEqual(self.calls, ["killed"])
        self.assertEqual((self.ledger()["runs"][0]["error"], self.ledger()["runs"][0]["calls"]), ("interrupted", 1))
        self.assertEqual(self.daily("2026-11-07")["passed"], 1)

    def test_excerpts_show_the_log_even_when_later_metadata_is_long(self):
        record = dict(command="verify", error=json.dumps(dict(log_tail="AssertionError: real cause", zz="x" * 5000)))
        self.assertEqual(learn_daily.excerpt(record), "AssertionError: real cause")
        self.assertEqual(learn_daily.excerpt(dict(command="verify", error="plain text")), "plain text")

    def test_excerpts_start_at_the_failing_test_rather_than_the_job_cleanup_after_it(self):
        log = ("setup\n" * 50 + "verify\tRun tests\t2026-11-06T19:31:00Z FAIL: test_sync (test_mirror.MirrorTests)\n"
               + "AssertionError: 1 != 2\n" + "[command]/usr/bin/git config --local --unset-all\n" * 200)
        shown = learn_daily.excerpt(dict(command="verify", error=json.dumps(dict(log_tail=log))))
        self.assertIn("FAIL: test_sync (test_mirror.MirrorTests)", shown)
        self.assertIn("AssertionError: 1 != 2", shown)
        self.assertLessEqual(len(shown), learn_daily.EXCERPT_CHARS)

    def test_a_rule_already_proposed_active_or_passed_is_recorded_as_a_duplicate_and_never_reviewed(self):
        for name in "abc":
            self.recurring(name)
        reviewed = []
        def ask(model, prompt, shape, work):
            self.calls.append(model["model"])
            cases = json.loads(prompt[prompt.index("\n") + 1:])
            if shape is learn_daily.DRAFTS:
                rules = ["Use a fresh destination.", "use a  FRESH destination.", "Retired rule."]
                return dict(drafts=[dict(fingerprint=case["fingerprint"], kind="test", cause="c", prevention="p", title="t", rule=rule,
                                         check_command="python3 -m unittest") for case, rule in zip(cases, rules)])
            reviewed.extend(draft["rule"] for draft in cases["drafts"])
            return dict(reviews=[dict(fingerprint=draft["fingerprint"], verdict="PASS", reason="checked")
                                 for draft in cases["drafts"]])
        lessons = dict(one=dict(lesson_id="L1", rule="Use a fresh   destination.", status="proposed"),
                       two=dict(lesson_id="L2", rule="Retired rule.", status="retired"))
        with patch.object(learn_daily.c, "latest_lessons", return_value=lessons):
            summary = self.daily(ask=ask)
        self.assertEqual((summary["drafted"], summary["passed"]), (3, 1))
        self.assertEqual(reviewed, ["Retired rule."])
        verdicts = sorted((row["verdict"], row["reason"]) for row in self.ledger()["drafts"].values())
        self.assertEqual(verdicts, [("FAIL", "duplicate of lesson L1"), ("FAIL", "duplicate of lesson L1"), ("PASS", "checked")])
        self.recurring("d")
        def again(model, prompt, shape, work):
            self.calls.append(model["model"])
            cases = json.loads(prompt[prompt.index("\n") + 1:])
            if shape is learn_daily.DRAFTS:
                return dict(drafts=[dict(fingerprint=case["fingerprint"], kind="test", cause="c", prevention="p", title="t", rule="retired RULE.",
                                         check_command="python3 -m unittest") for case in cases])
            self.fail("a duplicate of a passing draft must not be reviewed")
        with patch.object(learn_daily.c, "latest_lessons", return_value={}):
            summary = self.daily("2026-11-07", ask=again)
        self.assertEqual((summary["candidates"], summary["passed"]), (1, 0))
        reasons = {row["reason"] for row in self.ledger()["drafts"].values()}
        self.assertEqual(len([r for r in reasons if r.startswith("duplicate of draft ")]), 1)
        with patch.object(learn_daily.c, "latest_lessons", return_value={}):
            self.assertEqual(self.daily("2026-11-08")["candidates"], 0)

    def test_a_rule_reserved_by_the_live_store_is_not_reviewed(self):
        self.recurring("a")
        def ask(model, prompt, shape, work):
            self.assertIs(shape, learn_daily.DRAFTS)
            cases = json.loads(prompt[prompt.index("\n") + 1:])
            return dict(drafts=[dict(fingerprint=case["fingerprint"], kind="test", cause="c", prevention="p", title="t",
                                     rule="Existing LIVE rule.", check_command="python3 -m unittest")
                                for case in cases])
        summary = learn_daily.run(self.store, "2026-11-07", route=self.route, ask=ask,
                                  reserved_rules={"existing live rule.": "lesson live"})
        self.assertEqual(summary["passed"], 0)
        self.assertEqual([row["reason"] for row in self.ledger()["drafts"].values()],
                         ["duplicate of lesson live"])

    def test_drafts_sharing_a_rule_in_one_reply_are_reviewed_and_only_a_later_pass_is_a_duplicate(self):
        for name in "abc":
            self.recurring(name)
        def ask(model, prompt, shape, work):
            cases = json.loads(prompt[prompt.index("\n") + 1:])
            if shape is learn_daily.DRAFTS:
                return dict(drafts=[dict(fingerprint=case["fingerprint"], kind="test", cause="c", prevention="p", title="t", rule="Same rule.",
                                         check_command="python3 -m unittest") for case in cases])
            return dict(reviews=[dict(fingerprint=draft["fingerprint"], verdict="FAIL" if index == 0 else "PASS",
                                      reason="checked") for index, draft in enumerate(cases["drafts"])])
        self.assertEqual(self.daily(ask=ask)["passed"], 1)
        reasons = sorted((row["verdict"], row["reason"].split(" ")[0]) for row in self.ledger()["drafts"].values())
        self.assertEqual(reasons, [("FAIL", "checked"), ("FAIL", "duplicate"), ("PASS", "checked")])

    def test_invalid_or_unknown_drafts_are_not_reviewed(self):
        self.recurring("a")
        def ask(model, prompt, shape, work):
            self.calls.append(model["model"])
            return dict(drafts=[dict(fingerprint="unknown", kind="test", cause="c", prevention="p", title="t", rule="r", check_command="x")])
        summary = self.daily(ask=ask)
        self.assertEqual((summary["calls"], summary["drafted"]), (1, 0))
        self.assertEqual(self.ledger()["drafts"], {})
        self.assertEqual(self.daily("2026-11-07")["passed"], 1)

    def test_a_draft_keeps_its_one_line_cause_and_prevention_and_a_multiline_or_missing_one_is_dropped(self):
        self.recurring("alpha")
        self.recurring("multi")
        self.recurring("bare")
        self.assertIn("cause", learn_daily.DRAFTS["properties"]["drafts"]["items"]["required"])

        def ask(model, prompt, shape, work):
            if shape is not learn_daily.DRAFTS:
                return self.ask(model, prompt, shape, work)
            self.assertIn("one-line cause", prompt)
            cases = json.loads(prompt[prompt.index("\n") + 1:])
            return dict(drafts=[dict(fingerprint=case["fingerprint"], kind="test", title="Keep " + case["headline"],
                                     cause="Why " + case["headline"] + ("\nbroke" if "multi" in case["headline"] else ""),
                                     rule="Run the tests for " + case["headline"] + ".", check_command="python3 -m unittest",
                                     **({} if "bare" in case["headline"] else dict(prevention="Check " + case["headline"] + " first.")))
                                for case in cases])
        self.assertEqual(self.daily(ask=ask)["drafted"], 1)
        draft, = [row["draft"] for row in self.ledger()["drafts"].values()]
        self.assertEqual((draft["cause"].startswith("Why "), draft["prevention"].startswith("Check ")), (True, True))

    def test_drafts_the_reviewer_skips_are_retried_the_next_day(self):
        self.recurring("a")
        self.recurring("b")
        def ask(model, prompt, shape, work):
            reply = self.ask(model, prompt, shape, work)
            return reply if shape is learn_daily.DRAFTS else dict(reviews=reply["reviews"][:1])
        summary = self.daily(ask=ask)
        self.assertEqual((summary["calls"], summary["drafted"], summary["passed"]), (2, 2, 1))
        self.assertEqual(len(self.ledger()["drafts"]), 1)
        self.assertEqual(self.daily("2026-11-07")["candidates"], 1)
        self.assertEqual(len(self.ledger()["drafts"]), 2)

    def test_cli_holds_a_lock_and_exits_zero_without_candidates(self):
        result = subprocess.run(["python3", str(ROOT / "learn_daily.py"), "--store", str(self.store)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["calls"], 0)

    def test_daily_replays_transcript_code_leads_and_reports_their_counts(self):
        output = io.StringIO()
        with patch.object(learn_daily.c, 'on_replay_host', return_value=True), \
                patch('codex_review_source.resume', return_value=({'counts': {}}, self.store)), \
                patch('learn_replay.transcript_run', return_value=dict(routed=2, proven=1, rejected=1)) as run, \
                redirect_stdout(output):
            code = learn_daily.main(['--store', str(self.store)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())['transcript_replay'], dict(routed=2, proven=1, rejected=1))
        self.assertEqual(run.call_args.args[0], self.store)

    def test_replay_delivered_counts_check_deliveries(self):
        (self.store / learn_daily.LEDGER).write_text(json.dumps(dict(runs=[], drafts={"a" * 32: dict(
            day="2026-11-06", verdict="PASS", replay=dict(status="rejected"))})))
        (self.store / "check-deliveries.json").write_text(json.dumps(dict(
            x=dict(state="delivered"), y=dict(state="delivered"), z=dict(state="waiting"))))
        output = io.StringIO()
        with patch.object(learn_daily.c, 'on_replay_host', return_value=True), \
                patch('codex_review_source.resume', return_value=({'counts': {}}, self.store)), \
                patch('learn_replay.run', return_value=dict(proven=0, rejected=0)), redirect_stdout(output):
            learn_daily.main(['--store', str(self.store)])
        summary = json.loads(output.getvalue())
        self.assertEqual(summary['replay']['delivered'], 2)
        self.assertNotIn('spread', summary)

    def test_hourly_replay_line_counts_only_this_runs_rejection_reasons_by_bucket(self):
        failure = "f" * 32
        old = dict(day="2026-11-06", reason="source repository checkout unavailable")
        (self.store / learn_daily.LEDGER).write_text(json.dumps(dict(runs=[], drafts={"a" * 32: dict(
            day="2026-11-06", verdict="PASS", replay=dict(status="rejected"), replay_attempts=[old])})))
        def replay(root, ledger, projects=None):
            ledger["drafts"]["a" * 32]["replay_attempts"] += [
                dict(day="2026-11-07", reason=f"{failure}: recorded failure has no commit; "
                                              f"{failure}: local privacy Git operation failed"),
                dict(day="2026-11-07", reason='incident cannot be exported; privacy screen blocked: {"hostname": 4}')]
            return dict(proven=0, rejected=2)
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(learn_daily.c, 'on_replay_host', return_value=True), \
                patch('codex_review_source.resume', return_value=({'counts': {}}, self.store)), \
                patch('learn_replay.run', side_effect=replay), \
                redirect_stdout(output), patch.object(sys, 'stderr', errors):
            learn_daily.main(['--store', str(self.store)])
        line = next(row for row in errors.getvalue().splitlines() if row.startswith("learn_daily replay "))
        self.assertEqual(json.loads(line[len("learn_daily replay "):]), dict(proven=0, delivered=0, rejected=2, reasons=dict(
            ours={"local privacy Git operation failed": 1},
            not_provable={"recorded failure has no commit": 1},
            privacy_screen={"privacy screen: hostname": 1})))

    def test_replay_line_splits_out_review_drafts_and_skips_the_frozen_snapshot_replay(self):
        def review(number):
            row = c.event_record(dict(kind="review", command="PR review finding", error=f"Finding {number}",
                                      repo="example/project", agent="codex-review", occurred="2026-10-20T12:00:00Z",
                                      source_ref=f"https://github.com/example/project/pull/{number}#discussion_r{number}"),
                                 "github")
            c.save(self.store, "failures", row)
            return row["id"]
        drafts = {key: dict(day="2026-11-06", verdict="PASS", evidence=[ref])
                  for key, ref in (("a" * 32, review(1)), ("b" * 32, review(2)), ("c" * 32, review(3)))}
        drafts["d" * 32] = dict(day="2026-11-06", verdict="PASS", evidence=["f" * 32])
        drafts["e" * 32] = dict(day="2026-11-06", verdict="PASS", evidence=[review(4)])
        drafts["f" * 32] = dict(day="2026-11-06", verdict="PASS", evidence=[review(5)],
                                replay=dict(status="rejected", reason="replay budget exhausted"))
        (self.store / learn_daily.LEDGER).write_text(json.dumps(dict(runs=[], drafts=drafts)))
        def replay(root, ledger, projects=None):
            rows = ledger["drafts"]
            rows["a" * 32]["replay"] = dict(status="proven", proof=dict(receipt_id="r" * 32))
            rows["b" * 32]["replay"] = dict(status="rejected", reason="no changed unittest matches the recorded failure")
            rows["b" * 32]["replay_attempts"] = [dict(day="2026-11-07", reason="no changed unittest matches the recorded failure")]
            rows["d" * 32]["replay"] = dict(status="proven", proof=dict(receipt_id="r" * 32))
            rows["c" * 32]["replay_attempts"] = [dict(day="2026-11-07", reason="replay budget exhausted")] * 2
            rows["c" * 32]["replay_resume"] = dict(keys=[])
            rows["e" * 32]["replay"] = dict(status="rejected", reason="no fix exists: FLAGGED_HUNK_UNCHANGED")
            rows["f" * 32]["replay_attempts"] = [dict(day="2026-11-07", reason="no changed unittest matches the recorded failure")]
            rows["f" * 32]["replay_resume"] = dict(keys=[])
            return dict(proven=2, rejected=3, deferred=1)
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(learn_daily.c, 'on_replay_host', return_value=True), \
                patch('codex_review_source.resume', return_value=({'counts': {}}, self.store)) as snapshot, \
                patch('learn_replay.run', side_effect=replay), patch('learn_replay.transcript_run', return_value={}), \
                redirect_stdout(output), patch.object(sys, 'stderr', errors):
            code = learn_daily.main(['--store', str(self.store)])
        self.assertEqual(code, 0, output.getvalue())
        snapshot.assert_not_called()
        line = next(row for row in errors.getvalue().split("\n") if row.startswith("learn_daily replay "))
        self.assertEqual(json.loads(line[len("learn_daily replay "):])["review"], dict(proven=1, rejected=2, waiting=2))


class ReplayHostTests(unittest.TestCase):
    def test_replay_runs_only_on_the_configured_host(self):
        with patch.object(c.platform, 'node', return_value='replay-box'):
            with patch.dict('os.environ', {'COLDFIX_REPLAY_HOST': 'replay-box'}):
                self.assertTrue(c.on_replay_host())
            with patch.dict('os.environ', {'COLDFIX_REPLAY_HOST': 'other-box'}):
                self.assertFalse(c.on_replay_host())
            with patch.dict('os.environ', {}, clear=True):
                self.assertFalse(c.on_replay_host())
        with patch.object(c.platform, 'node', return_value=''), patch.dict('os.environ', {}, clear=True):
            self.assertFalse(c.on_replay_host())


if __name__ == "__main__":
    unittest.main()
