"""Collect configured read-only evidence sources."""
import io
import json
import os
from pathlib import Path
import re
import subprocess
from collections import Counter
from datetime import datetime, timezone
import shlex
import uuid
import tarfile
import time
import tempfile

import coldfix as c
import coldpass_sources
import failure_ingest as fi


def stable(record):
    return {key: value for key, value in record.items() if key != "created"}


def vm02_jobs(root, data):
    from learn_collect import SETTLE_SECONDS

    total = dict(imported=0, deduped=0, rejected=0, unavailable=0, passed=0, unsettled=0)
    reasons = Counter()
    known = c.load_store(root)[1]["failures"]
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
        files, modified = {}, {}
        for member in archive:
            name = member.name.removeprefix("./")
            if member.isfile() and Path(name).name == name and name.endswith((".json", ".log")):
                if name in files:
                    raise ValueError("duplicate archive member")
                files[name] = archive.extractfile(member).read()
                modified[name] = member.mtime
        for name in sorted(n for n in files if n.endswith(".json")):
            try:
                row = json.loads(files[name], object_pairs_hook=c.unique_object)
                identifier = name[:-5]
                c.require(str(uuid.UUID(identifier)) == identifier and row["token"] == identifier, "invalid job identity")
                c.require(row["version"] == "vm02-lifecycle-v2", "invalid job version")
                c.require(row["verdict"] in ("PASS", "FAIL", "INCOMPLETE"), "invalid job verdict")
                if (not row.get("cleaned_at") or row.get("cleanup") != "gone"
                        or time.time() - max(modified[name], modified.get(identifier + ".log", 0)) < SETTLE_SECONDS):
                    total["unsettled"] += 1
                    continue
                repo, commit = (None if row.get(key) == "" else row.get(key) for key in ("repo", "commit"))
                tests = row.get("failed_tests")
                c.require(repo is None or isinstance(repo, str) and re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+", repo), "invalid job repo")
                c.require(commit is None or isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit), "invalid job commit")
                c.require(tests is None or isinstance(tests, list) and all(c.text(test, "failed test", single_line=True) for test in tests), "invalid job failed tests")
                if row["verdict"] == "PASS":
                    total["passed"] += 1
                    continue
                command = row.get("command", ["remote job"])
                c.require(isinstance(command, list) and command and all(isinstance(x, str) for x in command), "invalid job command")
                started = row["started"]
                c.require(type(started) in (int, float), "invalid job timestamp")
                log = files[identifier + ".log"].decode("utf-8", errors="replace")[-fi.TAIL:]
                identity = {key: value for key, value in (("commit", commit), ("failed_tests", tests)) if value}

                def job_record(repo, **identity):
                    error = c.encoded(dict(type="vm02-job", verdict=row["verdict"], exit=row.get("guest_exit"), log_tail=log, **identity)).strip()
                    event = dict(kind="ci" if row["verdict"] == "FAIL" else "stuck", repo=repo, agent=row["owner"],
                                 command=shlex.join(command), error=error, source_ref="vm02-job:" + c.digest(identifier),
                                 occurred=datetime.fromtimestamp(started, timezone.utc).isoformat())
                    record = c.event_record(event, "vm02-jobs")
                    record["id"] = c.digest("vm02-job\n" + identifier)[:32]
                    return record

                record, legacy = job_record(repo or "unknown", **identity), job_record("unknown")
                if stable(record) != stable(legacy) and stable(known.get(record["id"], {})) == stable(legacy):
                    total["deduped"] += 1
                    continue
                if record["id"] in known and stable(known[record["id"]]) == stable(record):
                    total["deduped"] += 1
                    continue
                result = fi.import_records(root, [record])
                total["imported"] += result["imported"]
                total["deduped"] += result["skipped"]
            except (c.ColdfixError, ValueError, TypeError, KeyError, OverflowError, OSError) as error:
                total["rejected"] += 1
                reasons[type(error).__name__] += 1
    total["rejection_reasons"] = dict(reasons)
    return total


def collect(root, config):
    from learn_collect import SETTLE_SECONDS, receipts, import_reviews

    sources = json.loads(Path(config).read_text())
    if not isinstance(sources, dict):
        raise ValueError('sources must be an object')
    results = {}
    for name, source in sources.items():
        total = dict(imported=0, deduped=0, rejected=0, unavailable=0)
        results[name] = total
        try:
            if not isinstance(source, dict) or set(source) != {'format', 'command'} or not isinstance(source['command'], list) or not source['command'] or not all(isinstance(x, str) and x for x in source['command']):
                raise ValueError('expected format and command argv')
            if source['format'] == 'cold-review-tar' and not c.fleet_cli():
                total.update(unavailable=1, reason='review receipt import is off: set COLDFIX_FLEET_CLI')
                continue
            data = subprocess.run(source['command'], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  check=True, timeout=120).stdout
            if source['format'] == 'coldbox-tar':
                with tempfile.TemporaryDirectory(dir=root) as directory:
                    with tarfile.open(fileobj=io.BytesIO(data), mode='r:*') as archive:
                        for member in archive:
                            filename = Path(member.name).name
                            if not member.isfile() or filename != member.name.removeprefix('./') or not filename.endswith(('.json', '.log')):
                                continue
                            target = Path(directory) / filename
                            with archive.extractfile(member) as stream, target.open('wb') as out:
                                out.write(stream.read())
                            os.utime(target, (member.mtime, member.mtime))
                    ledger = root / 'collected-receipts.json'
                    before = json.loads(ledger.read_text()) if ledger.exists() else {}
                    before = dict.fromkeys(before, 'imported') if isinstance(before, list) else before
                    names = {p.name for p in Path(directory).glob('*.json') if time.time() - p.stat().st_mtime >= SETTLE_SECONDS}
                    reasons = Counter()
                    result = receipts(root, directory, on_reject=lambda reason: reasons.update([reason or 'unknown error']))
                total.update(imported=result['imported'], deduped=result['skipped'] + sum(before.get(name) == 'imported' for name in names), rejected=result['rejected'], known_invalid=result['known_invalid'], rejection_reasons=dict(reasons))
            elif source['format'] == 'vm02-tar':
                results[name] = vm02_jobs(root, data)
            elif source['format'] == 'failure-json':
                value = json.loads(data, object_pairs_hook=c.unique_object)
                rows = value if isinstance(value, list) else [value]
                records = [fi.record(fi.ci_event(row) if row.get('type') == 'ci' else fi.t3_event(row)) for row in rows]
                result = fi.import_records(root, records)
                total.update(imported=result['imported'], deduped=result['skipped'])
            elif source['format'] == 'coldpass-runs':
                reasons = Counter()
                records, summary = coldpass_sources.collect(data, on_reject=lambda reason: reasons.update([reason]))
                coldpass_sources.save_rechecks(root, summary)
                result = fi.import_records(root, records)
                total.update(imported=result['imported'], deduped=result['skipped'])
                if reasons:
                    total.update(rejected=sum(reasons.values()), rejection_reasons=dict(reasons))
            elif source['format'] == 'cold-review-tar':
                from github_sources import private_store
                private_store(root)
                records = []
                with tarfile.open(fileobj=io.BytesIO(data), mode='r:*') as archive:
                    for member in archive:
                        if not member.isfile() or Path(member.name).name != member.name.removeprefix('./') or not member.name.endswith('.json'):
                            continue
                        try:
                            receipt = json.load(archive.extractfile(member), object_pairs_hook=c.unique_object)
                            if receipt.get('verdict') == 'INCOMPLETE':
                                continue
                            records.extend(c.cold_review_events(receipt))
                        except (c.ColdfixError, ValueError, TypeError, AttributeError, UnicodeError, RecursionError):
                            total['rejected'] += 1
                total.update(import_reviews(root, records))
            else:
                raise ValueError('unknown source format')
        except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError, tarfile.TarError, subprocess.SubprocessError, c.ColdfixError) as error:
            total['unavailable'] = 1
            total['reason'] = type(error).__name__
    return results
