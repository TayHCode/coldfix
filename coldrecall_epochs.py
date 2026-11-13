#!/usr/bin/env python3
"""Run independent contract probes between fresh Codex execution epochs."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import coldfix as c
import coldrecall as ci
import coldrecall_measure as measure

ROOT = Path(__file__).resolve().parent
VARIANTS = ("without", "reflex-current", "contract-after", "contract-before")
TAIL = "capture --tail must retain the last lines exactly, including Unicode separators and newline terminators."
SHOW = "show output must not reveal captured command or error text."


def probe(workspace, timeout, scratch=None, task_id=None):
    """Return only fixed diagnostics. All test inputs and output stay outside the workspace."""
    cli = workspace / "coldfix.py"
    with tempfile.TemporaryDirectory(prefix="coldrecall-probe-", dir=scratch) as tmp:
        root = Path(tmp)
        def call(*args):
            return subprocess.run([sys.executable, str(cli), "--store", str(root / "store"), *args],
                                  cwd=root, capture_output=True, timeout=timeout)
        def help_text(*args):
            result = call(*args, "--help")
            if result.returncode:
                raise RuntimeError("probe help failed")
            return result.stdout.decode("utf-8", errors="strict")
        failures = []
        top = help_text()
        capture_help = help_text("capture")
        if "--tail" in capture_help:
            log = root / "input.log"
            log.write_bytes("discard\nalpha\u2028beta\u0085gamma\u2029delta\nlast\n".encode())
            result = call("capture", "--command", "probe-command", "--repo", "probe", "--agent", "probe",
                          "--log-file", str(log), "--tail", "2")
            records = list((root / "store" / "failures").glob("*.json"))
            try:
                valid = result.returncode == 0 and len(records) == 1 and json.loads(records[0].read_text())["error"] == "alpha\u2028beta\u0085gamma\u2029delta\nlast\n"
            except (ValueError, KeyError, OSError):
                valid = False
            if not valid:
                failures.append(TAIL)
        elif task_id == "capture-tail":
            failures.append(TAIL)
        choices = re.search(r"\{([^{}]+)\}", top)
        if choices and "show" in choices.group(1).split(","):
            help_text("show")
            command, secret = "private-command-probe-2718", "private-error-probe-3141"
            result = call("capture", "--command", command, "--error", secret, "--repo", "probe", "--agent", "probe")
            try:
                identifier = json.loads(result.stdout)["failure_id"]
                if result.returncode != 0 or not isinstance(identifier, str):
                    raise ValueError()
                result = call("show", "--id", identifier)
                shown = json.loads(result.stdout)
                valid = (result.returncode == 0 and isinstance(shown, dict) and shown.get("id") == identifier
                         and all(value.encode() not in result.stdout + result.stderr for value in (command, secret)))
            except (ValueError, KeyError, TypeError):
                valid = False
            if not valid:
                failures.append(SHOW)
        elif task_id == "show-failure":
            failures.append(SHOW)
        return failures


def prompt(original, failures):
    return original if not failures else original + "\n\nIndependent contract diagnostics:\n" + "\n".join("- " + x for x in failures)


def valid_transcript(raw):
    completed = False
    last_type = None
    try:
        for line in raw.split("\n"):
            if not line.strip():
                continue
            event = json.loads(line)
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                return False
            last_type = event["type"]
            if event["type"] in ("turn.failed", "error"):
                return False
            if event["type"] == "turn.completed":
                usage = event.get("usage")
                if not isinstance(usage, dict) or any(type(usage.get(k)) is not int or usage[k] < 0 for k in ("input_tokens", "output_tokens")):
                    return False
                for k in ("cached_input_tokens", "reasoning_output_tokens"):
                    if k in usage and (type(usage[k]) is not int or usage[k] < 0):
                        return False
                completed = True
    except (ValueError, TypeError):
        return False
    return completed and last_type == "turn.completed"


def run_epoch(config, model, task, home, workspace, private, number):
    started = time.monotonic()
    try:
        done = c.process(measure.codex_command(config, model, task, home, workspace), cwd=workspace, timeout=config["session_timeout_seconds"])
        error = None if done.returncode == 0 else "nonzero-exit"
    except subprocess.TimeoutExpired as exc:
        done = subprocess.CompletedProcess(exc.cmd, None, exc.output or b"", exc.stderr or b"")
        error = "timeout"
    except OSError:
        done = subprocess.CompletedProcess([], None, b"", b"")
        error = "process-error"
    wall = time.monotonic() - started
    (private / f"epoch-{number}.stdout").write_bytes(done.stdout or b"")
    (private / f"epoch-{number}.stderr").write_bytes(done.stderr or b"")
    try:
        raw = done.stdout.decode("utf-8")
        valid = valid_transcript(raw)
        metrics = measure.analyze_codex(raw)
    except (UnicodeError, ValueError, TypeError, OverflowError):
        valid, metrics = False, None
    if not valid and error is None:
        error = "invalid-transcript-or-usage"
    return dict(error=error, exit=done.returncode, wall_seconds=wall, metrics=metrics)


def session(config, model, source, work, facts, task, variant, repetition, max_epochs):
    sid = f"{variant}.{task['id']}.{repetition}"
    workspace = measure.start_workspace(source, work, sid, task["base"])
    baseline = next(v for v in config["variants"] if v["id"] == ("reflex-current" if variant == "reflex-current" else "without"))
    home, image, log = measure.prepare_codex(baseline, source, workspace, config, facts, work, sid)
    private = work / ".measure" / sid
    private.mkdir(parents=True, mode=0o700)
    epochs, errors, diagnostics = [], [], []
    probe_results = []
    if variant == "contract-before":
        try:
            diagnostics = probe(workspace, config["acceptance_timeout_seconds"], private, task["id"])
            probe_results.append(diagnostics)
        except (OSError, subprocess.SubprocessError, UnicodeError, RuntimeError):
            errors.append("probe-error")
    while not errors and len(epochs) < max_epochs:
        current = dict(task, prompt=prompt(task["prompt"], diagnostics))
        epoch = run_epoch(config, model, current, home, workspace, private, len(epochs) + 1)
        epochs.append(epoch)
        if epoch["error"]:
            errors.append(epoch["error"])
            break
        if variant not in ("contract-before", "contract-after"):
            break
        try:
            diagnostics = probe(workspace, config["acceptance_timeout_seconds"], private, task["id"])
            probe_results.append(diagnostics)
        except (OSError, subprocess.SubprocessError, UnicodeError, RuntimeError):
            errors.append("probe-error")
            break
        if not diagnostics:
            break
    passed = not errors and not diagnostics and measure.accepted(source, workspace, task["accept"], config["acceptance_timeout_seconds"])
    metrics = [e["metrics"] for e in epochs if e["metrics"] is not None]
    tokens = {k: sum(m["tokens"][k] for m in metrics) for k in ("input", "cached", "output", "reasoning", "total")}
    record = dict(id=sid, task=task["id"], kind=task["kind"], variant=variant, repetition=repetition,
                  accepted=passed, errors=errors, probe_results=probe_results, epochs=len(epochs), epoch_errors=[e["error"] for e in epochs], tokens=tokens, usage_complete=bool(epochs) and not errors and all(e["metrics"] is not None and e["metrics"]["tokens"]["source"] != "none" for e in epochs),
                  wall_seconds=round(sum(e["wall_seconds"] for e in epochs), 3),
                  tool_calls=sum(m["tool_calls"] for m in metrics),
                  edits_failed=sum(m["edits_failed"] + m["bash_edits_failed"] for m in metrics), image=image)
    c.atomic_create(work / "sessions" / (sid + ".json"), c.encoded(record), replace=True)
    return record


def lesson_hashes(source):
    root = source / '.coldlocker'
    c.require(root.is_dir() and not root.is_symlink(), 'invalid lesson directory')
    paths = sorted(root.rglob('*'))
    c.require(not any(path.is_symlink() for path in paths), 'symlinked lesson inputs are unsupported')
    return {str(path.relative_to(source)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths if path.is_file()}


def run(args):
    source = Path(args.source).resolve()
    experiment_path = Path(args.config).resolve()
    experiment = json.loads(experiment_path.read_text())
    c.require(set(experiment) == {"version", "climb_config", "max_epochs", "repetitions"} and experiment["version"] == 1, "invalid experiment config")
    c.require(all(type(experiment[k]) is int and experiment[k] > 0 for k in ("max_epochs", "repetitions")), "invalid epoch or repetition count")
    repetitions = args.repetitions if args.repetitions is not None else experiment["repetitions"]
    c.require(repetitions > 0 and args.jobs > 0, "invalid repetition or worker count")
    c.require(len(args.variant) == len(set(args.variant)), "duplicate variants")
    climb_path = source / experiment["climb_config"]
    config = measure.load_climb(climb_path)
    facts_path = source / config["facts"]
    facts = measure.load_facts(facts_path)
    tasks = [t for t in config["tasks"] if not args.task or t["id"] in args.task]
    c.require(tasks and (not args.task or len(tasks) == len(set(args.task))), "unknown task")
    work = Path(args.work).resolve()
    work.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.chmod(work, 0o700)
    model = measure.resolve_model(config["model_role"], "codex")
    c.require(model["model"] == "gpt-6-luna", "experiment requires the requested Luna route")
    paths = {experiment_path, climb_path, facts_path, Path(__file__).resolve(), Path(measure.__file__).resolve(), source / "coldrecall.py", source / "coldfix.py"}
    paths.update(source / name for task in config["tasks"] for name in task["accept"]["copy"].values())
    hashes = {str(p.relative_to(source)) if p.is_relative_to(source) else p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}
    meta = dict(version=1, source_head=ci.git(source, "rev-parse", "HEAD"), source_hashes=hashes,
                lesson_hashes=lesson_hashes(source), model=model,
                experiment=experiment, repetitions=repetitions, jobs=args.jobs, variants=args.variant or list(VARIANTS), tasks=[t["id"] for t in tasks])
    c.atomic_create(work / "meta.json", c.encoded(meta), replace=True)
    def task_panel(task):
        for repetition in range(1, repetitions + 1):
            variants = args.variant or list(VARIANTS)
            offset = (repetition - 1) % len(variants)
            for variant in variants[offset:] + variants[:offset]:
                result = session(config, model, source, work, facts, task, variant, repetition, experiment["max_epochs"])
                print(f"{result['id']} accepted={result['accepted']} errors={','.join(result['errors']) or 'none'}", flush=True)
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        list(pool.map(task_panel, tasks))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    runner = sub.add_parser("run")
    runner.add_argument("--config", default=str(ROOT / "tests/fixtures/coldrecall_measure/epochs.json"))
    runner.add_argument("--source", default=str(ROOT))
    runner.add_argument("--work", required=True)
    runner.add_argument("--repetitions", type=int)
    runner.add_argument("--jobs", type=int, default=1)
    runner.add_argument("--variant", action="append", choices=VARIANTS, default=[])
    runner.add_argument("--task", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        return run(args)
    except (c.ColdfixError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print("coldrecall_epochs: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
