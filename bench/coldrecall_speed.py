#!/usr/bin/env python3
"""Time Coldrecall ingest and recall on a repository's real proof records, repeatably."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import coldfix as c
import coldrecall as cr

RECORD = ".coldlocker/rules"
MISS = "no/such/file.py"
TIME = "/usr/bin/time"


def run_timed(command, cwd, stdin=None):
    start = time.perf_counter()
    result = subprocess.run(command, cwd=cwd, input=stdin, capture_output=True)
    seconds = time.perf_counter() - start
    c.require(result.returncode == 0, "benchmark child failed: " + result.stderr.decode("utf-8", "replace")[-200:])
    return seconds


def peak_rss_mb(command, cwd, stdin=None):
    """Peak resident set of the command and the children it waited for, or None without GNU time."""
    if not os.access(TIME, os.X_OK):
        return None
    with tempfile.NamedTemporaryFile("r") as report:
        result = subprocess.run([TIME, "-f", "%M", "-o", report.name, *command], cwd=cwd, input=stdin, capture_output=True)
        c.require(result.returncode == 0, "benchmark child failed: " + result.stderr.decode("utf-8", "replace")[-200:])
        return int(report.read().split()[-1]) / 1024


def put(out, key, value):
    if value is not None:
        out[key] = value


def percentile(values, share):
    ordered = sorted(values)
    return ordered[max(0, -(-len(ordered) * share // 100) - 1)]


def current_rss_kb():
    with open("/proc/self/statm") as stream:
        return int(stream.read().split()[1]) * (os.sysconf("SC_PAGE_SIZE") // 1024)


def real_rules(repo):
    registry = json.loads((repo / RECORD).parent.joinpath("rules.json").read_text(encoding="utf-8"))
    names = [name for name, entry in sorted(registry.items()) if isinstance(entry, dict) and entry.get("lesson") and entry.get("pending") is False]
    c.require(names, "no ready proof records in " + str(repo))
    return {name: (repo / RECORD / (name + ".md")).read_text(encoding="utf-8") for name in names}


def populate(scratch, templates, copies):
    shutil.rmtree(scratch / cr.DIRECTORY, ignore_errors=True)
    shutil.rmtree(scratch / RECORD, ignore_errors=True)
    (scratch / RECORD).mkdir(parents=True)
    registry = {}
    for index in range(copies):
        name, text = list(templates.items())[index % len(templates)]
        old = name.rsplit("-", 1)[1]
        new = old if index < len(templates) else hashlib.sha256(f"{name}:{index}".encode()).hexdigest()[:32]
        copy = name.replace(old, new)
        path = scratch / RECORD / (copy + ".md")
        path.write_text(text.replace(old, new), encoding="utf-8")
        digest = hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        registry[copy] = dict(hashes=[digest], lesson=True, pending=False)
    (scratch / ".coldlocker/rules.json").write_text(json.dumps(registry), encoding="utf-8")


def ingest(scratch, copies, templates, out):
    populate(scratch, templates, copies)
    prefix = f"ingest.n{copies}."
    command = [sys.executable, str(ROOT / "coldrecall.py"), "compile", "--repo", str(scratch)]
    cold = run_timed(command, ROOT)
    again = run_timed(command, ROOT)
    out.update({prefix + "cold_s": cold, prefix + "cold_records_per_s": copies / cold, prefix + "reingest_s": again,
                prefix + "reingest_records_per_s": copies / again})
    put(out, prefix + "compile_peak_rss_mb", peak_rss_mb(command, ROOT))
    start = time.perf_counter()
    found, _, _ = cr.read_sources(scratch)
    out[prefix + "parse_and_anchor_s"] = time.perf_counter() - start
    head = cr.git(scratch, "rev-parse", "HEAD")
    start = time.perf_counter()
    results = [cr.run_witness(scratch, fact, head, None)["result"] for fact in found.values()]
    out[prefix + "witnesses_s"] = time.perf_counter() - start
    out[prefix + "witness_each_ms"] = out[prefix + "witnesses_s"] / copies * 1000
    facts = {ref: {key: value for key, value in fact.items() if not key.startswith("_")} for ref, fact in found.items()}
    for fact in facts.values():
        fact["witness"] = dict(result="pass", head=head, detail="")
    with tempfile.TemporaryDirectory(dir=scratch) as directory:
        start = time.perf_counter()
        cr.write_image(Path(directory), 1, head, facts, [])
        out[prefix + "write_image_s"] = time.perf_counter() - start
        out[prefix + "image_bytes"] = (Path(directory) / cr.IMAGE).stat().st_size
    out[prefix + "witnesses_passing"] = results.count("pass")
    out[prefix + "ingested"] = len(found)


def recall(scratch, copies, queries, out):
    prefix = f"recall.n{copies}."
    table = json.loads((scratch / cr.DIRECTORY / cr.TABLE).read_text(encoding="utf-8"))
    anchors = [anchor[len("path:"):] for anchor in table["anchors"]]
    c.require(anchors, "no fact is anchored to a path; the source repository lacks the proof commits")
    hit = anchors[0]
    out[prefix + "table_bytes"] = (scratch / cr.DIRECTORY / cr.TABLE).stat().st_size
    out[prefix + "facts_per_hit"] = len(table["anchors"].get("path:" + hit, []))
    for kind, path in (("hit", hit), ("miss", MISS)):
        cr.lookup(scratch, [path])
        samples, before = [], current_rss_kb()
        for _ in range(queries):
            start = time.perf_counter_ns()
            cr.lookup(scratch, [path])
            samples.append((time.perf_counter_ns() - start) / 1e6)
        for share in (50, 95):
            out[f"{prefix}{kind}_p{share}_ms"] = percentile(samples, share)
        out[prefix + kind + "_rss_growth_kb"] = current_rss_kb() - before
    start = time.perf_counter_ns()
    for _ in range(queries):
        cr.git(scratch, "rev-parse", "--show-toplevel")
    out[prefix + "one_git_call_ms"] = (time.perf_counter_ns() - start) / 1e6 / queries
    text = (scratch / cr.DIRECTORY / cr.TABLE).read_text(encoding="utf-8")
    start = time.perf_counter_ns()
    for _ in range(queries):
        json.loads(text)
    out[prefix + "table_parse_ms"] = (time.perf_counter_ns() - start) / 1e6 / queries
    return hit


def cold_start(scratch, hit, starts, out):
    event = json.dumps(dict(hook_event_name="PreToolUse", cwd=str(scratch), tool_input=dict(file_path=hit))).encode()
    commands = dict(
        python_floor=([sys.executable, "-c", "pass"], None),
        import_only=([sys.executable, "-c", "import coldrecall"], None),
        lookup_hit=([sys.executable, str(ROOT / "coldrecall.py"), "lookup", "--repo", str(scratch), "--path", hit], None),
        lookup_miss=([sys.executable, str(ROOT / "coldrecall.py"), "lookup", "--repo", str(scratch), "--path", MISS], None),
        hook_hit=([sys.executable, str(ROOT / "coldrecall.py"), "hook"], event))
    for name, (command, stdin) in commands.items():
        samples = [run_timed(command, ROOT, stdin) for _ in range(starts)]
        out[f"cold_start.{name}_ms_median"] = statistics.median(samples) * 1000
        out[f"cold_start.{name}_ms_max"] = max(samples) * 1000
        put(out, f"cold_start.{name}_rss_mb", peak_rss_mb(command, ROOT, stdin))


def measure(repo, workdir, sizes, queries, starts):
    templates = real_rules(repo)
    workdir.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="scratch-", dir=workdir))
    out = {}
    try:
        result = c.process(["git", "clone", "-q", "--shared", str(repo), str(scratch)])
        c.require(result.returncode == 0, "could not clone " + str(repo))
        for copies in sizes:
            ingest(scratch, copies, templates, out)
            hit = recall(scratch, copies, queries, out)
            if copies == sizes[0]:
                cold_start(scratch, hit, starts, out)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return out


def summarize(runs):
    summary = {}
    for key in runs[0]:
        values = [run[key] for run in runs]
        middle = statistics.median(values)
        summary[key] = dict(min=min(values), median=middle, max=max(values),
                            spread_pct=round((max(values) - min(values)) / middle * 100, 1) if middle else 0.0)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=str(ROOT), help="git repository whose .coldlocker proof records are ingested")
    parser.add_argument("--workdir", default=str(ROOT / ".measure/coldrecall-speed"))
    parser.add_argument("--sizes", default="1,10,50", help="record counts; the real records are cycled with fresh lesson ids past their number")
    parser.add_argument("--queries", type=int, default=500, help="warm lookups per kind per size")
    parser.add_argument("--cold-starts", type=int, default=10, help="fresh-process runs per command")
    parser.add_argument("--runs", type=int, default=3, help="independent repeats of the whole measurement")
    parser.add_argument("--out", help="write the JSON report here instead of stdout")
    args = parser.parse_args(argv)
    sizes = [int(size) for size in args.sizes.split(",")]
    runs = [measure(Path(args.repo).resolve(), Path(args.workdir).resolve(), sizes, args.queries, args.cold_starts) for _ in range(args.runs)]
    report = dict(config=dict(repo_records=len(real_rules(Path(args.repo).resolve())), sizes=sizes, queries=args.queries,
                              cold_starts=args.cold_starts, runs=args.runs, python=sys.version.split()[0],
                              cpus=os.cpu_count(), load_1min=os.getloadavg()[0]),
                  runs=runs, summary=summarize(runs))
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
