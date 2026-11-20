#!/usr/bin/env python3
"""Coldrecall measurement harness: arms and hillclimb variants, scored from agent session transcripts."""
import argparse
import csv
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import statistics
import subprocess
import sys
import time

import coldfix as c
import coldrecall as ci

ROOT = Path(__file__).resolve().parent
ARMS = ("without", "coldrecall", "wiki")
EDIT_TOOLS = ("Edit", "MultiEdit", "Write", "NotebookEdit")
TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
CONFIG_KEYS = {"version", "model_role", "repetitions", "session_timeout_seconds", "acceptance_timeout_seconds",
               "agent_command", "agent_tools", "agent_allowed", "wiki_command", "tasks"}
TASK_KEYS = {"id", "base", "prompt", "accept"}
ACCEPT_KEYS = {"copy", "commands"}
METRICS = ("tokens", "cost_usd", "edits_attempted", "edits_failed", "re_edits", "reverts")
TASK_ID = re.compile(r"[a-z0-9][a-z0-9-]*\Z")
EXCLUDED = (".coldrecall/",)
EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse")
RANKINGS = ("ref", "overlap")
KINDS = ("knowledge", "control")
OBJECTIVES = ("tokens_total", "wall_seconds", "tool_calls")
CLIMB_KEYS = {"version", "model_role", "seeds", "session_timeout_seconds", "acceptance_timeout_seconds", "hook_timeout_seconds",
              "agent_command", "agent_sandbox", "hook_matcher", "objective", "noise_multiplier", "control", "incumbent",
              "facts", "tasks", "variants"}
PARAM_KEYS = {"events", "budget", "ranking", "once"}
FACT_KEYS = {"id", "anchors", "text"}
CODEX_TOKENS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
NOT_TOOLS = ("agent_message", "reasoning", "error", "todo_list")
CHARS_PER_TOKEN = 4
ACKNOWLEDGED = re.compile(r"coldrecall|reflex|record boundar|unicode separator|u\+2028|splitlines", re.I)
DISMISSED = re.compile(r"nothing to do with|unrelated|doesn't relate|does not relate|not relevant|left it alone|ignored it", re.I)
BASH_EDIT = re.compile(
    r"\bsed\s+(?:-\S*i|--in-place)|\bperl\s+-\S*i|\btee\b|\bgit\s+apply\b|\bpatch\b|\b(?:mv|cp|rm)\s|\bpython3?\b.*(?:write_text|\.write\(|open\([^)]*,\s*['\"][wa+])"
    r"|(?<![<>&\d])>{1,2}(?![&=])\s*(?!/dev/null)[^\s&|;]", re.S)
TSV_COLUMNS = ("variant", "params", "seed", "role", "provider", "model", "effort", "sessions", "accepted", "accepted_knowledge",
               "tokens_input", "tokens_cached", "tokens_output", "tokens_reasoning", "tokens_total", "wall_seconds", "tool_calls",
               "edits_failed", "bash_edits", "bash_edits_failed", "re_edits", "deliveries", "delivered_facts", "delivered_chars",
               "verdict", "reason")


def strings(value):
    return isinstance(value, list) and bool(value) and all(isinstance(v, str) and v for v in value)


def number(value):
    return type(value) in (int, float) and value > 0


def load_config(path):
    config = json.loads(c.read_text(path), object_pairs_hook=c.unique_object)
    c.require(isinstance(config, dict) and set(config) == CONFIG_KEYS and config["version"] == 1, "invalid measurement config")
    c.require(isinstance(config["model_role"], str) and config["model_role"], "invalid model role")
    c.require(type(config["repetitions"]) is int and config["repetitions"] > 0, "invalid repetitions")
    c.require(number(config["session_timeout_seconds"]) and number(config["acceptance_timeout_seconds"]), "invalid timeouts")
    c.require(all(strings(config[key]) for key in ("agent_command", "agent_tools", "agent_allowed", "wiki_command")), "invalid command lists")
    check_tasks(config["tasks"], TASK_KEYS)
    return config


def check_tasks(tasks, keys):
    c.require(isinstance(tasks, list) and tasks and all(isinstance(t, dict) and set(t) == keys for t in tasks), "invalid tasks")
    c.require(len({t["id"] for t in tasks}) == len(tasks) and all(TASK_ID.fullmatch(t["id"]) for t in tasks), "invalid task IDs")
    for task in tasks:
        accept = task["accept"]
        c.require(all(isinstance(task[key], str) and task[key] for key in ("base", "prompt")), "invalid task text")
        c.require(isinstance(accept, dict) and set(accept) == ACCEPT_KEYS and isinstance(accept["copy"], dict)
                  and isinstance(accept["commands"], list) and accept["commands"] and all(strings(v) for v in accept["commands"]), "invalid acceptance")
        for dest, source in accept["copy"].items():
            c.require(all(isinstance(v, str) and v and not Path(v).is_absolute() and ".." not in Path(v).parts for v in (dest, source)), "invalid acceptance path")


def valid_params(params):
    return (isinstance(params, dict) and set(params) == PARAM_KEYS and isinstance(params["events"], list) and bool(params["events"])
            and len(set(params["events"])) == len(params["events"]) and all(e in EVENTS for e in params["events"])
            and (params["budget"] is None or (type(params["budget"]) is int and params["budget"] > 0))
            and params["ranking"] in RANKINGS and type(params["once"]) is bool)


def load_climb(path):
    config = json.loads(c.read_text(path), object_pairs_hook=c.unique_object)
    c.require(isinstance(config, dict) and set(config) == CLIMB_KEYS and config["version"] == 1, "invalid hillclimb config")
    c.require(isinstance(config["model_role"], str) and config["model_role"], "invalid model role")
    c.require(type(config["seeds"]) is int and config["seeds"] > 0, "invalid seeds")
    c.require(all(number(config[key]) for key in ("session_timeout_seconds", "acceptance_timeout_seconds", "hook_timeout_seconds", "noise_multiplier")), "invalid timeouts or noise multiplier")
    c.require(strings(config["agent_command"]) and all(isinstance(config[key], str) and config[key] for key in ("agent_sandbox", "hook_matcher", "facts")), "invalid agent settings")
    c.require(config["objective"] in OBJECTIVES, "invalid objective")
    check_tasks(config["tasks"], TASK_KEYS | {"kind"})
    c.require(all(t["kind"] in KINDS for t in config["tasks"]) and any(t["kind"] == "knowledge" for t in config["tasks"]), "invalid task kinds")
    variants = config["variants"]
    c.require(isinstance(variants, list) and all(isinstance(v, dict) and set(v) == {"id", "params"} for v in variants), "invalid variants")
    ids = [v["id"] for v in variants]
    c.require(len(set(ids)) == len(ids) and all(isinstance(i, str) and TASK_ID.fullmatch(i) for i in ids), "invalid variant IDs")
    c.require(config["control"] in ids and config["incumbent"] in ids and config["control"] != config["incumbent"], "control and incumbent must be distinct variants")
    for variant in variants:
        c.require((variant["params"] is None) == (variant["id"] == config["control"]) and (variant["params"] is None or valid_params(variant["params"])), "invalid variant params")
    return config


def load_facts(path):
    facts = json.loads(c.read_text(path), object_pairs_hook=c.unique_object)
    c.require(isinstance(facts, list) and facts and all(isinstance(f, dict) and set(f) == FACT_KEYS for f in facts), "invalid curated facts")
    c.require(len({f["id"] for f in facts}) == len(facts) and all(TASK_ID.fullmatch(f["id"]) and strings(f["anchors"]) and isinstance(f["text"], str) and f["text"] for f in facts), "invalid curated facts")
    return facts


def resolve_model(role, provider="claudeAgent"):
    done = c.process(c.fleet_model("get", role))
    c.require(done.returncode == 0, f"{c.fleet_label('model')} get {role} failed")
    model = json.loads(done.stdout.decode("utf-8"))
    c.require(model.get("provider") == provider and model.get("model"), "role " + role + " does not resolve to a " + provider + " model")
    return dict(role=role, provider=model["provider"], model=model["model"], effort=model.get("effort", "default"))


def blocks(event):
    content = event.get("message", {}).get("content") if event.get("type") in ("assistant", "user") else None
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def hunks(name, arguments):
    path = arguments.get("file_path") or arguments.get("notebook_path")
    if not isinstance(path, str):
        return []
    if name == "Edit":
        return [(path, arguments.get("old_string"), arguments.get("new_string"))]
    if name == "MultiEdit":
        return [(path, e.get("old_string"), e.get("new_string")) for e in arguments.get("edits") or [] if isinstance(e, dict)]
    return [(path, None, arguments.get("content", arguments.get("new_source")))]


def rework(sequence):
    history, re_edits, reverts = {}, 0, 0
    for path, old, new in sequence:
        earlier = history.setdefault(path, [])
        text = isinstance(old, str) and old != "" and isinstance(new, str)
        touched = bool(earlier) and (old is None or (text and any(
            (isinstance(w, str) and w != "" and (old in w or w in old)) or old == o for o, w in earlier)))
        undone = text and old != new and any(o == new and w == old for o, w in earlier)
        re_edits += touched
        reverts += undone
        earlier.append((old, new))
    return re_edits, reverts


def is_bash_edit(command):
    return isinstance(command, str) and bool(BASH_EDIT.search(command))


def analyze(raw):
    calls, failed, succeeded, messages, final, unparsed, reflexes, tool_calls, shell = {}, set(), set(), {}, None, 0, 0, 0, set()
    for line in raw.split("\n"):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            unparsed += 1
            continue
        if not isinstance(event, dict):
            unparsed += 1
            continue
        if event.get("type") == "result":
            final = event
        if str(event.get("subtype", "")).startswith("hook") and "[coldrecall]" in line:
            reflexes += 1
        for block in blocks(event):
            if block.get("type") == "tool_use":
                tool_calls += 1
                if block.get("name") == "Bash" and is_bash_edit((block.get("input") or {}).get("command")):
                    shell.add(block.get("id"))
            if block.get("type") == "tool_use" and block.get("name") in EDIT_TOOLS:
                calls[block.get("id")] = hunks(block["name"], block.get("input") or {})
            elif block.get("type") == "tool_result":
                (failed if block.get("is_error") else succeeded).add(block.get("tool_use_id"))
        message = event.get("message")
        if event.get("type") == "assistant" and isinstance(message, dict) and isinstance(message.get("usage"), dict):
            messages[message.get("id")] = message["usage"]
    usage = final["usage"] if final and isinstance(final.get("usage"), dict) else None
    pieces = usage or {key: sum(u.get(key) or 0 for u in messages.values()) for key in TOKEN_KEYS}
    tokens = {key: int(pieces.get(key) or 0) for key in TOKEN_KEYS}
    re_edits, reverts = rework([h for id_, hs in calls.items() if id_ in succeeded and id_ not in failed for h in hs])
    return dict(tokens=dict(tokens, total=sum(tokens.values()), source="result" if usage else "messages"),
                cost_usd=final.get("total_cost_usd") if final else None, turns=final.get("num_turns") if final else None,
                edits_attempted=len(calls), edits_failed=len([i for i in calls if i in failed]),
                re_edits=re_edits, reverts=reverts, reflex_lines=reflexes, unparsed_lines=unparsed,
                tool_calls=tool_calls, bash_edits=len(shell), bash_edits_failed=len(shell & failed))


def shell_command(command):
    try:
        parts = shlex.split(command)
    except ValueError:
        return command
    return parts[2] if len(parts) == 3 and parts[0].endswith("sh") and parts[1] == "-lc" else command


def analyze_codex(raw):
    usage, turns, calls, edits, failed, shell, shell_failed, touched, unparsed = dict.fromkeys(CODEX_TOKENS, 0), 0, 0, 0, 0, 0, 0, [], 0
    for line in raw.split("\n"):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            event = None
        if not isinstance(event, dict):
            unparsed += 1
            continue
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
            turns += 1
            for key in CODEX_TOKENS:
                usage[key] += int(event["usage"].get(key) or 0)
        elif event.get("type") == "item.completed" and item.get("type") not in NOT_TOOLS:
            calls += 1
            if item.get("type") == "command_execution" and is_bash_edit(shell_command(str(item.get("command")))):
                shell += 1
                shell_failed += item.get("status") == "failed" or item.get("exit_code") not in (0, None)
            elif item.get("type") == "file_change":
                edits += 1
                if item.get("status") == "completed":
                    touched += [(ch.get("path"), None, None) for ch in item.get("changes") or [] if isinstance(ch, dict) and isinstance(ch.get("path"), str)]
                else:
                    failed += 1
    re_edits, _ = rework(touched)
    tokens = dict(input=usage["input_tokens"], cached=usage["cached_input_tokens"], output=usage["output_tokens"], reasoning=usage["reasoning_output_tokens"],
                  total=usage["input_tokens"] + usage["output_tokens"], source="turn.completed" if turns else "none")
    return dict(tokens=tokens, cost_usd=None, turns=turns, tool_calls=calls, edits_attempted=edits, edits_failed=failed, bash_edits=shell,
                bash_edits_failed=int(shell_failed), re_edits=re_edits, reverts=None, unparsed_lines=unparsed)


def start_workspace(source, work, sid, base):
    workspace = work / "workspaces" / sid
    shutil.rmtree(workspace, ignore_errors=True)
    workspace.parent.mkdir(parents=True, exist_ok=True)
    done = c.process(["git", "clone", "-q", "--no-hardlinks", str(source), str(workspace)])
    c.require(done.returncode == 0, "clone failed")
    ci.git(workspace, "checkout", "-q", "--detach", base)
    with (workspace / ".git/info/exclude").open("a") as exclude:
        exclude.write("".join(name + "\n" for name in EXCLUDED))
    return workspace


def build_image(source, workspace):
    shutil.copytree(source / ".coldlocker", workspace / ".coldlocker")
    image = ci.compile_image(workspace)
    shutil.rmtree(workspace / ".coldlocker")
    return image


def prepare_arm(arm, source, workspace, config, work, sid):
    settings, extra, image = {}, [], None
    if arm == "coldrecall":
        image = build_image(source, workspace)
        hook = dict(type="command", command=shlex.join([sys.executable, str(ROOT / "coldrecall.py"), "hook"]))
        settings = dict(hooks=dict(PreToolUse=[dict(matcher="Edit|Write|MultiEdit|NotebookEdit|Bash", hooks=[hook])]))
        image = dict(epoch=image["epoch"], active=len(image["active"]), regressions=len(image["regressions"]), retired=len(image["retired"]))
    elif arm == "wiki":
        done = c.process([*config["wiki_command"], str(workspace)], cwd=ROOT)
        c.require(done.returncode == 0 and done.stdout.strip(), "wiki fixture failed")
        extra = ["--append-system-prompt", done.stdout.decode("utf-8")]
    path = work / "settings" / (sid + ".json")
    c.atomic_create(path, json.dumps(settings), replace=True)
    return path, extra, image


def agent_command(config, model, task, settings, extra):
    command = [*config["agent_command"], "-p", task["prompt"], "--output-format", "stream-json", "--verbose",
               "--include-hook-events", "--model", model["model"], "--permission-mode", "acceptEdits",
               "--tools", ",".join(config["agent_tools"]), "--allowedTools", ",".join(config["agent_allowed"]),
               "--setting-sources", "project", "--settings", str(settings), "--strict-mcp-config",
               "--no-session-persistence", *extra]
    return command + ["--effort", model["effort"]] if model["effort"] != "default" else command


def accepted(source, workspace, accept, timeout):
    for dest, name in accept["copy"].items():
        (workspace / dest).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, workspace / dest)
    for command in accept["commands"]:
        try:
            if c.process(command, cwd=workspace, timeout=timeout).returncode != 0:
                return False
        except (subprocess.TimeoutExpired, OSError):
            return False
    return True


def run_session(config, model, source, work, task, arm, repetition):
    sid = f"{task['id']}.{arm}.{repetition}"
    workspace = start_workspace(source, work, sid, task["base"])
    settings, extra, image = prepare_arm(arm, source, workspace, config, work, sid)
    timed_out = False
    try:
        done = c.process(agent_command(config, model, task, settings, extra), cwd=workspace, timeout=config["session_timeout_seconds"])
    except subprocess.TimeoutExpired as error:
        timed_out, done = True, subprocess.CompletedProcess(error.cmd, None, error.output or b"", error.stderr or b"")
    raw = done.stdout.decode("utf-8", errors="replace")
    c.atomic_create(work / "transcripts" / (sid + ".jsonl"), raw, replace=True)
    changed = sorted(set(ci.git(workspace, "diff", "--name-only", "HEAD").split("\n") + ci.git(workspace, "ls-files", "--others", "--exclude-standard").split("\n")) - {""})
    record = dict(id=sid, task=task["id"], arm=arm, repetition=repetition, exit=done.returncode, timed_out=timed_out,
                  accepted=accepted(source, workspace, task["accept"], config["acceptance_timeout_seconds"]),
                  changed_files=changed, image=image, **analyze(raw))
    c.atomic_create(work / "sessions" / (sid + ".json"), c.encoded(record), replace=True)
    return record


def accept_run_identity(work, meta, force):
    path = work / "meta.json"
    if path.exists() and json.loads(c.read_text(path)) != meta:
        c.require(force, "work directory holds sessions for a different run; use --force or a new --work")
        for stale in (work / "sessions").glob("*.json"):
            stale.unlink()
    c.atomic_create(path, c.encoded(meta), replace=True)


def run(args):
    config = load_config(args.config)
    source, work = c.ordinary_path(args.source), c.ordinary_path(args.work)
    model = resolve_model(config["model_role"])
    meta = dict(version=1, config=config, model=model, source_head=ci.git(source, "rev-parse", "HEAD"))
    tasks = [t for t in config["tasks"] if not args.task or t["id"] in args.task]
    c.require(tasks, "no task matches --task")
    accept_run_identity(work, meta, args.force)
    for repetition in range(1, config["repetitions"] + 1):
        for task in tasks:
            for arm in args.arm or ARMS:
                sid = f"{task['id']}.{arm}.{repetition}"
                if (work / "sessions" / (sid + ".json")).exists() and not args.force:
                    print("skip " + sid)
                    continue
                record = run_session(config, model, source, work, task, arm, repetition)
                print(f"{sid} accepted={record['accepted']} tokens={record['tokens']['total']} failed_edits={record['edits_failed']} re_edits={record['re_edits']}")
    return 0


def load_sessions(work):
    return [json.loads(c.read_text(p)) for p in sorted((work / "sessions").glob("*.json"))]


def summarize(rows):
    result = dict(sessions=len(rows), accepted=sum(r["accepted"] for r in rows), timed_out=sum(r["timed_out"] for r in rows))
    for metric in METRICS:
        values = [(r["tokens"]["total"] if metric == "tokens" else r[metric]) for r in rows]
        known = [v for v in values if v is not None]
        result[metric] = dict(sum=sum(known), mean=sum(known) / len(known) if known else None, missing=len(values) - len(known))
    return result


def reflex_deliveries(raw):
    found, fired = [], False
    for line in raw.split("\n"):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if str(event.get("subtype", "")).startswith("hook") and "[coldrecall]" in line:
            try:
                context = json.loads(event.get("output") or "")["hookSpecificOutput"]["additionalContext"]
            except (ValueError, KeyError, TypeError):
                context = str(event.get("output") or "")
            found.append(dict(hook=event.get("hook_name"), facts=context.count("[coldrecall]"), chars=len(context), acknowledged=False, dismissed=False))
            fired = True
        elif fired and found and event.get("type") == "assistant":
            texts = [str(b.get("text")) for b in blocks(event) if b.get("type") == "text"]
            found[-1]["acknowledged"] |= any(ACKNOWLEDGED.search(t) for t in texts)
            found[-1]["dismissed"] |= any(ACKNOWLEDGED.search(t) and DISMISSED.search(t) for t in texts)
    return found


def diagnose(work, sessions):
    rows, shell = [], 0
    for session in sessions:
        path = work / "transcripts" / (session["id"] + ".jsonl")
        if not path.is_file():
            return None
        raw = c.read_text(path)
        shell += analyze(raw)["bash_edits"] > 0
        if session["arm"] == "coldrecall":
            found = reflex_deliveries(raw)
            rows.append(dict(id=session["id"], task=session["task"], tool_calls=analyze(raw)["tool_calls"], fires=len(found),
                             facts=sum(f["facts"] for f in found), chars=sum(f["chars"] for f in found),
                             acknowledged=any(f["acknowledged"] for f in found), dismissed=any(f["dismissed"] for f in found), accepted=session["accepted"]))
    return dict(rows=rows, bash_edit_sessions=shell, sessions=len(sessions)) if rows else None


def build_report(work):
    meta = json.loads(c.read_text(work / "meta.json"))
    sessions = load_sessions(work)
    c.require(sessions, "no sessions to report")
    tasks = [t["id"] for t in meta["config"]["tasks"]]
    return dict(version=1, meta=meta, sessions=sessions, diagnosis=diagnose(work, sessions),
                arms={arm: summarize([s for s in sessions if s["arm"] == arm]) for arm in ARMS},
                tasks={task: {arm: summarize([s for s in sessions if s["task"] == task and s["arm"] == arm]) for arm in ARMS} for task in tasks})


def cell(value, digits=1):
    if value is None:
        return "n/a"
    if not isinstance(value, float):
        return str(value)
    text = f"{value:.{digits}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def render_diagnosis(report):
    found = report.get("diagnosis")
    if not found:
        return []
    rows, tasks = found["rows"], report["tasks"]
    fired, acknowledged = [r for r in rows if r["fires"]], [r for r in rows if r["acknowledged"]]
    dismissed = [r for r in acknowledged if r["dismissed"]]
    lines = ["", "## Diagnosis: did the reflex fire, and did the tasks need it?", "",
             "| session | tool calls | reflex fires | facts delivered | chars delivered (≈ tokens) | mentioned by the agent | dismissed as unrelated | accepted |", "| --- | ---: | ---: | ---: | ---: | --- | --- | --- |"]
    lines += [f"| {r['id']} | {r['tool_calls']} | {r['fires']} | {r['facts']} | {r['chars']} (≈ {r['chars'] // CHARS_PER_TOKEN}) | {'yes' if r['acknowledged'] else 'no'} | {'yes' if r['dismissed'] else 'no'} | {r['accepted']} |" for r in rows]
    lines += ["", f"- The reflex fired in {len(fired)} of {len(rows)} `coldrecall` sessions and delivered {sum(r['facts'] for r in rows)} fact lines in total, {sum(r['chars'] for r in rows)} characters "
              f"(≈ {sum(r['chars'] for r in rows) // CHARS_PER_TOKEN} tokens at {CHARS_PER_TOKEN} characters per token, a rough rule).",
              f"- The agent's text mentioned the fact (pattern `ACKNOWLEDGED`: `coldrecall`, the hook, record boundaries, Unicode separators, U+2028 or `splitlines`) in {len(acknowledged)} of {len(rows)} sessions; in {len(dismissed)} of those it also said the fact was unrelated to the task (pattern `DISMISSED`). A mention is not proof the fact changed the work.",
              "- The `without` arm already passed the acceptance checks without any fact:"]
    lines += [f"  - `{task}`: {arms['without']['accepted']}/{arms['without']['sessions']} accepted with no fact, so these tasks did not need project knowledge." if arms["without"]["accepted"] == arms["without"]["sessions"]
              else f"  - `{task}`: {arms['without']['accepted']}/{arms['without']['sessions']} accepted with no fact." for task, arms in tasks.items()]
    lines += [f"- {found['bash_edit_sessions']} of {found['sessions']} sessions made at least one shell edit (pattern `BASH_EDIT`); these are not in the edit, failed-edit or re-edit counts above.", ""]
    return lines


def render(report):
    meta, model = report["meta"], report["meta"]["model"]
    lines = ["# Coldrecall measurement", "",
             "Generated by `coldrecall_measure.py report`; raw per-session records are in `coldrecall-measurement.json`.", "",
             "## Setup", "",
             f"- Model: `{model['model']}` (effort `{model['effort']}`), resolved with `{c.fleet_label('model')} get {model['role']}`.",
             f"- Source commit (rules and task repository): `{meta['source_head'][:12]}`.",
             f"- Repetitions per task and arm: {meta['config']['repetitions']}; session timeout: {meta['config']['session_timeout_seconds']} s.",
             "- Arms: `without` (plain agent), `coldrecall` (compiled image plus the PreToolUse reflex hook),",
             "  `wiki` (a generated wiki-style repo doc appended to the system prompt; the generator is a test fixture).", "",
             "## Tasks", ""]
    lines += [f"- `{t['id']}` at `{t['base'][:12]}`: {t['prompt']}" for t in meta["config"]["tasks"]]
    columns = "| {} | sessions | accepted | tokens (mean) | cost USD (mean) | edits | failed edits | re-edits | reverts |"
    row = "| {} | {} | {} | {} | {} | {} | {} | {} | {} |"
    separator = "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"

    def line(label, s):
        return row.format(label, s["sessions"], f"{s['accepted']}/{s['sessions']}", cell(s["tokens"]["mean"], 0), cell(s["cost_usd"]["mean"], 4),
                          cell(s["edits_attempted"]["mean"]), cell(s["edits_failed"]["mean"]), cell(s["re_edits"]["mean"]), cell(s["reverts"]["mean"]))
    lines += ["", "## Per-session means by arm", "", columns.format("arm"), separator]
    lines += [line(arm, report["arms"][arm]) for arm in ARMS]
    lines += ["", "## Per-session means by task and arm", "", columns.format("task / arm"), separator]
    lines += [line(f"{task} / {arm}", s) for task, arms in report["tasks"].items() for arm, s in arms.items()]
    lines += ["", "## Sessions", "", "| session | exit | accepted | tokens | edits | failed | re-edits | reverts | reflex lines | timed out |", "| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |"]
    lines += [f"| {s['id']} | {s['exit']} | {s['accepted']} | {s['tokens']['total']} | {s['edits_attempted']} | {s['edits_failed']} | {s['re_edits']} | {s['reverts']} | {s['reflex_lines']} | {s['timed_out']} |" for s in report["sessions"]]
    lines += render_diagnosis(report)
    lines += ["", "## Definitions", "",
              "- **tokens**: input + output + cache-creation + cache-read tokens of the session, from the agent's final `result` event",
              "  (the sum of per-message usage when the session was cut off). Background helper-model usage is not included.",
              "- **edits / failed edits**: `Edit`, `MultiEdit`, `Write` and `NotebookEdit` calls; failed means the tool result was an error.",
              "- **re-edits**: successful edits whose target text overlaps text an earlier successful edit in the same session wrote or replaced,",
              "  or a `Write` to a file already edited. **reverts** are the re-edits that exactly undo an earlier edit.",
              "- **accepted**: the task's acceptance commands passed in the final workspace.",
              "- **reflex lines**: hook events in the transcript carrying a `[coldrecall]` fact (only the `coldrecall` arm can have them).", "",
              "## Limits", "",
              "- Counts are per-session means over the repetitions above. With few repetitions, differences between arms are anecdotes, not effects.",
              "- One model, one small repository, a few tasks; the task set is a configured fixture, not a sample of real work.",
              "- Edits made through `Bash` (for example `sed -i`) are not counted as edits, failed edits or rework.",
              "- The `coldrecall` workspace compiles the image from the repository's delivered lesson, then removes the rule files, so the agent sees the fact only through the hook.",
              "- Tokens are dominated by the fixed system prompt and tool definitions shared by every arm; compare arms, not absolute values.", ""]
    return "\n".join(lines)


def write_report(args):
    report = build_report(c.ordinary_path(args.work))
    c.atomic_create(args.json, c.encoded(report), replace=True)
    c.atomic_create(args.md, render(report), replace=True)
    return 0


def add_curated(workspace, facts):
    path = workspace / ci.DIRECTORY / ci.TABLE
    table = json.loads(c.read_text(path))
    for fact in facts:
        ref = "curated:" + fact["id"]
        table["atoms"][ref] = dict(text=fact["text"], witness=None, proven=None)
        for anchor in fact["anchors"]:
            table["anchors"].setdefault("path:" + anchor, []).append(ref)
    c.atomic_create(path, c.encoded(table), replace=True)


def hook_command(params, log):
    command = [sys.executable, str(ROOT / "coldrecall.py"), "hook", "--ranking", params["ranking"], "--log", str(log)]
    if params["budget"]:
        command += ["--budget", str(params["budget"])]
    if params["once"]:
        command.append("--once")
    return shlex.join(command)


def hooks_for(params, matcher, timeout, log):
    entry = dict(hooks=[dict(type="command", command=hook_command(params, log), timeout=timeout)])
    return {event: [dict(entry, matcher=matcher) if event == "PreToolUse" else entry] for event in params["events"]}


def prepare_codex(variant, source, workspace, config, facts, work, sid):
    auth = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "auth.json"
    c.require(auth.is_file(), "no codex login found")
    home, log, params = work / "codex-homes" / sid, work / "deliveries" / (sid + ".jsonl"), variant["params"]
    shutil.rmtree(home, ignore_errors=True)
    home.mkdir(parents=True)
    (home / "auth.json").symlink_to(auth)
    c.atomic_create(home / "config.toml", "[features]\nhooks = true\n\n[projects." + json.dumps(str(workspace)) + "]\ntrust_level = \"trusted\"\n", replace=True)
    image = None
    if params:
        summary = build_image(source, workspace)
        add_curated(workspace, facts)
        image = dict(epoch=summary["epoch"], active=len(summary["active"]) + len(facts), curated=len(facts))
    hooks = hooks_for(params, config["hook_matcher"], config["hook_timeout_seconds"], log) if params else {}
    c.atomic_create(home / "hooks.json", json.dumps(dict(hooks=hooks)), replace=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.unlink(missing_ok=True)
    return home, image, log


def codex_command(config, model, task, home, workspace):
    command = ["env", "CODEX_HOME=" + str(home), *config["agent_command"], "exec", "--json", "-m", model["model"], "-s", config["agent_sandbox"],
               "-C", str(workspace), "--dangerously-bypass-hook-trust"]
    if model["effort"] != "default":
        command += ["-c", "model_reasoning_effort=" + json.dumps(model["effort"])]
    return command + [task["prompt"]]


def deliveries(log):
    rows = [json.loads(line) for line in c.read_text(log).split("\n") if line.strip()] if log.is_file() else []
    return dict(deliveries=len(rows), delivered_facts=sum(len(r["refs"]) for r in rows), delivered_chars=sum(r["chars"] for r in rows),
                delivered_refs=sorted({ref for r in rows for ref in r["refs"]}), delivered_events=sorted({r["event"] for r in rows}))


def climb_session(config, model, source, work, facts, task, variant, seed):
    sid = f"{variant['id']}.{task['id']}.{seed}"
    workspace = start_workspace(source, work, sid, task["base"])
    home, image, log = prepare_codex(variant, source, workspace, config, facts, work, sid)
    timed_out, started = False, time.monotonic()
    try:
        done = c.process(codex_command(config, model, task, home, workspace), cwd=workspace, timeout=config["session_timeout_seconds"])
    except subprocess.TimeoutExpired as error:
        timed_out, done = True, subprocess.CompletedProcess(error.cmd, None, error.output or b"", error.stderr or b"")
    wall = time.monotonic() - started
    raw = done.stdout.decode("utf-8", errors="replace")
    c.atomic_create(work / "transcripts" / (sid + ".jsonl"), raw, replace=True)
    changed = sorted(set(ci.git(workspace, "diff", "--name-only", "HEAD").split("\n") + ci.git(workspace, "ls-files", "--others", "--exclude-standard").split("\n")) - {""})
    record = dict(id=sid, variant=variant["id"], task=task["id"], kind=task["kind"], seed=seed, exit=done.returncode, timed_out=timed_out,
                  wall_seconds=round(wall, 3), accepted=accepted(source, workspace, task["accept"], config["acceptance_timeout_seconds"]),
                  changed_files=changed, image=image, **analyze_codex(raw), **deliveries(log))
    c.atomic_create(work / "sessions" / (sid + ".json"), c.encoded(record), replace=True)
    return record


def climb(args):
    config = load_climb(args.config)
    source, work = c.ordinary_path(args.source), c.ordinary_path(args.work)
    facts = load_facts(source / config["facts"])
    model = resolve_model(config["model_role"], "codex")
    meta = dict(version=1, config=config, facts=facts, model=model, source_head=ci.git(source, "rev-parse", "HEAD"))
    tasks = [t for t in config["tasks"] if not args.task or t["id"] in args.task]
    variants = [v for v in config["variants"] if not args.variant or v["id"] in args.variant]
    c.require(tasks and variants, "no task or variant matches the filters")
    accept_run_identity(work, meta, args.force)
    for seed in range(1, config["seeds"] + 1):
        for variant in variants:
            for task in tasks:
                sid = f"{variant['id']}.{task['id']}.{seed}"
                if (work / "sessions" / (sid + ".json")).exists() and not args.force:
                    print("skip " + sid)
                    continue
                r = climb_session(config, model, source, work, facts, task, variant, seed)
                print(f"{sid} accepted={r['accepted']} tokens={r['tokens']['total']} wall={r['wall_seconds']} tools={r['tool_calls']} deliveries={r['deliveries']}", flush=True)
    return 0


def aggregate(rows):
    def total(key):
        return sum(r["tokens"][key] for r in rows)
    return dict(sessions=len(rows), accepted=sum(r["accepted"] for r in rows),
                accepted_knowledge=sum(r["accepted"] for r in rows if r["kind"] == "knowledge"),
                tokens_input=total("input"), tokens_cached=total("cached"), tokens_output=total("output"), tokens_reasoning=total("reasoning"),
                tokens_total=total("total"), wall_seconds=round(sum(r["wall_seconds"] for r in rows), 3), tool_calls=sum(r["tool_calls"] for r in rows),
                edits_failed=sum(r["edits_failed"] + r["bash_edits_failed"] for r in rows), bash_edits=sum(r["bash_edits"] for r in rows),
                bash_edits_failed=sum(r["bash_edits_failed"] for r in rows), re_edits=sum(r["re_edits"] for r in rows),
                deliveries=sum(r["deliveries"] for r in rows), delivered_facts=sum(r["delivered_facts"] for r in rows),
                delivered_chars=sum(r["delivered_chars"] for r in rows))


def seed_rows(sessions, config):
    expected = {t["id"] for t in config["tasks"]}
    rows = {}
    for session in sessions:
        rows.setdefault((session["variant"], session["seed"]), []).append(session)
    return {key: dict(aggregate(group), complete=expected == {s["task"] for s in group} and len(group) == len(expected) and all(s["tokens"]["source"] != "none" for s in group)) for key, group in sorted(rows.items())}


def spread(values):
    return (statistics.fmean(values), statistics.stdev(values) if len(values) > 1 else None) if values else (None, None)


def standing(config, seeds, variant):
    complete = [r for (name, _), r in seeds.items() if name == variant and r["complete"]]
    mean, sd = spread([r[config["objective"]] for r in complete])
    return dict(seeds=len(complete), mean=mean, sd=sd, accepted=spread([r["accepted"] for r in complete])[0])


def judge(config, seeds):
    verdicts = {config["control"]: ("control", "reference run without hooks"), config["incumbent"]: ("baseline", "starting best")}
    best, objective = config["incumbent"], config["objective"]
    for variant in config["variants"]:
        name = variant["id"]
        if name in verdicts:
            continue
        mine, top = standing(config, seeds, name), standing(config, seeds, best)
        if mine["seeds"] < 2 or top["seeds"] < 2:
            verdicts[name] = ("insufficient-seeds", f"{name} has {mine['seeds']} and {best} has {top['seeds']} complete seeds; spread needs 2")
            continue
        gain = top["mean"] - mine["mean"]
        noise = config["noise_multiplier"] * max(mine["sd"], top["sd"])
        detail = f"{objective} mean {mine['mean']:.1f} vs {best} {top['mean']:.1f}; gain {gain:.1f}, noise {noise:.1f}; accepted {mine['accepted']:.2f} vs {top['accepted']:.2f}"
        if mine["accepted"] < top["accepted"]:
            verdicts[name] = ("reject-quality", detail)
        elif gain > noise:
            verdicts[name], best = ("keep", detail), name
        else:
            verdicts[name] = ("reject-noise" if gain >= -noise else "reject-worse", detail)
    return verdicts


def tsv(meta, config, seeds, verdicts):
    model, params = meta["model"], {v["id"]: v["params"] for v in config["variants"]}
    out = io.StringIO()
    writer = csv.writer(out, delimiter="\t", lineterminator="\n")
    writer.writerow(TSV_COLUMNS)
    for (variant, seed), row in seeds.items():
        verdict, reason = verdicts.get(variant, ("unknown-variant", "variant not in config")) if row["complete"] else ("incomplete", "not every task has a session for this seed")
        fixed = dict(variant=variant, params=json.dumps(params.get(variant), sort_keys=True, separators=(",", ":")), seed=seed, role=model["role"],
                     provider=model["provider"], model=model["model"], effort=model["effort"], verdict=verdict, reason=reason)
        writer.writerow([fixed[k] if k in fixed else row[k] for k in TSV_COLUMNS])
    return out.getvalue()


def climb_data(work):
    meta = json.loads(c.read_text(work / "meta.json"))
    sessions = load_sessions(work)
    c.require(sessions, "no sessions to report")
    config = meta["config"]
    seeds = seed_rows(sessions, config)
    return meta, sessions, seeds, judge(config, seeds)


def pair(values, digits=0):
    mean, sd = spread(values)
    return "n/a" if mean is None else cell(mean, digits) + ("" if sd is None else " ± " + cell(sd, digits))


def describe(params):
    if params is None:
        return "no hook"
    return f"{'+'.join(params['events'])}, budget {params['budget'] or 'all'}, {params['ranking']}, {'once' if params['once'] else 'every call'}"


def render_climb(meta, sessions, seeds, verdicts, latency=None):
    config, model = meta["config"], meta["model"]
    tasks, variants = config["tasks"], config["variants"]
    count = len(tasks)
    lines = ["# Coldrecall hillclimb", "",
             "Generated by `coldrecall_measure.py climb-report`; one row per variant and seed is in `coldrecall-experiments.tsv`.", "",
             "## Setup", "",
             f"- Role `{model['role']}` resolved with `{c.fleet_label('model')} get {model['role']}`: provider `{model['provider']}`, model `{model['model']}`, effort `{model['effort']}`.",
             f"- Source commit (rules and task repository): `{meta['source_head'][:12]}`. Seeds per variant: {config['seeds']}. Session timeout: {config['session_timeout_seconds']} s.",
             f"- Objective: `{config['objective']}` (lower is better). Control: `{config['control']}`. Incumbent: `{config['incumbent']}`.",
             f"- Keep rule: a variant is kept only if its accepted count per seed is at least the current best's and it beats the current best's objective by more than "
             f"{config['noise_multiplier']} × the larger standard deviation of the two variants' per-seed totals (`noise_multiplier` in the config). Variants are judged in config order against the best so far.",
             f"- The fixed task set has {count} tasks per seed; a seed counts only when every task has a session.",
             "- Facts: the compiled image holds the one real delivered lesson. The other facts are curated convention notes written for this experiment from the repository's conventions; they are not proven lessons.",
             f"  {len(meta['facts'])} curated facts, anchored to files, are added to the reflex table after compiling.", "",
             "## Hook placements on Codex", "",
             "- `SessionStart` and `UserPromptSubmit` exist on Codex and are used as is; both deliver every fact that is not already delivered, ranked by the prompt (SessionStart has no prompt, so `overlap` falls back to reference order).",
             f"- `PreToolUse` exists on Codex but its tools are `Bash` and `apply_patch`, not Claude's `Edit|Write|MultiEdit|NotebookEdit`. It is the nearest equivalent, with matcher `{config['hook_matcher']}`.",
             "- Codex transcripts do not show hook output, so deliveries are counted from a log the hook itself writes.",
             "- Sessions run `codex exec --json` with `--dangerously-bypass-hook-trust` (untrusted hooks otherwise stop the run), inside an isolated `CODEX_HOME` that holds only a link to the login and this harness's own hook.", "",
             "## Variants", "", "| variant | placement and options | verdict | reason |", "| --- | --- | --- | --- |"]
    lines += [f"| `{v['id']}` | {describe(v['params'])} | {verdicts[v['id']][0]} | {verdicts[v['id']][1]} |" for v in variants]
    lines += ["", f"## Results over complete seeds (mean ± sd of per-seed totals across the {count} tasks)", "",
              "| variant | seeds | accepted | knowledge accepted | tokens total | cached | output | wall s | tool calls | failed edits | deliveries | delivered chars |",
              "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for v in variants:
        rows = [r for (name, _), r in seeds.items() if name == v["id"] and r["complete"]]
        column = lambda key, digits=0: pair([r[key] for r in rows], digits)
        lines.append(f"| `{v['id']}` | {len(rows)} | {column('accepted', 1)}/{count} | {column('accepted_knowledge', 1)} | {column('tokens_total')} | {column('tokens_cached')} | "
                     f"{column('tokens_output')} | {column('wall_seconds', 1)} | {column('tool_calls', 1)} | {column('edits_failed', 1)} | {column('deliveries', 1)} | {column('delivered_chars')} |")
    for title, field in (("Accepted per task (accepted sessions / sessions)", "accepted"), ("Mean tokens per task", "tokens")):
        lines += ["", "## " + title, "", "| task | kind | " + " | ".join(f"`{v['id']}`" for v in variants) + " |", "| --- | --- | " + " | ".join("---:" for _ in variants) + " |"]
        for task in tasks:
            cells = []
            for v in variants:
                group = [s for s in sessions if s["variant"] == v["id"] and s["task"] == task["id"]]
                cells.append("n/a" if not group else (f"{sum(s['accepted'] for s in group)}/{len(group)}" if field == "accepted" else cell(statistics.fmean(s["tokens"]["total"] for s in group), 0)))
            lines.append(f"| `{task['id']}` | {task['kind']} | " + " | ".join(cells) + " |")
    unsolved = [t["id"] for t in tasks if not any(s["accepted"] for s in sessions if s["task"] == t["id"])]
    separating = [t["id"] for t in tasks if len({sum(s["accepted"] for s in sessions if s["variant"] == v["id"] and s["task"] == t["id"]) for v in variants}) > 1]
    lines += ["", "## What separates the variants", "",
              "- Tasks no variant solved, which carry no signal for ranking: " + (", ".join(f"`{t}`" for t in unsolved) or "none") + ".",
              "- Tasks whose accepted count differs between variants: " + (", ".join(f"`{t}`" for t in separating) or "none") + "."]
    if latency:
        lines += ["", "## Precomputed image versus live compile", "",
                  f"- Compiling the image in a fresh workspace (runs the lesson witness): median {cell(latency['compile_seconds'], 2)} s over {latency['repeat']} runs.",
                  f"- One hook call against the precomputed table: median {cell(latency['hook_seconds'], 3)} s over {latency['repeat']} runs.",
                  "- A live compile inside the session would also discard the curated facts, so `precomputed` versus `live` was measured as this latency only, not as a session variant."]
    lines += ["", "## Definitions", "",
              "- **tokens total**: Codex `input_tokens` + `output_tokens` summed over turns (input includes cached input; reasoning tokens are reported inside output). **cached** is `cached_input_tokens`. No cost is reported by Codex.",
              "- **wall s**: monotonic seconds around the agent process only, not setup or acceptance. Sessions run one after another unless the operator started several `climb` processes.",
              "- **tool calls**: completed Codex items that are not messages, reasoning or errors.",
              "- **failed edits**: failed `apply_patch` changes plus failed shell edits. A shell edit is a command matching the documented pattern `BASH_EDIT` in the harness (`sed -i`, `perl -i`, `tee`, redirects other than `/dev/null`, `git apply`, `patch`, `mv`/`cp`/`rm`, scripted file writes); failed means a non-zero exit.",
              "- **re-edits** (in the TSV): later `apply_patch` changes to a path already changed. Shell edits are not attributed to paths, so they are not in re-edits.",
              "- **accepted**: the task's acceptance commands passed in the final workspace; knowledge tasks fail on the untouched base and encode a repository convention.", "",
              "## Limits", "",
              "- Few seeds make the spread itself uncertain; a `keep` is a result of this rule on this task set, not a general claim.",
              "- Wall time includes model and network latency and is noisier than tokens. The committed round ran 4 `climb` processes at once on one machine, so wall times are measured under shared load.",
              "- No Sonnet comparison round was run; whether Sonnet ranks the variants the same way as Luna is not measured.",
              "- One model on one small repository; the task set and the curated facts are fixtures.",
              "- The Sonnet 5.5 numbers in `coldrecall-measurement.md` are the original baseline and are not comparable with these runs.", ""]
    return "\n".join(lines)


def climb_report(args):
    work = c.ordinary_path(args.work)
    meta, sessions, seeds, verdicts = climb_data(work)
    path = work / "latency.json"
    c.atomic_create(args.tsv, tsv(meta, meta["config"], seeds, verdicts), replace=True)
    c.atomic_create(args.md, render_climb(meta, sessions, seeds, verdicts, json.loads(c.read_text(path)) if path.is_file() else None), replace=True)
    return 0


def latency(args):
    config = load_climb(args.config)
    source, work = c.ordinary_path(args.source), c.ordinary_path(args.work)
    facts = load_facts(source / config["facts"])
    task = config["tasks"][0]
    compile_times, hook_times = [], []
    for _ in range(args.repeat):
        workspace = start_workspace(source, work, "latency", task["base"])
        started = time.monotonic()
        build_image(source, workspace)
        compile_times.append(time.monotonic() - started)
        add_curated(workspace, facts)
        payload = json.dumps(dict(hook_event_name="PreToolUse", cwd=str(workspace), tool_input=dict(file_path=str(workspace / "coldfix.py"))))
        started = time.monotonic()
        done = subprocess.run([sys.executable, str(ROOT / "coldrecall.py"), "hook"], input=payload.encode(), capture_output=True, check=True)
        hook_times.append(time.monotonic() - started)
        c.require(b"[coldrecall]" in done.stdout, "latency hook delivered nothing")
    result = dict(repeat=args.repeat, compile_seconds=statistics.median(compile_times), hook_seconds=statistics.median(hook_times))
    c.atomic_create(work / "latency.json", c.encoded(result), replace=True)
    print(c.encoded(result), end="")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    runner = commands.add_parser("run", help="run every task in every arm and store one record per session")
    runner.add_argument("--config", default=str(ROOT / "tests/fixtures/coldrecall_measure/config.json"))
    runner.add_argument("--source", default=str(ROOT), help="repository holding the task commits and the delivered .coldlocker rules")
    runner.add_argument("--work", default=str(ROOT / ".measure"))
    runner.add_argument("--task", action="append", default=[])
    runner.add_argument("--arm", action="append", choices=ARMS, default=[])
    runner.add_argument("--force", action="store_true", help="rerun finished sessions and accept a changed run identity")
    climber = commands.add_parser("climb", help="run every hillclimb variant on every task and seed with the configured Codex agent")
    climber.add_argument("--config", default=str(ROOT / "tests/fixtures/coldrecall_measure/hillclimb.json"))
    climber.add_argument("--source", default=str(ROOT))
    climber.add_argument("--work", default=str(ROOT / ".measure/climb"))
    climber.add_argument("--task", action="append", default=[])
    climber.add_argument("--variant", action="append", default=[])
    climber.add_argument("--force", action="store_true")
    scorer = commands.add_parser("climb-report", help="judge variants and write the experiments TSV and the Markdown report")
    scorer.add_argument("--work", default=str(ROOT / ".measure/climb"))
    scorer.add_argument("--tsv", default=str(ROOT / "docs/coldrecall-experiments.tsv"))
    scorer.add_argument("--md", default=str(ROOT / "docs/coldrecall-hillclimb.md"))
    timer = commands.add_parser("latency", help="time a live image compile against one hook call")
    timer.add_argument("--config", default=str(ROOT / "tests/fixtures/coldrecall_measure/hillclimb.json"))
    timer.add_argument("--source", default=str(ROOT))
    timer.add_argument("--work", default=str(ROOT / ".measure/climb"))
    timer.add_argument("--repeat", type=int, default=3)
    writer = commands.add_parser("report", help="aggregate stored sessions into the JSON and Markdown report")
    writer.add_argument("--work", default=str(ROOT / ".measure"))
    writer.add_argument("--json", default=str(ROOT / "docs/coldrecall-measurement.json"))
    writer.add_argument("--md", default=str(ROOT / "docs/coldrecall-measurement.md"))
    args = parser.parse_args(argv)
    try:
        return {"run": run, "report": write_report, "climb": climb, "climb-report": climb_report, "latency": latency}[args.action](args)
    except (c.ColdfixError, OSError, UnicodeError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print("coldrecall_measure: " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
