"""Deliver replay-proven lessons on the current event; retain all other work."""
import argparse
import datetime
import json
import os
from pathlib import Path
import re
import sys
import tempfile

import coldfix as c


def lesson_ref(row):
    return "coldfix:lesson:" + row["lesson_id"] + ":" + row["revision"]


def withdraw(args, root, records, lesson_id, keep_revision=None, keep_hub=None, keys=None):
    complete = True
    for key, row in records.items():
        if (row["lesson_id"] != lesson_id or keys is not None and key not in keys or row["state"] == "withdrawn"
                or row["revision"] == keep_revision and (keep_hub is None or row["agentlocker"] == keep_hub)):
            continue
        try:
            hub = row["agentlocker"]
            target = c.ordinary_path(row["target"])
            never_installed = (row["state"] in ("pending", "withdrawing") and not c.ordinary_path(target / ".coldlocker").exists()
                               and not c.ordinary_path(target / "AGENTS.md").exists())
            row["state"] = "withdrawing"
            c.atomic_create(root / "deliveries.json", c.encoded(records), replace=True)
            listed = c.process([sys.executable, hub, "list", "--kind", "rule", "--json"])
            c.require(listed.returncode == 0, "catalog withdrawal incomplete")
            entries = json.loads(listed.stdout, object_pairs_hook=c.unique_object)
            c.require(isinstance(entries, list) and all(isinstance(entry, dict) for entry in entries), "invalid catalog listing")
            for entry in entries:
                if entry.get("name") == "lesson-" + lesson_id:
                    c.require(entry.get("owner") == "coldfix" and entry.get("origin") == "coldfix"
                              and entry.get("source") == "imported", "foreign catalog rule preserved")
                    removed = c.process([sys.executable, hub, "remove", "rule", "lesson-" + lesson_id,
                                         "--owner", "coldfix", "--yes"])
                    c.require(removed.returncode == 0, "catalog withdrawal incomplete")
            if not never_installed:
                result = c.process([sys.executable, hub, "deliver-rules", "--target", row["target"],
                                    "--rule", "lesson-" + lesson_id, "--withdraw"])
                c.require(result.returncode == 0, "owned withdrawal incomplete")
            row["state"] = "withdrawn"
            c.atomic_create(root / "deliveries.json", c.encoded(records), replace=True)
        except (c.ColdfixError, OSError, ValueError):
            complete = False
    return complete


def read_records(path, store):
    try:
        records = json.loads(c.read_text(path), object_pairs_hook=c.unique_object) if path.exists() else {}
    except (ValueError, TypeError, RecursionError):
        raise c.ColdfixError("invalid delivery records") from None
    c.require(isinstance(records, dict) and all(isinstance(row, dict)
              and set(row) == {"revision", "lesson_id", "target", "agentlocker", "export", "state", "export_hashes", "delivered_sha256"}
              and isinstance(key, str) and c.GROUP.fullmatch(key)
              and isinstance(row["revision"], str) and row["revision"] in store["lessons"]
              and isinstance(row["lesson_id"], str) and c.ID.fullmatch(row["lesson_id"])
              and store["lessons"][row["revision"]]["lesson_id"] == row["lesson_id"]
              and isinstance(row["target"], str) and key == c.digest(str(c.ordinary_path(row["target"])) + row["lesson_id"])
              and isinstance(row["agentlocker"], str) and c.ordinary_path(row["agentlocker"])
              and isinstance(row["export"], str) and c.ordinary_path(row["export"]).name == "lesson-" + row["lesson_id"] + ".md"
              and row["state"] in ("pending", "delivered", "withdrawing", "withdrawn")
              and isinstance(row["export_hashes"], list) and row["export_hashes"]
              and all(isinstance(digest, str) and c.GROUP.fullmatch(digest) for digest in row["export_hashes"])
              and isinstance(row["delivered_sha256"], str) and c.GROUP.fullmatch(row["delivered_sha256"])
              for key, row in records.items()), "invalid delivery records")
    return records


def first_activation(target, row):
    path = ".coldlocker/rules/lesson-" + row["lesson_id"] + ".md"
    paths = (path, ".coldlocker/rules.json", "AGENTS.md")
    try:
        c.require(c.privacy_git(target, "rev-parse", "--show-toplevel").decode().rstrip("\n") == str(target),
                  "delivery target is not a repository root")
        if c.privacy_git(target, "rev-parse", "--is-shallow-repository").strip() != b"false":
            return "incomplete_history"
    except (c.ColdfixError, UnicodeError):
        return "missing_target"
    try:
        if not c.ordinary_path(target / path).is_file():
            return "missing_commit"
    except c.ColdfixError:
        return "missing_commit"
    try:
        history = c.privacy_git(target, "--literal-pathspecs", "log", "--first-parent", "--diff-filter=AM",
                                "--format=%H%x00%cI", "--", *paths).decode().splitlines()
        for change in reversed(history):
            commit, at = change.split("\x00")
            present = c.privacy_git(target, "--literal-pathspecs", "ls-tree", "-r", "--name-only", "-z",
                                    commit, "--", *paths).decode().split("\x00")
            if not set(paths) <= set(present):
                continue
            content = c.privacy_git(target, "show", commit + ":" + path).decode("utf-8")
            registration = c.privacy_git(target, "show", commit + ":.coldlocker/rules.json")
            agents = c.privacy_git(target, "show", commit + ":AGENTS.md").decode("utf-8")
            try:
                registry = json.loads(registration, object_pairs_hook=c.unique_object)["lesson-" + row["lesson_id"]]
                if (c.digest(content) == row["delivered_sha256"] and isinstance(registry, dict)
                        and registry.get("pending") is False and registry.get("lesson") is True
                        and isinstance(registry.get("hashes"), list)
                        and c.digest(content.replace("\r\n", "\n")) in registry["hashes"]
                        and "- Read `" + path + "`." in agents.splitlines()):
                    return dict(lesson_revision=row["revision"], at=at, commit=commit,
                                evidence_ref=str(target) + ":" + path + "@" + commit)
            except (ValueError, KeyError, TypeError, RecursionError):
                continue
    except (c.ColdfixError, ValueError, UnicodeError):
        return "unreadable_history"
    return "missing_commit"


def activations(args, root, store):
    from measurements import validate_input
    output = c.ordinary_path(args.output)
    c.require(not root.exists() or root.stat().st_mode & 0o077 == 0, "measurement store must be private (0700)")
    records = read_records(c.ordinary_path(root / "deliveries.json"), store)
    repos = [c.ordinary_path(repo) for repo in args.repo or ()]
    found = {}
    counts = dict(not_delivered=0, ineligible=0, missing_target=0, missing_commit=0, incomplete_history=0,
                  unreadable_history=0, duplicates=0)
    for row in records.values():
        lesson = store["lessons"][row["revision"]]
        if row["state"] != "delivered":
            counts["not_delivered"] += 1
            continue
        if lesson["status"] not in c.LIVE or lesson["verification"] != "proven":
            counts["ineligible"] += 1
            continue
        target = c.ordinary_path(row["target"])
        results = [first_activation(target, row)] + [first_activation(repo, row) for repo in repos
                                                     if repo in target.parents]
        blocked = [result for result in results if result in ("incomplete_history", "unreadable_history")]
        entries = [result for result in results if isinstance(result, dict)]
        if blocked or not entries:
            counts[(blocked or results)[0]] += 1
            continue
        entry = min(entries, key=lambda e: c.time_key(e["at"]))
        previous = found.get(row["revision"])
        if previous:
            counts["duplicates"] += 1
        if previous is None or c.time_key(entry["at"]) < c.time_key(previous["at"]):
            found[row["revision"]] = entry
    data = dict(version=1, activations=sorted(found.values(), key=lambda row: row["lesson_revision"]))
    validate_input(data, store)
    for folder in reversed((output.parent, *output.parent.parents)):
        if not folder.exists():
            folder.mkdir(mode=0o700)
    c.require(output.parent.stat().st_mode & 0o077 == 0, "activation output directory must be private (0700)")
    c.atomic_create(output, c.encoded(data))
    return dict(activations=len(found), skipped=sum(counts.values()), **counts)


def confirm(path, records, key):
    row = records[key]
    destination = c.ordinary_path(row["target"]) / ".coldlocker/rules" / ("lesson-" + row["lesson_id"] + ".md")
    try:
        if c.digest(c.read_text(destination)) != row["delivered_sha256"]:
            return False
    except (c.ColdfixError, OSError, UnicodeError):
        return False
    row.update(state="delivered", export_hashes=[row["delivered_sha256"]])
    c.atomic_create(path, c.encoded(records), replace=True)
    return True


def deliver(args, root, store, requests):
    path = c.ordinary_path(root / "deliveries.json")
    records = read_records(path, store)
    delivered, pending = [], []
    for request in requests:
        proof = request["replay"]
        lesson = store["lessons"].get(proof.get("id"))
        reason, invalidated = None, False
        if lesson and lesson["status"] in c.ENDED:
            reason, invalidated = "lesson " + lesson["status"], True
        elif proof.get("status") != "PASS" or proof.get("verification") != "proven" or not lesson or lesson["verification"] != "proven" or lesson["status"] not in c.LIVE:
            reason, invalidated = "replay proof incomplete", True
        elif not lesson.get("failure", "").strip() or not lesson.get("fix", "").strip() or not lesson["rule"].strip():
            reason, invalidated = "four-part lesson record incomplete", True
        elif not all(getattr(args, key, None) for key in
                     ("agentlocker", "target", "exports", "denylist", "release_check", "release_check_repo")):
            reason = "delivery safety configuration incomplete"
        if lesson and not invalidated:
            current_hub = str(c.ordinary_path(args.agentlocker)) if getattr(args, "agentlocker", None) else None
            if not withdraw(args, root, records, lesson["lesson_id"], keep_revision=lesson["id"], keep_hub=current_hub):
                reason = "superseded owned withdrawal incomplete"
        if reason is None:
            stage = "target binding"
            try:
                target = c.ordinary_path(args.target)
                if c.lesson_scope(lesson, store) != "global":
                    receipt = store["receipts"][lesson["receipt"]]
                    rooted = c.proven_repos(lesson, store["lessons"], store["receipts"])
                    roots = c.repo_root(target, "HEAD", None)
                    c.require(rooted and receipt.get("repo_root") == rooted[0] and roots is not None
                              and not set(roots.split(",")).isdisjoint(rooted[0].split(",")),
                              "lesson target does not share its proven repository root")
                stage = "export safety"
                exports = c.ordinary_path(args.exports)
                c.require(root != exports and root not in exports.parents, "exports must be outside the private store")
                name = "lesson-" + lesson["lesson_id"]
                output = exports / (name + ".md")
                key = c.digest(str(target) + lesson["lesson_id"])
                exports.mkdir(parents=True, exist_ok=True, mode=0o700)
                with tempfile.TemporaryDirectory(prefix=".export-", dir=exports) as temporary:
                    candidate = Path(temporary) / output.name
                    export = argparse.Namespace(lesson=lesson["id"], format="coldlocker", output=str(candidate),
                        public=True, denylist=args.denylist, git_repo=[str(target)],
                        release_check=args.release_check, release_check_repo=args.release_check_repo)
                    c.export_lessons(export, root, store)
                    body = c.read_text(candidate)
                    owned_hashes = {digest for row in records.values() if row["lesson_id"] == lesson["lesson_id"] and row["export"] == str(output)
                                    for digest in row["export_hashes"]}
                    if output.exists():
                        c.require(c.digest(c.read_text(output)) in owned_hashes,
                                  "export destination changed")
                    intended = c.digest(body)
                    records[key] = dict(revision=lesson["id"], lesson_id=lesson["lesson_id"], target=str(target),
                                        agentlocker=str(c.ordinary_path(args.agentlocker)), export=str(output), state="pending",
                                        export_hashes=sorted(owned_hashes | {c.digest(body)}), delivered_sha256=intended)
                    c.atomic_create(path, c.encoded(records), replace=True)
                    c.atomic_create(output, body, replace=output.exists())
                stage = "Coldlocker delivery"
                for command in (
                    [sys.executable, args.agentlocker, "import-rule", str(output), "--name", name,
                     "--owner", "coldfix", "--origin", "coldfix", "--approve-risky"],
                    [sys.executable, args.agentlocker, "deliver-rules", "--target", str(target), "--rule", name]):
                    result = c.process(command)
                    c.require(result.returncode == 0, "Coldlocker delivery failed")
                c.require(confirm(path, records, key), "delivered content differs from export")
                delivered.append(lesson["id"])
            except (c.ColdfixError, OSError, UnicodeError, ValueError):
                reason = stage + " gate incomplete"
                invalidated = stage == "export safety"
        if reason:
            if invalidated and lesson and not withdraw(args, root, records, lesson["lesson_id"]):
                reason += "; owned withdrawal incomplete"
            pending.append(dict(request, reason=reason))
        log = c.ordinary_path(root / "delivery-events.jsonl")
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(dict(lesson=proof.get("id"), status="pending" if reason else "delivered",
                                         reason=reason)) + "\n")
    return delivered, pending


RUNS = "recheck-runs.jsonl"
MONTHLY_AFTER = 3
MONTHLY = datetime.timedelta(weeks=4) - datetime.timedelta(days=1)


def recheck_history(root):
    path = c.ordinary_path(root / RUNS)
    history = {}
    for line in c.read_text(path).split("\n") if path.exists() else ():
        try:
            run = json.loads(line)
            at = datetime.datetime.fromisoformat(run["at"])
            results = [((row["lesson"], row.get("target")), row["result"]) for row in run["lessons"]]
            c.require(at.tzinfo is not None, "naive run time")
        except (ValueError, TypeError, KeyError, AttributeError, c.ColdfixError):
            continue
        for key, result in results:
            if result != "skipped":
                streak = history.get(key, (0, at))[0]
                history[key] = (streak + 1 if result == "passed" else 0, at)
    return history


def deferral(history, now):
    def deferred(lesson, target=None):
        streak, at = history.get((lesson, target), (0, now))
        if streak >= MONTHLY_AFTER and now - at < MONTHLY:
            return f"passed {streak} rechecks in a row; next monthly check after {(at + MONTHLY).date()}"
        return None
    return deferred


def recheck(args, root, store):
    import coldrecall as recall
    now = datetime.datetime.now(datetime.timezone.utc)
    deferred = deferral(recheck_history(root), now)
    records = read_records(c.ordinary_path(root / "deliveries.json"), store)
    if args.repo:
        targets = [c.ordinary_path(recall.git(c.ordinary_path(args.repo), "rev-parse", "--show-toplevel"))]
    else:
        targets = sorted({c.ordinary_path(row["target"]) for row in records.values()})
    rows, withdrawn, complete = [], 0, True
    for repo in targets:
        checked, removed, done = recheck_repo(args, root, records, repo, deferred)
        rows += checked
        withdrawn += removed
        complete = complete and done
    if getattr(args, "lessons", None):
        import transcript_delivery
        folder = c.ordinary_path(args.lessons)
        c.require(folder.is_dir(), "lessons folder not found")
        judged = transcript_delivery.recheck(root, store, folder, transcript_delivery.env_jev(),
                                             deferred=deferred, withdraw_at=now.isoformat())
        withdrawn += sum(row["result"] == "withdrawn" for row in judged)
        rows += judged
    incomplete = not complete or any(row["result"] == "unchecked" and row["reason"] != "no witness" for row in rows)
    regressed = sum(row["result"] == "regression" for row in rows)
    value = dict(delivered=len(rows), passed=sum(row["result"] == "passed" for row in rows), withdrawn=withdrawn,
                 regression=regressed, retired=sum(row["result"] == "retired" for row in rows),
                 unchecked=sum(row["result"] == "unchecked" for row in rows), gone=sum(bool(row.get("gone")) for row in rows),
                 lessons=rows)
    fd = os.open(c.ordinary_path(root / RUNS), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(dict(value, at=now.isoformat()), sort_keys=True) + "\n")
    return dict(value, _exit=2 if incomplete or regressed else 1 if withdrawn else 0)


def recheck_repo(args, root, records, repo, deferred=lambda lesson, target: None):
    import coldrecall as recall
    mine = {key for key, row in records.items() if c.ordinary_path(row["target"]) == repo}
    unconfirmed = sorted(records[key]["lesson_id"] for key in mine
                         if records[key]["state"] == "pending"
                         and not confirm(c.ordinary_path(root / "deliveries.json"), records, key))
    delivered = {lesson_ref(records[key]): records[key]["lesson_id"] for key in mine if records[key]["state"] == "delivered"}
    notes = args.notes and (repo / ".coldlocker/rules.json").exists()
    missing = "rule not registered"
    try:
        c.require(c.ordinary_path(recall.git(repo, "rev-parse", "--show-toplevel")) == repo, "not a repository root")
        found = recall.read_sources(repo)[0] if delivered or notes else {}
        head = recall.git(repo, "rev-parse", "HEAD") if delivered else None
    except (c.ColdfixError, OSError, UnicodeError, ValueError, RecursionError):
        found, head = {}, None
        missing = "repository or rule registry unreadable" if repo.exists() else "repository gone"
    later = {ref: deferred(delivered[ref], str(repo)) for ref in delivered if ref in found}
    runs = {ref: recall.run_witness(repo, found[ref], head, args.timeout) for ref in sorted(delivered)
            if ref in found and not later[ref]}
    failed = sorted(delivered[ref] for ref in delivered if runs.get(ref, {}).get("result") == "regression")
    interrupted = {records[key]["lesson_id"] for key in mine if records[key]["state"] == "withdrawing"}
    outcomes = {lesson: withdraw(args, root, records, lesson, keys=mine) for lesson in sorted(set(failed) | interrupted)}
    rows = []
    for ref, lesson in sorted(delivered.items()):
        run = runs.get(ref)
        if run is None and later.get(ref):
            row = dict(result="skipped", reason=later[ref])
        elif run is None:
            row = dict(result="unchecked", reason=missing, **(dict(gone=[str(repo)]) if missing == "repository gone" else {}))
        elif run["result"] == "pass":
            row = dict(result="passed", reason="witness passed")
        elif run["result"] == "regression":
            row = dict(result="withdrawn") if outcomes[lesson] else dict(result="failed", reason="withdrawal incomplete")
        else:
            row = dict(result="unchecked", reason=run["detail"])
        rows.append(dict(lesson=lesson, target=str(repo), **row))
    rows += [dict(lesson=lesson, target=str(repo), result="unchecked", reason="delivery unconfirmed") for lesson in unconfirmed]
    if args.notes:
        write_notes(c.ordinary_path(args.notes), repo, found, {ref: run["result"] for ref, run in runs.items()},
                    {records[key]["lesson_id"] for key in mine if records[key]["state"] == "withdrawn"})
    return rows, sum(outcomes.values()), all(outcomes.values())


def write_notes(directory, repo, found, results, withdrawn):
    import coldrecall as recall
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        origin = recall.git(repo, "remote", "get-url", "origin")
    except c.ColdfixError:
        origin = ""
    slug = re.search(r"github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?$", origin)
    slug = slug.group(1) if slug else repo.name

    def commit(sha):
        return f"[{sha[:12]}](https://github.com/{slug}/commit/{sha})" if "/" in slug else f"`{sha}`"

    def previous(path, key):
        match = re.search(r"^" + key + r": (.+)$", c.read_text(path), re.M) if path.exists() else None
        return match.group(1) if match else None

    suffix = "-" + slug.replace("/", "-") + ".md"
    directory.mkdir(parents=True, exist_ok=True)
    for ref, fact in sorted(found.items()):
        path = directory / ("lesson-" + fact["lesson_id"] + suffix)
        title = fact["_title"]
        verdict = results.get(ref)
        checked = now if verdict in ("pass", "regression") else previous(path, "last_recheck") or "never"
        status = "withdrawn" if fact["lesson_id"] in withdrawn else "active"
        body = "\n".join([
            "---",
            "coldfix_lesson: " + json.dumps(ref),
            "title: " + json.dumps(title),
            "status: " + status,
            "scope: " + json.dumps(fact["scope"] or "unknown"),
            "repo: " + json.dumps(slug),
            "last_recheck: " + checked,
            "receipts:",
            *("  - " + json.dumps(value) for value in (fact["_replay"], *fact["evidence"])),
            "---",
            "",
            "# " + title,
            "",
            fact["text"],
            "",
            "## Proof",
            "",
            "- Replay receipt: `" + fact["_replay"] + "`",
            "- Fails before: " + commit(fact["proven"]["before"]),
            "- Passes after: " + commit(fact["proven"]["after"]),
            "- Rule file: `" + fact["source"] + "`",
            *("- Source failure: `" + value + "`" for value in fact["evidence"]),
            "",
        ])
        c.atomic_create(path, body, replace=path.exists())
    for lesson_id in withdrawn - {fact["lesson_id"] for fact in found.values()}:
        path = directory / ("lesson-" + lesson_id + suffix)
        if path.exists():
            c.atomic_create(path, re.sub(r"^status: .*$", "status: withdrawn", c.read_text(path), count=1, flags=re.M),
                            replace=True)
