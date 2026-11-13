"""One offline run over retained source files and replay-bound lesson records."""

from argparse import Namespace
import json
from pathlib import Path
import tempfile

import coldfix as c
import measurements as m


def prepare(path):
    path = c.ordinary_path(path)
    content = c.read_text(path)
    manifest = json.loads(content, object_pairs_hook=c.unique_object)
    required = {"version", "sources", "lesson_store"}
    c.require(isinstance(manifest, dict) and required <= set(manifest)
              <= required | {"measurement_input", "window_days"}, "invalid run manifest")
    c.require(type(manifest["version"]) is int and manifest["version"] == 1, "invalid run version")
    days = manifest.get("window_days", 30)
    c.require(type(days) is int and days > 0, "invalid run window")

    def resolve(value):
        c.text(value, "input path")
        c.require(value != "-", "run inputs must be retained files")
        return c.ordinary_path(path.parent / value)

    seed_path, seed = c.load_store(resolve(manifest["lesson_store"]))
    c.require(c.latest_lessons(seed), "run requires retained authored lessons")
    store = {key: dict(seed[key]) if key in m.BASE else {} for key in c.FIELDS}
    seed_snapshot = {key: {ref: c.digest(c.encoded(row)) for ref, row in sorted(seed[key].items())}
                     for key in m.BASE}
    c.require(isinstance(manifest["sources"], list) and manifest["sources"], "run requires sources")
    sources, seen = [], set()
    for source in manifest["sources"]:
        c.require(isinstance(source, dict) and set(source) == {"format", "input"}, "invalid run source")
        source_path = resolve(source["input"])
        c.require(source_path not in seen, "duplicate run input")
        seen.add(source_path)
        source_content = c.read_text(source_path)
        records = c.input_records(source_content, source["format"], str(source_path))
        source_digest = c.digest(source_content)
        for index, row in enumerate(records):
            row["id"] = c.digest(c.encoded([str(source_path), source_digest, index]))[:32]
            previous = store["failures"].get(row["id"])
            c.require(previous is None or {k: v for k, v in previous.items() if k != "created"}
                      == {k: v for k, v in row.items() if k != "created"}, "conflicting retained event")
            store["failures"].setdefault(row["id"], row)
        sources.append(dict(input=str(source_path), format=source["format"],
                            sha256=source_digest, records=len(records)))
    c.require(any(row["records"] for row in sources), "run requires source events")
    data = {"version": 1}
    measurement = None
    if "measurement_input" in manifest:
        measurement_path = resolve(manifest["measurement_input"])
        measurement_content = c.read_text(measurement_path)
        measurement = dict(input=str(measurement_path), sha256=c.digest(measurement_content))
        data = json.loads(measurement_content, object_pairs_hook=c.unique_object)
    data = m.validate_input(data, store)
    provenance = dict(manifest=str(path), manifest_sha256=c.digest(content),
                      lesson_store=str(seed_path), lesson_snapshot=seed_snapshot,
                      sources=sources, measurement_input=measurement)
    return store, data, days, provenance


def private_directory(path):
    path = c.ordinary_path(path)
    if not path.exists():
        private_directory(path.parent)
        path.mkdir(mode=0o700, exist_ok=True)
    c.require(path.is_dir(), "run output must be a directory")
    return path


def run(args):
    try:
        store, data, days, provenance = prepare(args.input)
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise c.ColdfixError("invalid private run input; no run written") from None
    root = c.ordinary_path(args.store)
    runs = c.ordinary_path(root / "runs")
    for folder in (root, runs):
        c.require(not folder.exists() or folder.is_dir() and folder.stat().st_mode & 0o077 == 0,
                  "run output directories must be private (0700)")
    private_directory(runs)
    attempt = Path(tempfile.mkdtemp(prefix="run-", dir=runs))
    run_id = c.new_record()["id"]
    data["model_runs"].append(dict(run_id=run_id, complete=True, calls=[], evidence_ref="coldfix:run:" + run_id))
    c.atomic_create(attempt / "inputs.json", c.encoded(provenance))
    for collection in m.BASE:
        for row in store[collection].values():
            c.save(attempt, collection, row)
    grouped = c.group(None, attempt, store)
    measurement_path = attempt / "measurement-input.json"
    c.atomic_create(measurement_path, c.encoded(data))
    measured = m.measure(Namespace(input=str(measurement_path), window_days=days), attempt, store)
    _, validated = c.load_store(attempt)
    report = validated["measurements"][measured["report_id"]]
    c.require(report["outcomes"], "run produced no outcomes")
    summary = dict(events=len(validated["failures"]), groups=grouped["groups"],
                   lessons=len(c.latest_lessons(validated)), receipts=len(validated["receipts"]),
                   outcomes=len(report["outcomes"]), model_calls=0, status=report["status"])
    # This marker publishes last. Interrupted attempts remain private and never count as completed runs.
    c.atomic_create(attempt / "run.json", c.encoded(dict(version=1, run_id=run_id,
                    report_id=report["id"], summary=summary, inputs_sha256=c.digest(c.encoded(provenance)))))
    return dict(summary, _exit=measured["_exit"])
