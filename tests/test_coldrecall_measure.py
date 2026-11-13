import contextlib
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import coldrecall_measure as cm
import repo_checkout
import test_coldrecall as base

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/coldrecall_measure"
MODEL = dict(role="builder-fast", provider="claudeAgent", model="claude-test", effort="high")


def edit(id_, path, old, new, name="Edit"):
    arguments = dict(file_path=path, old_string=old, new_string=new) if name == "Edit" else dict(file_path=path, content=new)
    return json.dumps(dict(type="assistant", message=dict(id="m" + id_, content=[dict(type="tool_use", id=id_, name=name, input=arguments)],
                                                         usage=dict(input_tokens=1, output_tokens=2))))


def result(id_, error=False):
    return json.dumps(dict(type="user", message=dict(content=[dict(type="tool_result", tool_use_id=id_, is_error=error)])))


def final(**fields):
    return json.dumps(dict(type="result", **fields))


class AnalyzeTests(unittest.TestCase):
    def analyze(self, *lines):
        return cm.analyze("\n".join(lines))

    def test_failed_edits_are_counted_and_do_not_count_as_rework(self):
        out = self.analyze(edit("a", "f.py", "x", "y"), result("a", error=True), edit("b", "f.py", "x", "y"), result("b"))
        self.assertEqual((out["edits_attempted"], out["edits_failed"], out["re_edits"], out["reverts"]), (2, 1, 0, 0))

    def test_editing_text_an_earlier_edit_wrote_is_a_re_edit_and_an_exact_undo_is_a_revert(self):
        out = self.analyze(edit("a", "f.py", "one", "two"), result("a"), edit("b", "f.py", "two", "three"), result("b"),
                           edit("c", "f.py", "three", "two"), result("c"), edit("d", "other.py", "two", "x"), result("d"))
        self.assertEqual((out["re_edits"], out["reverts"]), (2, 1))

    def test_write_to_an_already_edited_file_is_a_re_edit_but_a_first_write_is_not(self):
        first = self.analyze(edit("a", "f.py", None, "body", name="Write"), result("a"))
        again = self.analyze(edit("a", "f.py", "x", "y"), result("a"), edit("b", "f.py", None, "body", name="Write"), result("b"))
        self.assertEqual((first["re_edits"], again["re_edits"]), (0, 1))

    def test_an_edit_request_without_a_tool_result_is_not_rework(self):
        out = self.analyze(edit("a", "f.py", "one", "two"), result("a"), edit("b", "f.py", "two", "one"))
        self.assertEqual((out["edits_attempted"], out["re_edits"], out["reverts"]), (2, 0, 0))

    def test_tokens_come_from_the_result_event_and_fall_back_to_message_usage(self):
        full = self.analyze(edit("a", "f.py", "x", "y"), final(usage=dict(input_tokens=100, output_tokens=20, cache_creation_input_tokens=5, cache_read_input_tokens=30),
                                                              total_cost_usd=0.25, num_turns=4))
        cut = self.analyze(edit("a", "f.py", "x", "y"), edit("b", "f.py", "y", "z"))
        self.assertEqual((full["tokens"]["total"], full["tokens"]["source"], full["cost_usd"], full["turns"]), (155, "result", 0.25, 4))
        self.assertEqual((cut["tokens"]["total"], cut["tokens"]["source"], cut["cost_usd"]), (6, "messages", None))

    def test_unicode_separators_inside_a_json_string_do_not_split_a_record(self):
        out = self.analyze(edit("a", "f.py", "x", "line para next\u0085end"), result("a"))
        self.assertEqual((out["edits_attempted"], out["unparsed_lines"]), (1, 0))

    def test_garbage_lines_are_counted_and_reflex_lines_need_a_hook_event_with_a_fact(self):
        hook = json.dumps(dict(type="system", subtype="hook_response", output="[coldrecall] REGRESSION"))
        mention = json.dumps(dict(type="assistant", message=dict(content=[dict(type="text", text="[coldrecall]")])))
        out = self.analyze("not json", "[1]", hook, mention, "")
        self.assertEqual((out["unparsed_lines"], out["reflex_lines"]), (2, 1))


class ConfigTests(unittest.TestCase):
    def config(self, **changes):
        config = json.loads((FIXTURES / "config.json").read_text())
        config.update(changes)
        return config

    def load(self, config):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(config))
            return cm.load_config(path)

    def test_shipped_config_is_valid(self):
        self.assertEqual(self.load(self.config())["model_role"], "builder-fast")

    def test_invalid_configs_are_rejected(self):
        task = self.config()["tasks"][0]
        bad = dict(
            repetitions=self.config(repetitions=0), timeout=self.config(session_timeout_seconds=-1), command=self.config(agent_command=[]),
            extra=dict(self.config(), surprise=1), duplicate=self.config(tasks=[task, task]), id=self.config(tasks=[dict(task, id="Bad Id")]),
            escape=self.config(tasks=[dict(task, accept=dict(task["accept"], copy={"../x": "y"}))]), empty=self.config(tasks=[]))
        for name, config in bad.items():
            with self.subTest(name), self.assertRaises(cm.c.ColdfixError):
                self.load(config)


class ReportTests(unittest.TestCase):
    def session(self, task, arm, tokens, accepted=True, re_edits=0, cost=None):
        return dict(id=f"{task}.{arm}.1", task=task, arm=arm, repetition=1, exit=0, timed_out=False, accepted=accepted, changed_files=[], image=None,
                    tokens=dict(total=tokens), cost_usd=cost, turns=1, edits_attempted=2, edits_failed=1, re_edits=re_edits, reverts=0, reflex_lines=0, unparsed_lines=0)

    def test_summarize_means_and_missing_values(self):
        rows = [self.session("t", "without", 100, cost=1.0), self.session("t", "without", 300, accepted=False, re_edits=2)]
        out = cm.summarize(rows)
        self.assertEqual((out["sessions"], out["accepted"]), (2, 1))
        self.assertEqual(out["tokens"], dict(sum=400, mean=200, missing=0))
        self.assertEqual(out["cost_usd"], dict(sum=1.0, mean=1.0, missing=1))
        self.assertEqual(out["re_edits"]["mean"], 1)
        self.assertEqual(cm.summarize([])["tokens"]["mean"], None)

    def test_report_round_trips_through_the_cli_and_names_every_arm_and_task(self):
        with tempfile.TemporaryDirectory() as directory:
            work, out = Path(directory) / "work", Path(directory)
            (work / "sessions").mkdir(parents=True)
            config = json.loads((FIXTURES / "config.json").read_text())
            (work / "meta.json").write_text(json.dumps(dict(version=1, config=config, model=MODEL, source_head="f" * 40)))
            for task in config["tasks"]:
                for arm in cm.ARMS:
                    (work / "sessions" / f"{task['id']}.{arm}.1.json").write_text(json.dumps(self.session(task["id"], arm, 1000 + len(arm))))
            code = cm.main(["report", "--work", str(work), "--json", str(out / "r.json"), "--md", str(out / "r.md")])
            data, text = json.loads((out / "r.json").read_text()), (out / "r.md").read_text()
        self.assertEqual(code, 0)
        self.assertEqual(data["arms"]["wiki"]["sessions"], len(config["tasks"]))
        for word in (*cm.ARMS, *(t["id"] for t in config["tasks"]), "claude-test", "## Limits", "Bash"):
            self.assertIn(word, text)

    def test_cell_keeps_integer_zeros_and_trims_fraction_zeros(self):
        self.assertEqual([cm.cell(v) for v in (111190.0, 100.0, 0.5, 2.0, None, 7)], ["111190", "100", "0.5", "2", "n/a", "7"])

    def test_report_without_sessions_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "sessions").mkdir()
            (Path(directory) / "meta.json").write_text(json.dumps(dict(version=1, config=dict(tasks=[]), model=MODEL, source_head="f" * 40)))
            self.assertEqual(cm.main(["report", "--work", directory, "--json", directory + "/j", "--md", directory + "/m"]), 2)


class WikiFixtureTests(unittest.TestCase):
    def test_overview_lists_files_and_signatures(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = repo_checkout.checkout(Path(temporary) / "repo")
            done = subprocess.run([sys.executable, str(FIXTURES / "wiki_fixture.py"), str(repo)], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("- `coldrecall.py`", done.stdout)
        self.assertIn("def render(atom, head)", done.stdout)


class ArmWiringTests(base.ImageCase):
    def setUp(self):
        super().setUp()
        self.out = tempfile.TemporaryDirectory()
        self.addCleanup(self.out.cleanup)
        self.work = Path(self.out.name) / "work"
        config = json.loads((FIXTURES / "config.json").read_text())
        config.update(repetitions=1, agent_command=[sys.executable, str(FIXTURES / "fake_agent.py")],
                      wiki_command=[sys.executable, str(FIXTURES / "wiki_fixture.py")],
                      tasks=[dict(id="guard", base=self.before, prompt="Fix guarded.py.", accept=dict(copy={}, commands=[["python3", "check.py"]]))])
        self.config = Path(self.out.name) / "config.json"
        self.config.write_text(json.dumps(config))

    def measure(self, *extra):
        with mock.patch.object(cm, "resolve_model", return_value=MODEL):
            return cm.main(["run", "--config", str(self.config), "--source", str(self.repo), "--work", str(self.work), *extra])

    def session(self, arm):
        return json.loads((self.work / "sessions" / f"guard.{arm}.1.json").read_text())

    def argv(self, arm):
        lines = (self.work / "transcripts" / f"guard.{arm}.1.jsonl").read_text().split("\n")
        return json.loads(lines[0])["argv"]

    def test_each_arm_gets_only_its_own_context_and_every_session_is_scored(self):
        self.assertEqual(self.measure(), 0)
        records = {arm: self.session(arm) for arm in cm.ARMS}
        for arm, record in records.items():
            with self.subTest(arm):
                self.assertEqual((record["accepted"], record["edits_attempted"], record["edits_failed"], record["tokens"]["total"]), (True, 2, 1, 150))
        self.assertEqual([r["reflex_lines"] for r in records.values()], [0, 1, 0])
        self.assertEqual(records["coldrecall"]["image"]["active"], 1)
        self.assertIsNone(records["without"]["image"])
        self.assertIn("Guarded value must stay good.", (self.work / "transcripts/guard.coldrecall.1.jsonl").read_text())
        self.assertTrue(all("--append-system-prompt" not in self.argv(arm) for arm in ("without", "coldrecall")))
        prompt = self.argv("wiki")[self.argv("wiki").index("--append-system-prompt") + 1]
        self.assertIn("# Repository wiki", prompt)
        self.assertIn("claude-test", self.argv("without"))
        self.assertFalse((self.work / "workspaces/guard.coldrecall.1/.coldlocker").exists())

    def test_finished_sessions_are_skipped_and_a_changed_run_needs_force(self):
        self.assertEqual(self.measure("--arm", "without"), 0)
        marker = self.work / "sessions/guard.without.1.json"
        marker.write_text(json.dumps(dict(self.session("without"), exit=99)))
        self.assertEqual(self.measure("--arm", "without"), 0)
        self.assertEqual(self.session("without")["exit"], 99)
        config = json.loads(self.config.read_text())
        self.config.write_text(json.dumps(dict(config, repetitions=2)))
        self.assertEqual(self.measure("--arm", "without"), 2)
        self.assertEqual(self.measure("--arm", "without", "--force"), 0)
        self.assertEqual(self.session("without")["exit"], 0)

    def test_a_forced_run_with_a_changed_identity_drops_sessions_of_the_old_run(self):
        self.assertEqual(self.measure("--arm", "without"), 0)
        self.assertEqual(self.measure("--arm", "wiki"), 0)
        config = json.loads(self.config.read_text())
        self.config.write_text(json.dumps(dict(config, repetitions=2)))
        self.assertEqual(self.measure("--arm", "without", "--force"), 0)
        names = sorted(p.name for p in (self.work / "sessions").glob("*.json"))
        self.assertEqual(names, ["guard.without.1.json", "guard.without.2.json"])

    def test_a_mistyped_task_on_a_forced_run_keeps_the_existing_sessions(self):
        self.assertEqual(self.measure("--arm", "without"), 0)
        self.config.write_text(json.dumps(dict(json.loads(self.config.read_text()), repetitions=2)))
        self.assertEqual(self.measure("--task", "nope", "--force"), 2)
        self.assertTrue((self.work / "sessions/guard.without.1.json").exists())

    def test_unknown_task_filter_fails(self):
        self.assertEqual(self.measure("--task", "nope"), 2)


def codex_event(**event):
    return json.dumps(event)


def codex_command_item(command, code=0):
    return codex_event(type="item.completed", item=dict(type="command_execution", command="/bin/bash -lc " + json.dumps(command), exit_code=code,
                                                        status="completed" if code == 0 else "failed"))


def codex_change(path, status="completed"):
    return codex_event(type="item.completed", item=dict(type="file_change", status=status, changes=[dict(path=path, kind="update")]))


class AnalyzeCodexTests(unittest.TestCase):
    def test_tokens_tool_calls_and_edits_come_from_the_codex_events(self):
        usage = dict(input_tokens=100, cached_input_tokens=60, output_tokens=20, reasoning_output_tokens=5)
        out = cm.analyze_codex("\n".join([
            codex_event(type="turn.started"), codex_command_item("cat f.py"), codex_change("f.py"), codex_change("f.py"), codex_change("g.py", "failed"),
            codex_event(type="item.completed", item=dict(type="agent_message", text="hi")), codex_event(type="turn.completed", usage=usage),
            codex_event(type="turn.completed", usage=usage), "garbage", ""]))
        self.assertEqual(out["tokens"], dict(input=200, cached=120, output=40, reasoning=10, total=240, source="turn.completed"))
        self.assertEqual((out["turns"], out["tool_calls"], out["edits_attempted"], out["edits_failed"], out["re_edits"], out["unparsed_lines"]), (2, 4, 3, 1, 1, 1))
        self.assertEqual((out["reverts"], out["cost_usd"]), (None, None))

    def test_shell_edits_are_counted_and_a_failed_one_is_a_failed_edit(self):
        out = cm.analyze_codex("\n".join([codex_command_item("sed -i s/a/b/ f.py"), codex_command_item("python3 - <<'EOF'\nopen('f','w').write('x')\nEOF", 1),
                                         codex_command_item("git status --short"), codex_command_item("cat f.py > /dev/null")]))
        self.assertEqual((out["tool_calls"], out["bash_edits"], out["bash_edits_failed"], out["edits_attempted"]), (4, 2, 1, 0))
        self.assertEqual(out["tokens"]["source"], "none")

    def test_is_bash_edit_matches_writes_and_ignores_reads(self):
        for command in ("sed -i s/a/b/ f", "cat > f.py", "echo x >> f", "tee f", "git apply p", "mv a b", "perl -pi -e s/a/b/ f"):
            with self.subTest(command):
                self.assertTrue(cm.is_bash_edit(command))
        for command in ("cat f", "git diff", "python3 -m unittest 2>&1", "grep a f > /dev/null", "ls -la"):
            with self.subTest(command):
                self.assertFalse(cm.is_bash_edit(command))


def climb_config(**changes):
    config = json.loads((FIXTURES / "hillclimb.json").read_text())
    config.update(changes)
    return config


class ClimbConfigTests(unittest.TestCase):
    def load(self, config):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "climb.json"
            path.write_text(json.dumps(config))
            return cm.load_climb(path)

    def test_shipped_config_and_facts_are_valid(self):
        config = self.load(climb_config())
        self.assertEqual((config["model_role"], config["control"]), ("builder-trivial", "without"))
        self.assertTrue({t["kind"] for t in config["tasks"]} == {"knowledge", "control"})
        self.assertGreaterEqual(len(cm.load_facts(FIXTURES / "hillclimb_facts.json")), 1)
        events = {e for v in config["variants"] if v["params"] for e in v["params"]["events"]}
        self.assertEqual(events, set(cm.EVENTS))

    def test_invalid_configs_are_rejected(self):
        config = climb_config()
        variants, first = config["variants"], config["variants"][1]
        bad = dict(
            seeds=climb_config(seeds=0), objective=climb_config(objective="vibes"), extra=dict(config, surprise=1),
            same=climb_config(incumbent=config["control"]), missing=climb_config(control="nope"),
            control_with_params=climb_config(variants=[dict(variants[0], params=first["params"])] + variants[1:]),
            variant_without_params=climb_config(variants=[variants[0], dict(first, params=None)] + variants[2:]),
            event=climb_config(variants=[variants[0], dict(first, params=dict(first["params"], events=["Nope"]))] + variants[2:]),
            budget=climb_config(variants=[variants[0], dict(first, params=dict(first["params"], budget=0))] + variants[2:]),
            ranking=climb_config(variants=[variants[0], dict(first, params=dict(first["params"], ranking="magic"))] + variants[2:]),
            duplicate=climb_config(variants=variants + [first]),
            no_knowledge=climb_config(tasks=[dict(t, kind="control") for t in config["tasks"]]),
            kind=climb_config(tasks=[dict(config["tasks"][0], kind="odd")]), noise=climb_config(noise_multiplier=-1))
        for name, broken in bad.items():
            with self.subTest(name), self.assertRaises(cm.c.ColdfixError):
                self.load(broken)

    def test_invalid_facts_are_rejected(self):
        good = json.loads((FIXTURES / "hillclimb_facts.json").read_text())
        for name, facts in dict(empty=[], duplicate=[good[0], good[0]], extra=[dict(good[0], x=1)], no_anchor=[dict(good[0], anchors=[])]).items():
            with self.subTest(name), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "facts.json"
                path.write_text(json.dumps(facts))
                with self.assertRaises(cm.c.ColdfixError):
                    cm.load_facts(path)


def seed_session(variant, seed, task, tokens, accepted=True, kind="knowledge"):
    return dict(id=f"{variant}.{task}.{seed}", variant=variant, task=task, kind=kind, seed=seed, accepted=accepted, wall_seconds=1.5, tool_calls=2,
                tokens=dict(input=tokens, cached=0, output=0, reasoning=0, total=tokens, source="turn.completed"), edits_failed=0, bash_edits=0, bash_edits_failed=0, re_edits=0,
                deliveries=1, delivered_facts=1, delivered_chars=10)


class JudgeTests(unittest.TestCase):
    config = climb_config(tasks=[dict(t, kind=t["kind"]) for t in climb_config()["tasks"][:2]],
                          variants=[dict(id="without", params=None)] + [dict(id=name, params=dict(events=["PreToolUse"], budget=None, ranking="ref", once=False))
                                                                       for name in ("base", "noisy", "cheap", "worse", "bad", "short", "extra")],
                          control="without", incumbent="base")

    def sessions(self, **totals):
        out = []
        for variant, per_seed in totals.items():
            for seed, (tokens, accepted) in enumerate(per_seed, 1):
                out += [seed_session(variant, seed, t["id"], tokens // 2, accepted, t["kind"]) for t in self.config["tasks"]]
        return out

    def verdicts(self, **totals):
        return cm.judge(self.config, cm.seed_rows(self.sessions(**totals), self.config))

    def test_a_variant_is_kept_only_when_it_beats_the_best_by_more_than_the_noise(self):
        out = self.verdicts(base=[(1000, True), (1100, True)], cheap=[(500, True), (520, True)], noisy=[(1020, True), (990, True)], worse=[(2000, True), (2100, True)])
        self.assertEqual({k: v[0] for k, v in out.items()}, dict(without="control", base="baseline", cheap="keep", noisy="reject-noise", worse="reject-worse",
                                                                 bad="insufficient-seeds", short="insufficient-seeds", extra="insufficient-seeds"))

    def test_a_seed_with_a_session_that_has_no_token_measurement_is_not_complete(self):
        sessions = self.sessions(base=[(1000, True), (1100, True)])
        sessions[0]["tokens"]["source"] = "none"
        rows = cm.seed_rows(sessions, self.config)
        self.assertEqual([rows[("base", 1)]["complete"], rows[("base", 2)]["complete"]], [False, True])

    def test_later_variants_are_judged_against_the_new_best(self):
        out = self.verdicts(base=[(1000, True), (1100, True)], cheap=[(500, True), (520, True)], extra=[(480, True), (540, True)])
        self.assertEqual((out["cheap"][0], out["extra"][0]), ("keep", "reject-noise"))

    def test_fewer_accepted_tasks_reject_the_variant_even_when_it_is_cheaper(self):
        out = self.verdicts(base=[(1000, True), (1100, True)], bad=[(100, False), (100, True)])
        self.assertEqual(out["bad"][0], "reject-quality")

    def test_a_seed_missing_a_task_is_incomplete(self):
        sessions = [s for s in self.sessions(base=[(1000, True), (1100, True)], short=[(500, True), (500, True)]) if s["id"] != f"short.{self.config['tasks'][0]['id']}.2"]
        seeds = cm.seed_rows(sessions, self.config)
        self.assertEqual([k for k, v in seeds.items() if not v["complete"]], [("short", 2)])
        self.assertEqual(cm.judge(self.config, seeds)["short"][0], "insufficient-seeds")

    def test_every_tsv_row_names_the_role_and_exact_model_and_the_variant_parameters(self):
        sessions = self.sessions(base=[(1000, True), (1100, True)], cheap=[(500, True), (520, True)])
        seeds = cm.seed_rows(sessions, self.config)
        meta = dict(model=dict(role="builder-trivial", provider="codex", model="gpt-test", effort="default"))
        rows = list(csv.reader(io.StringIO(cm.tsv(meta, self.config, seeds, cm.judge(self.config, seeds))), delimiter="\t"))
        header = rows[0]
        body = [dict(zip(header, row)) for row in rows[1:]]
        self.assertEqual(tuple(header), cm.TSV_COLUMNS)
        self.assertEqual(len(body), 4)
        for row in body:
            self.assertEqual((row["role"], row["provider"], row["model"], row["effort"]), ("builder-trivial", "codex", "gpt-test", "default"))
        cheap = [r for r in body if r["variant"] == "cheap"]
        self.assertEqual([r["verdict"] for r in cheap], ["keep", "keep"])
        self.assertEqual(json.loads(cheap[0]["params"])["events"], ["PreToolUse"])
        self.assertEqual((cheap[0]["tokens_total"], cheap[0]["sessions"]), ("500", "2"))


class ClimbWiringTests(base.ImageCase):
    def setUp(self):
        super().setUp()
        self.out = tempfile.TemporaryDirectory()
        self.addCleanup(self.out.cleanup)
        self.work = Path(self.out.name) / "work"
        (self.repo / "facts.json").write_text(json.dumps([dict(id="alpha", text="Alpha keeps strings intact.", anchors=["guarded.py"])]))
        def entry(**changes):
            return dict(dict(events=["PreToolUse"], budget=None, ranking="ref", once=False), **changes)
        variants = [dict(id="without", params=None), dict(id="every", params=entry()), dict(id="once", params=entry(once=True)),
                    dict(id="start", params=entry(events=["SessionStart", "UserPromptSubmit"], budget=1))]
        config = climb_config(seeds=2, agent_command=[sys.executable, str(FIXTURES / "fake_codex.py")], facts="facts.json", variants=variants, incumbent="every",
                              tasks=[dict(id="guard", kind="knowledge", base=self.before, prompt="Fix guarded.py.", accept=dict(copy={}, commands=[["python3", "check.py"]]))])
        self.config = Path(self.out.name) / "climb.json"
        self.config.write_text(json.dumps(config))
        self.login = Path(self.out.name) / "login"
        self.login.mkdir()
        (self.login / "auth.json").write_text("{}")
        patcher = mock.patch.dict(os.environ, CODEX_HOME=str(self.login))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.model = dict(role="builder-trivial", provider="codex", model="gpt-test", effort="high")

    def climb(self, *extra):
        with mock.patch.object(cm, "resolve_model", return_value=self.model) as resolve, contextlib.redirect_stdout(io.StringIO()):
            code = cm.main(["climb", "--config", str(self.config), "--source", str(self.repo), "--work", str(self.work), *extra])
        resolve.assert_called_with("builder-trivial", "codex")
        return code

    def session(self, variant, seed=1):
        return json.loads((self.work / "sessions" / f"{variant}.guard.{seed}.json").read_text())

    def test_each_variant_gets_its_own_hooks_and_every_session_is_scored_on_the_codex_transcript(self):
        self.assertEqual(self.climb(), 0)
        records = {name: self.session(name) for name in ("without", "every", "once", "start")}
        for name, record in records.items():
            with self.subTest(name):
                self.assertEqual((record["accepted"], record["tokens"]["total"], record["tokens"]["cached"], record["edits_attempted"], record["wall_seconds"] > 0), (True, 120, 60, 1, True))
        self.assertEqual([records[n]["deliveries"] for n in ("without", "every", "once", "start")], [0, 2, 1, 2])
        self.assertEqual(records["start"]["delivered_events"], ["SessionStart", "UserPromptSubmit"])
        self.assertEqual((records["start"]["delivered_facts"], records["every"]["delivered_events"]), (2, ["PreToolUse"]))
        self.assertIsNone(records["without"]["image"])
        self.assertEqual(records["every"]["image"]["curated"], 1)
        self.assertEqual(records["every"]["tool_calls"], 3)
        first = json.loads((self.work / "transcripts/every.guard.1.jsonl").read_text().splitlines()[0])
        self.assertIn("gpt-test", first["argv"])
        self.assertTrue(first["home_has_auth"])
        self.assertIn("model_reasoning_effort=\"high\"", first["argv"])

    def test_the_isolated_codex_home_links_the_login_instead_of_copying_it_and_trusts_the_workspace(self):
        self.assertEqual(self.climb("--variant", "every", "--variant", "without", "--task", "guard"), 0)
        home = self.work / "codex-homes/every.guard.1"
        self.assertTrue((home / "auth.json").is_symlink())
        self.assertEqual((home / "auth.json").resolve(), (self.login / "auth.json").resolve())
        self.assertIn("hooks = true", (home / "config.toml").read_text())
        hooks = json.loads((home / "hooks.json").read_text())["hooks"]["PreToolUse"][0]
        self.assertEqual(hooks["matcher"], "Bash|apply_patch")
        self.assertEqual(json.loads((self.work / "codex-homes/without.guard.1/hooks.json").read_text()), dict(hooks={}))

    def test_report_judges_the_variants_and_writes_one_tsv_row_per_variant_and_seed(self):
        self.assertEqual(self.climb(), 0)
        tsv, md = Path(self.out.name) / "e.tsv", Path(self.out.name) / "h.md"
        self.assertEqual(cm.main(["climb-report", "--work", str(self.work), "--tsv", str(tsv), "--md", str(md)]), 0)
        rows = list(csv.reader(io.StringIO(tsv.read_text()), delimiter="\t"))
        self.assertEqual(len(rows) - 1, 8)
        self.assertTrue(all(row[rows[0].index("model")] == "gpt-test" and row[rows[0].index("role")] == "builder-trivial" for row in rows[1:]))
        text = md.read_text()
        for word in ("builder-trivial", "gpt-test", "every", "once", "start", "reject-", "SessionStart", "## Limits"):
            self.assertIn(word, text)

    def test_finished_sessions_are_skipped_and_a_changed_run_needs_force(self):
        self.assertEqual(self.climb("--variant", "without"), 0)
        marker = self.work / "sessions/without.guard.1.json"
        marker.write_text(json.dumps(dict(self.session("without"), exit=99)))
        self.assertEqual(self.climb("--variant", "without"), 0)
        self.assertEqual(self.session("without")["exit"], 99)
        self.config.write_text(json.dumps(dict(json.loads(self.config.read_text()), seeds=3)))
        self.assertEqual(self.climb("--variant", "without"), 2)

    def test_a_forced_climb_with_a_changed_identity_drops_sessions_of_the_old_run(self):
        self.assertEqual(self.climb(), 0)
        self.config.write_text(json.dumps(dict(json.loads(self.config.read_text()), seeds=3)))
        self.assertEqual(self.climb("--variant", "without", "--force"), 0)
        names = sorted(p.name for p in (self.work / "sessions").glob("*.json"))
        self.assertEqual(names, ["without.guard.1.json", "without.guard.2.json", "without.guard.3.json"])

    def test_a_mistyped_filter_on_a_forced_climb_keeps_the_existing_sessions(self):
        self.assertEqual(self.climb(), 0)
        before = sorted((self.work / "sessions").glob("*.json"))
        self.config.write_text(json.dumps(dict(json.loads(self.config.read_text()), seeds=3)))
        self.assertEqual(self.climb("--variant", "nope", "--force"), 2)
        self.assertEqual(sorted((self.work / "sessions").glob("*.json")), before)

    def test_unknown_filters_fail(self):
        self.assertEqual(self.climb("--variant", "nope"), 2)
        self.assertEqual(self.climb("--task", "nope"), 2)

    def test_codex_command_passes_the_role_model_and_sandbox_from_config(self):
        task = dict(prompt="do it")
        config = json.loads(self.config.read_text())
        command = cm.codex_command(config, dict(model="m", effort="default"), task, Path("/h"), Path("/w"))
        self.assertEqual(command[:2], ["env", "CODEX_HOME=/h"])
        self.assertEqual((command[command.index("-m") + 1], command[command.index("-s") + 1], command[-1]), ("m", config["agent_sandbox"], "do it"))
        self.assertNotIn("-c", command)


class DiagnoseTests(unittest.TestCase):
    def hook(self, text):
        output = json.dumps(dict(hookSpecificOutput=dict(additionalContext=text)))
        return json.dumps(dict(type="system", subtype="hook_response", hook_name="PreToolUse:Edit", output=output))

    def say(self, text):
        return json.dumps(dict(type="assistant", message=dict(content=[dict(type="text", text=text)])))

    def test_deliveries_are_counted_and_mentions_are_separated_from_dismissals(self):
        fact = "[coldrecall] Parse JSONL at newline boundaries."
        raw = "\n".join([self.say("Starting."), self.hook(fact + "\n" + fact), self.say("Noted the Unicode separator fact; it is unrelated to this test."), self.hook(fact)])
        found = cm.reflex_deliveries(raw)
        self.assertEqual([(f["facts"], f["acknowledged"], f["dismissed"]) for f in found], [(2, True, True), (1, False, False)])

    def test_report_without_transcripts_has_no_diagnosis_and_with_them_renders_the_table(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            (work / "transcripts").mkdir()
            sessions = [dict(id="t.coldrecall.1", task="t", arm="coldrecall", accepted=True), dict(id="t.without.1", task="t", arm="without", accepted=True)]
            self.assertIsNone(cm.diagnose(work, sessions))
            (work / "transcripts/t.coldrecall.1.jsonl").write_text(self.hook("[coldrecall] fact") + "\n" + self.say("ok"))
            (work / "transcripts/t.without.1.jsonl").write_text(self.say("ok"))
            found = cm.diagnose(work, sessions)
            self.assertEqual((found["rows"][0]["fires"], found["rows"][0]["facts"], found["bash_edit_sessions"]), (1, 1, 0))
            text = "\n".join(cm.render_diagnosis(dict(diagnosis=found, tasks=dict(t={arm: dict(sessions=1, accepted=1) for arm in cm.ARMS}))))
        self.assertIn("t.coldrecall.1", text)
        self.assertIn("did not need project knowledge", text)


class ResolveModelTests(unittest.TestCase):
    def test_only_claude_models_resolve(self):
        def process(provider):
            return subprocess.CompletedProcess([], 0, json.dumps(dict(provider=provider, model="m", effort="high")).encode(), b"")
        fleet = mock.patch.dict(os.environ, {"COLDFIX_FLEET_CLI": "acme"})
        fleet.start()
        self.addCleanup(fleet.stop)
        with mock.patch.object(cm.c, "process", return_value=process("claudeAgent")) as call:
            self.assertEqual(cm.resolve_model("builder-fast")["model"], "m")
            self.assertEqual(call.call_args.args[0], ["acme", "model", "get", "builder-fast"])
        with mock.patch.object(cm.c, "process", return_value=process("codex")), self.assertRaises(cm.c.ColdfixError):
            cm.resolve_model("builder-fast")
        with mock.patch.object(cm.c, "process", return_value=process("codex")):
            self.assertEqual(cm.resolve_model("builder-trivial", "codex")["provider"], "codex")
        with mock.patch.object(cm.c, "process", return_value=process("claudeAgent")), self.assertRaises(cm.c.ColdfixError):
            cm.resolve_model("builder-trivial", "codex")


if __name__ == "__main__":
    unittest.main()
