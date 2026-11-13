#!/usr/bin/env python3
"""Collect product failures from Coldpass run results, grouped by failure class; harness and setup noise is counted, never ingested."""

from collections import Counter
import hashlib
import json
import re
import sys
from urllib.parse import quote

import coldfix as c
import failure_ingest as fi


OUTCOMES = {"pass", "harness_error", "blocked", "unverified", "unsupported", "too_slow", "needs_credentials", "needs_browser", "needs_keys_or_hardware"}
HARNESS_OUTCOMES = {"harness_error"}
SETUP_OUTCOMES = {"unsupported", "needs_credentials", "needs_browser", "needs_keys_or_hardware"}
CHECKED = {"partly_checked", "readme_checked"}
EXCERPT = 2000
NOT_REPO = re.compile(r"output file could not be checked|\bNone required\b|\bNone for the\b|No fix needed"
                      r"|\bno (?:documented |runnable |user-facing )*(?:CLI|command-line|command|entry ?point|product operation|program|operation)\b"
                      r"|import-only|exposes no runnable|\b(?:no|needs?|requires?) (?:an? )?(?:[\w-]+ ){0,2}API key"
                      r"|\bthe checker (?:runs [^.]{0,60}without a pseudo-terminal|does not|doesn't|cannot|can't|could not|has no)"
                      r"|\bchecker subprocess working directory", re.I)
BOILERPLATE = "Inspect the recorded blocker and correct the README prerequisites or harness error."
CLASSES = (
    ("sibling-import", r"package-relative import|bare absolute imports? of sibling|absolute import that fails|instead of relative imports"),
    ("missing-file", r"missing from the (?:installed )?(?:package|wheel|sdist|source|repo\w*|snapshot)"
                     r"|not (?:in|present in) the (?:repository|repo|source|pinned|wheel|package)"
                     r"|(?:repo|repository|package|wheel) does not ship|imports? missing \S+ (?:module|package)|TemplateNotFound"
                     r"|repository contains no|(?:Commit|Restore|Ship) the [\w./-]+ (?:package|module|directory|file)"),
    ("readme-mismatch", r"README[^.]{0,120}(?:does not match|stale|without the required|does not run as written|not accept|rejected|wrong|fails)"
                        r"|[Dd]ocumented (?:import path|command|install command|package name)[^.]{0,60}(?:does not|doesn't|not)"
                        r"|does not match the documented|(?:Change|Update|Correct|Fix) (?:the )?(?:[\w/]+/)?README"),
    ("undeclared-dependency", r"[Dd]eclare (?:and install )?[\w.'\"`-]+ (?:in|as)[^.]{0,60}dependenc"
                              r"|declare '?[\w-]+'? in (?:requirements|pyproject|setup)|absent from the installed environment"
                              r"|dependency named \S+ is a placeholder|which is not among (?:the )?installed dependencies"),
    ("stale-test", r"stale tests?\b|\btests?/[\w/.:-]+[^.]{0,80}\b(?:to match|to account for)"
                   r"|test_[\w.]+::[\w:]+[^.]{0,60}(?:to match|to account for)"),
    ("uncaught-error-exit", r"exits? with (?:status|code) 1 (?:and prints|instead of)|uncaught[- ](?:error|exception|\w+Error)|stack trace"
                            r"|clean (?:error message|process exit)|without a traceback"),
)


def need(value):
    c.require(value, "invalid or unsupported Coldpass source; nothing imported")


def optional_text(value):
    need(value is None or isinstance(value, str) and "\x00" not in value)
    return value


def normalize(row):
    """Map one Coldpass run result to the fields Coldfix uses.

    This is the only place that knows the run-result field names. When the
    shared run-result schema lands, change this function and nothing else.
    """
    need(isinstance(row, dict))
    for key in ("run_id", "date", "repo", "model", "outcome"):
        c.text(row[key], key, single_line=True)
    c.occurred(row["date"])
    need(row["outcome"] in OUTCOMES)
    fault = row["fault"]
    need(fault is None or isinstance(fault, dict))
    return dict(run_id=row["run_id"], occurred=row["date"], repo=row["repo"], model=row["model"],
                outcome=row["outcome"], purpose=optional_text(row["purpose"]),
                step=optional_text(row["stuck_step"]), commit=optional_text(row["commit"]),
                diagnostic=optional_text(row["what_failed"]) or optional_text(row["error_excerpt"]),
                text="\n".join(filter(None, (optional_text(row[key]) for key in ("what_failed", "error_excerpt", "suggested_fix")))),
                origin=optional_text(row["failure_origin"]),
                evidence=optional_text(row["evidence_strength"]),
                owner=optional_text(fault.get("owner")) if fault else None)


def classify(run):
    """Return pass, harness, setup, inconclusive or product. Only product failures describe the repo under test."""
    if run["outcome"] == "pass":
        return "pass"
    if run["outcome"] in HARNESS_OUTCOMES or run["owner"] == "test":
        return "harness"
    if run["outcome"] in SETUP_OUTCOMES or run["owner"] == "prerequisite":
        return "setup"
    if run["owner"] == "repo" or run["evidence"] in CHECKED and run["outcome"] == "blocked":
        return "product"
    return "inconclusive"


def failure_class(run):
    """Return the failure class named by the run's own evidence, else "not-repo" when it blames no repo bug, else None."""
    text = run["text"].replace(BOILERPLATE, "")
    named = next((name for name, pattern in CLASSES if re.search(pattern, text, re.I)), None)
    return named or ("not-repo" if NOT_REPO.search(text) else None)


def event(run, failure_class):
    diagnostic = run["diagnostic"] or "outcome: " + run["outcome"]
    metadata = dict(type="coldpass", run_id=run["run_id"], repo=run["repo"], commit=run["commit"], failure_class=failure_class,
                    diagnostic=diagnostic[:EXCERPT], log_tail=run["text"][:EXCERPT])
    return dict(kind="test", command="coldpass " + (failure_class or "unclassified"), error=c.encoded(metadata).strip(),
                repo=run["repo"], agent=run["model"], thread="",
                source_ref="coldpass:run:" + quote(run["run_id"], safe="") + ":" + (failure_class or "unclassified"),
                occurred=run["occurred"])


def collect(data, on_reject=None):
    records, buckets, noise, classes, seen = [], Counter(), Counter(), {}, set()
    for number, raw in enumerate(data.split(b"\n"), 1):
        if not raw.strip():
            continue
        try:
            run = normalize(json.loads(raw.decode("utf-8"), object_pairs_hook=c.unique_object))
            need(run["run_id"] not in seen)
            bucket = classify(run)
            name = failure_class(run) if bucket == "product" else None
            record = fi.record(event(run, name)) if bucket == "product" and name != "not-repo" else None
        except (KeyError, ValueError, RecursionError):
            if on_reject is None:
                raise c.ColdfixError("invalid Coldpass row " + str(number)) from None
            on_reject("invalid Coldpass row")
            continue
        seen.add(run["run_id"])
        buckets[bucket] += 1
        if bucket in ("harness", "setup", "inconclusive"):
            noise[bucket + ":" + run["outcome"]] += 1
        elif name == "not-repo":
            noise["product:not-repo"] += 1
        elif record is not None:
            entry = classes.setdefault(name or "unclassified", dict(runs=0, repos=set()))
            entry["runs"] += 1
            entry["repos"].add(run["repo"])
            records.append(record)
    records.sort(key=lambda row: (c.time_key(row["occurred"]), row["source_ref"]))
    receipt = dict(source="coldpass-runs", input_sha256=hashlib.sha256(data).hexdigest(), runs=len(seen),
                   buckets=dict(sorted(buckets.items())), noise=dict(sorted(noise.items())), records=len(records),
                   classes={name: dict(runs=entry["runs"], repos=sorted(entry["repos"])) for name, entry in sorted(classes.items())})
    return records, receipt


def main(argv=None):
    parser = c.Parser(description=__doc__)
    parser.add_argument("--input", required=True, help="Coldpass runs.jsonl, opened read-only")
    parser.add_argument("--store", default=".coldfix", help="private .coldfix directory")
    parser.add_argument("--dry-run", action="store_true", help="classify and count; write nothing")
    args = parser.parse_args(argv)
    try:
        source = c.ordinary_path(args.input)
        need(source.is_file())
        root = c.ordinary_path(args.store)
        need(root.name == ".coldfix")
        need(not root.exists() or root.is_dir() and root.stat().st_mode & 0o077 == 0)
        records, summary = collect(source.read_bytes())
        summary["dry_run"] = args.dry_run
        if not args.dry_run:
            root.mkdir(parents=True, mode=0o700, exist_ok=True)
            summary.update(fi.import_records(root, records))
        print(json.dumps(summary, sort_keys=True))
        return 0
    except (c.ColdfixError, KeyError, TypeError, ValueError, OverflowError, RecursionError, OSError, UnicodeError):
        print("coldpass_sources: invalid input, unsupported source, or local I/O failure; nothing imported", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
