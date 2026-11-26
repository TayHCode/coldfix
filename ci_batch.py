#!/usr/bin/env python3
"""Run one bounded batch of historical GitHub CI failures that have a later green run through ingest, draft and replay."""
import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import math
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
from urllib.parse import quote

import coldfix as c
import failure_ingest
import learn_candidates
import learn_collect
import learn_daily
import learn_replay
import regression_adapter
import oss_replay

LEDGER = "backlog-batches.json"
PAIRS = learn_daily.BACKLOG_PAIRS
WINDOW = timedelta(days=7)
RUN_ID = re.compile(r"/actions/runs/(\d+)")
ZERO_STREAK = 3
OSS_ZERO_STREAK = 10
PAUSE_HOURS = 12
FETCH_TRIES = 3
DRAFT_TRIES = 3
LEFTOVERS = (".backlog-sources-*", ".replay-*")
PULL_PAGES = 4


def api(endpoint, tries=3, missing=None):
    for attempt in range(tries):
        try:
            return failure_ingest.gh(["api", "--hostname", "github.com", endpoint], missing=missing)
        except c.ColdfixError:
            if attempt == tries - 1:
                raise
            time.sleep(10 * (attempt + 1))


def stamp(instant):
    return instant.strftime("%Y-%m-%dT%H:%M:%SZ")


def later_green(repo, red, api=api):
    window = red["created_at"] + ".." + stamp(c.time_key(red["created_at"]) + WINDOW)
    endpoint = (f"repos/{repo}/actions/workflows/{red['workflow_id']}/runs?status=success&per_page=100"
                f"&branch={quote(red['head_branch'], safe='')}&created={quote(window, safe='')}")
    first = api(endpoint)
    for number in range(math.ceil(first["total_count"] / 100), 0, -1):
        page = first if number == 1 else api(endpoint + "&page=" + str(number))
        later = [run for run in page["workflow_runs"]
                 if run["created_at"] > red["created_at"] and run["head_sha"] != red["head_sha"]]
        if later:
            return min(later, key=lambda run: run["created_at"])
    return None


def failed_page(repo, page, api=api):
    return api(f"repos/{repo}/actions/runs?status=failure&per_page=100&page={page}")["workflow_runs"]


def pool(ledger, repos, api=api):
    rows = []
    for repo in repos:
        cursor = ledger["pages"].setdefault(repo, dict(page=0, done=False))
        fresh = []
        while not fresh and not cursor["done"]:
            cursor["page"] += 1
            runs = failed_page(repo, cursor["page"], api)
            cursor["done"] = len(runs) < 100
            listed = ledger.setdefault("listed", {}).setdefault(repo, [])
            known = {row["id"] for row in listed}
            listed.extend(dict(id=run["id"], attempt=run["run_attempt"], created_at=run["created_at"],
                               head_sha=run["head_sha"], head_branch=run["head_branch"] or "",
                               workflow_id=run["workflow_id"]) for run in runs if run["id"] not in known)
            fresh = [run for run in runs if str(run["id"]) not in ledger["seen"]]
        rows += [dict(row, repo=repo) for row in ledger.get("listed", {}).get(repo, [])
                 if str(row["id"]) not in ledger["seen"]]
    unique = {row["id"]: row for row in rows}
    return sorted(unique.values(), key=lambda row: row["created_at"], reverse=True)


def select(ledger, repos, size, batch, api=api, known=frozenset(), now=None):
    now = now or datetime.now(timezone.utc)
    pending = {number for number, row in ledger["seen"].items() if row.get("pending")}
    for cursor in ledger["pages"].values():
        if cursor["done"]:
            cursor.update(page=0, done=False)
    picked, considered = [], 0
    while considered < size:
        rows = pool(ledger, repos, api)
        if not rows and pending:
            rows = sorted({row["id"]: dict(row, repo=repo) for repo, listed in ledger.get("listed", {}).items()
                           for row in listed if repo in repos and str(row["id"]) in pending}.values(),
                          key=lambda row: (ledger["seen"][str(row["id"])]["batch"], row["created_at"]))
            pending.clear()
        if not rows:
            break
        for red in rows[:size - considered]:
            considered += 1
            if str(red["id"]) in known:
                ledger["seen"][str(red["id"])] = dict(batch=batch, pair=None, known=True)
                continue
            green = later_green(red["repo"], red, api) if red["head_branch"] else None
            pair = green and dict(repo=red["repo"], before=red["head_sha"], after=green["head_sha"],
                                  red_run=red["id"], green_run=green["id"])
            ledger["seen"][str(red["id"])] = dict(batch=batch, pair=pair)
            if not green and red["head_branch"] and c.time_key(red["created_at"]) + WINDOW > now:
                ledger["seen"][str(red["id"])]["pending"] = True
            if pair:
                picked.append(dict(red, pair=pair))
    return picked, considered


def short(reason):
    return re.sub(r"\b[0-9a-f]{32}: ", "", reason)


def measure(root, ids):
    _, store = c.load_store(root)
    drafts = learn_daily_ledger(root)["drafts"]
    mine = {key: row for key, row in drafts.items() if set(row["evidence"]) & ids}
    attempts = [attempt for row in mine.values() for attempt in row.get("replay_attempts", [])
                if attempt.get("failure") in ids or attempt.get("failure") is None]
    reasons = Counter(short(attempt["reason"]) for attempt in attempts)
    environments = learn_replay.rejection_environments(attempts)
    return dict(
        drafted=len(mine), draft_pass=sum(row["verdict"] == "PASS" for row in mine.values()),
        proven=sum(proved_here(store, row, ids) for row in mine.values()),
        rejected=sum(row.get("replay", {}).get("status") == "rejected" for row in mine.values()),
        unittest_diagnostics=sum(bool(failure_ingest.unittest_diagnostics(store["failures"][ref])) for ref in ids),
        reject_reasons=dict(reasons.most_common(10)),
        **(dict(rejection_environments=environments) if environments else {}))


def proved_here(store, draft, ids):
    replay = draft.get("replay", {})
    lesson = store["lessons"].get((replay.get("proof") or {}).get("id"), {})
    replayed = (lesson.get("admission") or {}).get("source_ref")
    return (replay.get("status") in ("proven", "delivered") and replayed is not None
            and any(store["failures"][ref].get("source_ref") == replayed for ref in ids if ref in store["failures"]))


def learn_daily_ledger(root):
    path = root / learn_daily.LEDGER
    return json.loads(c.read_text(path)) if path.exists() else dict(drafts={}, runs=[])


def known_runs(store_path):
    if not store_path or not Path(store_path).exists():
        return frozenset()
    _, store = c.load_store(c.ordinary_path(store_path))
    return frozenset(match[1] for row in store["failures"].values()
                     for match in [RUN_ID.search(row.get("source_ref") or "")] if match)


def repo_key(name):
    value = re.sub(r"^[a-z]+://", "", name.strip().casefold()).strip("/ ").removeprefix("www.").removeprefix("github.com/")
    parts = [part for part in value.split("/") if part]
    return parts[0] + "/" + parts[1].removesuffix(".git") if len(parts) >= 2 else ""


def opted_out(path):
    try:
        register = json.loads(c.read_text(path), object_pairs_hook=c.unique_object)
    except ValueError:
        raise c.ColdfixError("invalid Coldpass opt-out register") from None
    c.require(isinstance(register, dict) and register.get("schema_version") == 1 and isinstance(register.get("repos"), list)
              and all(isinstance(row, dict) and set(row) == {"repo", "date"} and isinstance(row["repo"], str)
                      and repo_key(row["repo"]) and isinstance(row["date"], str)
                      and re.fullmatch(r"\d{4}-\d{2}-\d{2}", row["date"]) for row in register["repos"]),
              "invalid Coldpass opt-out register")
    return frozenset(repo_key(row["repo"]) for row in register["repos"])


def public_repos(path, api=api, optout=None):
    rows = json.loads(c.read_text(c.ordinary_path(path)))["repos"]
    skip = opted_out(optout) if optout else frozenset()
    repos = []
    for row in rows:
        c.require(isinstance(row, dict) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*",
                                                         str(row.get("repo"))), "invalid open-source repository entry")
        if repo_key(row["repo"]) in skip:
            continue
        meta = api(f"repos/{row['repo']}", missing="HTTP 404") or {}
        if (meta.get("private") is False and meta.get("visibility") == "public" and not meta.get("archived")
                and repo_key(meta["full_name"]) not in skip):
            repos.append(meta["full_name"])
    return sorted(repos)


LICENSES = {"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause"}


def catalog_eligible(name, skip, api, now):
    meta = api(f"repos/{name}", missing="HTTP 404") or {}
    canonical = meta.get("full_name")
    if (meta.get("private") is not False or meta.get("visibility") != "public" or meta.get("archived") is not False
            or not isinstance(canonical, str) or canonical.casefold() != name or repo_key(canonical) in skip
            or (meta.get("license") or {}).get("spdx_id") not in LICENSES):
        return False
    workflows = api(f"repos/{name}/actions/workflows?per_page=1", missing="HTTP 404") or {}
    if not workflows.get("workflows"):
        return False
    pulls = api(f"repos/{name}/pulls?state=closed&sort=updated&direction=desc&per_page=100", missing="HTTP 404") or []
    return isinstance(pulls, list) and any(now - timedelta(days=90) <= c.time_key(pr["merged_at"]) <= now
                                          for pr in pulls if isinstance(pr, dict) and isinstance(pr.get("merged_at"), str))


def catalog_names(catalog, skip):
    queue = json.loads(c.read_text(catalog), object_pairs_hook=c.unique_object)
    c.require(isinstance(queue, dict), "invalid Coldpass queue")
    names = {name.casefold() for name, row in queue.items() if failure_ingest.REPO.fullmatch(name)
             and isinstance(row, dict) and isinstance(row.get("target"), dict)
             and row["target"].get("private") is False and repo_key(name) not in skip}
    return queue, names


def recheck_catalog(catalog, optout, repos, api, now):
    skip = opted_out(optout)
    _, names = catalog_names(catalog, skip)
    now = now or datetime.now(timezone.utc)
    return [repo for repo in repos if repo in names and catalog_eligible(repo, skip, api, now)]


def inspect_sample(catalog, optout, store, size, api=api, now=None):
    c.require(0 < size <= 200, "sample size must be between 1 and 200")
    now = now or datetime.now(timezone.utc)
    with c.lesson_lock(store, name=".oss-catalog.lock"):
        skip = opted_out(optout)
        queue, names = catalog_names(catalog, skip)
        path = store / "oss-catalog-seen.json"
        if path.exists():
            seen = json.loads(c.read_text(path), object_pairs_hook=c.unique_object)
        else:
            ledger = store / LEDGER
            pages = json.loads(c.read_text(ledger))["pages"] if ledger.exists() else {}
            seen = {name.casefold(): 0 for name in pages}
        sample = sorted(names, key=lambda name: (seen.get(name, -1), name))[:size]
        selected = [name for name in sample if catalog_eligible(name, skip, api, now)]
        new = sum(name not in seen for name in sample)
        seen.update(dict.fromkeys(sample, max(seen.values(), default=0) + 1))
        c.atomic_create(path, c.encoded(seen), replace=True)
        return dict(catalog=len(queue), screened=len(sample), first_seen=new, eligible=selected)


def only_tests(diff, named):
    names = [name for row in diff.get("files", []) for name in (row.get("filename", ""), row.get("previous_filename")) if name is not None]
    if len(names) >= 300:
        return False
    if named:
        return len(names) <= 1 and all(learn_replay.TEST_FILE.search(name) for name in names)
    return all(name.startswith("tests/") for name in names)


def screen(diff, record=None):
    if diff and diff.get("status") != "ahead":
        return "fix is not a descendant of the recorded failure"
    if record is None:
        return None
    named = failure_ingest.unittest_diagnostics(record) or regression_adapter.PYTEST_ID.search(record["error"])
    if not named and not failure_ingest.recorded_command(record):
        return "no recorded failing command"
    if diff and only_tests(diff, named):
        return "no change between the replay revisions for the requested paths"
    return None


def adds_test(compare):
    return any(learn_replay.TEST_FILE.search(row.get("filename", "")) and regression_adapter.TEST_NAME.search(row.get("patch") or "")
               for row in compare.get("files", []))


def review_comments(repo, number, api):
    rows, page = [], 1
    while True:
        batch = api(f"repos/{repo}/pulls/{number}/comments?per_page=100&page={page}", missing="HTTP 404")
        if not isinstance(batch, list):
            return None
        rows += batch
        if len(batch) < 100:
            return rows
        page += 1


def compare(repo, before, head, api):
    return api(f"repos/{repo}/compare/{before}...{head}", missing="HTTP 404") or {}


def flagged_lines(comment, before):
    end = comment.get("original_line")
    start = comment.get("original_start_line")
    if start is None:
        start = end
    if (before == comment.get("original_commit_id") and comment.get("side") == "RIGHT"
            and comment.get("start_side") in (None, "RIGHT")
            and type(start) is int and type(end) is int and 1 <= start <= end):
        return dict(path=comment["path"], start_line=start, end_line=end)
    return {}


def pairable(diff, comment):
    return (diff.get("status") == "ahead" and adds_test(diff) and not only_tests(diff, True)
            and comment.get("path") in {row.get("filename") for row in diff.get("files", [])})


def pull_context(repo, pull, api):
    if oss_replay.sensitive(repo):
        return "security-sensitive repository", []
    reason = oss_replay.pull_reason(pull)
    if reason:
        return reason, []
    comments = review_comments(repo, pull["number"], api)
    if comments is None:
        return "incomplete PR reviews", []
    if any(not isinstance(row, dict) or not isinstance(row.get("body"), str)
           or oss_replay.sensitive(row["body"]) for row in comments):
        return "security-related or incomplete review", []
    files = api(f"repos/{repo}/pulls/{pull['number']}/files?per_page=100", missing="HTTP 404")
    if "changed_files" in pull and (not isinstance(files, list) or len(files) != pull["changed_files"]):
        return "incomplete changed files", []
    return oss_replay.files_reason(files), comments


def pull_commits(repo, number, api):
    shas, page = set(), 1
    while True:
        batch = api(f"repos/{repo}/pulls/{number}/commits?per_page=100&page={page}", missing="HTTP 404")
        if not isinstance(batch, list):
            return None
        shas.update(row["sha"] for row in batch if isinstance(row, dict) and isinstance(row.get("sha"), str))
        if len(batch) < 100:
            return shas
        page += 1


def ci_pull(repo, row, api):
    pair = row["pair"]
    pulls = api(f"repos/{repo}/commits/{pair['after']}/pulls?per_page=100", missing="HTTP 404") or []
    if not isinstance(pulls, list) or len(pulls) != 1:
        return None
    pull = pulls[0]
    if (pull.get("merged_at") and type(pull.get("id")) is int
            and (pull.get("base", {}).get("repo") or {}).get("full_name", "").casefold() == repo.casefold()):
        red_pulls = api(f"repos/{repo}/commits/{pair['before']}/pulls?per_page=100", missing="HTTP 404") or []
        if isinstance(red_pulls, list) and len(red_pulls) == 1 and red_pulls[0].get("id") == pull["id"]:
            if pair["after"] in (pull.get("merge_commit_sha"), (pull.get("head") or {}).get("sha")):
                return pull
            if type(pull.get("number")) is int:
                commits = pull_commits(repo, pull["number"], api)
                if commits and pair["after"] in commits and pair["before"] in commits:
                    return pull
    return None


def review_findings(ledger, repos, size, api=api, skipped=None):
    skipped = Counter() if skipped is None else skipped
    quota = max(1, size // max(1, len(repos)))
    seen = set(ledger.setdefault("pulls_seen", []))
    pairs, events, examined = [], [], 0
    turn = ledger.get("pull_turn", 0) % max(1, len(repos))
    ledger["pull_turn"] = turn + 1
    for repo in repos[turn:] + repos[:turn]:
        cursor = ledger.setdefault("pull_pages", {}).setdefault(repo, dict(page=0, done=False))
        if cursor["done"]:
            cursor.update(page=0, done=False)
        taken, room, fetched = 0, min(quota, size - examined), 0
        while taken < room and not cursor["done"] and fetched < PULL_PAGES:
            fetched += 1
            pulls = api(f"repos/{repo}/pulls?state=closed&sort=created&direction=desc&per_page=50&page={cursor['page'] + 1}",
                        missing="HTTP 404") or []
            fresh = [pull for pull in pulls if f"{repo}#{pull['number']}" not in seen and pull.get("merged_at")]
            if len(fresh) <= room - taken:
                cursor.update(page=cursor["page"] + 1, done=len(pulls) < 50)
            for pull in fresh[:room - taken]:
                key = f"{repo}#{pull['number']}"
                seen.add(key)
                taken += 1
                examined += 1
                head, author = pull["head"]["sha"], (pull.get("user") or {}).get("login")
                compares = {}
                reason, comments = pull_context(repo, pull, api)
                if reason:
                    skipped[reason] += 1
                    continue
                for comment in comments:
                    user = comment.get("user") or {}
                    before = comment.get("original_commit_id")
                    if (comment.get("in_reply_to_id") or user.get("type") != "User" or user.get("login") == author
                            or not before or before == head or not (comment.get("body") or "").strip()):
                        continue
                    if before not in compares:
                        compares[before] = compare(repo, before, head, api)
                    reason = (oss_replay.evidence_reason(comment["body"], tested=adds_test(compares[before]))
                              or oss_replay.compare_reason(compares[before]))
                    if reason:
                        skipped[reason] += 1
                        continue
                    if not pairable(compares[before], comment):
                        continue
                    pairs.append(dict(repo=repo, before=before, after=head, finding_url=comment["html_url"], oss_pr=pull["number"],
                                      **flagged_lines(comment, before)))
                    events.append(c.event_record(dict(kind="review", command="PR review finding", error=comment["body"],
                                                      repo=repo, agent="oss-review", source_ref=comment["html_url"],
                                                      occurred=comment["created_at"]), "github"))
    ledger["pulls_seen"] = sorted(seen)
    return pairs, events, examined


def ingest(root, ledger, owner, size, api, fetch, known=frozenset(), public=None):
    batch = len(ledger["batches"]) + 1
    repos = public if public is not None else sorted(row["nameWithOwner"] for row in failure_ingest.gh(
        ["repo", "list", owner, "--limit", "1000", "--json", "nameWithOwner"]))
    excluded = Counter()
    reviews, findings, pulls = review_findings(ledger, repos, size, api, excluded) if public is not None else ([], [], 0)
    picked, considered = select(ledger, repos, size, batch, api, known)
    ids, errors, unfetched, screened, kept = [], Counter(), [], Counter(), []
    for row in picked:
        try:
            diff = compare(row["repo"], row["pair"]["before"], row["pair"]["after"], api)
            reason = screen(diff)
            if public is not None:
                pull = ci_pull(row["repo"], row, api)
                excluded_reason = (pull_context(row["repo"], pull, api)[0] if pull else "unlinked CI run")
                excluded_reason = excluded_reason or oss_replay.compare_reason(diff)
                if excluded_reason:
                    excluded[excluded_reason] += 1
                    continue
            if not reason:
                attempt = api(f"repos/{row['repo']}/actions/runs/{row['id']}")["run_attempt"]
                record = fetch(failure_ingest.repo_name(row["repo"]), row["id"], attempt)
                if public is not None and (excluded_reason := oss_replay.evidence_reason(record["error"], tested=adds_test(diff))):
                    excluded[excluded_reason] += 1
                    continue
                reason = screen(diff, record)
            if reason:
                screened[reason] += 1
                ledger["seen"][str(row["id"])]["screened"] = reason
                continue
            failure_ingest.import_records(root, [record])
            ids.append(record["id"])
            kept.append(dict(row["pair"], oss_failure=record["id"], oss_pr=pull["number"], tested=adds_test(diff))
                        if public is not None else row["pair"])
        except failure_ingest.SOURCE_ERRORS as error:
            errors["GitHub evidence unavailable" if public is not None else str(error)] += 1
            unfetched.append(str(row["id"]))
    tries = ledger.setdefault("fetch_failures", {})
    for number in unfetched:
        tries[number] = tries.get(number, 0) + 1
        if tries[number] < FETCH_TRIES:
            del ledger["seen"][number]
    pairs_path = root / PAIRS
    pairs = json.loads(c.read_text(pairs_path)) if pairs_path.exists() else dict(results=[])
    entry = dict(batch=batch, considered=considered, fix_pairs=len(picked), ingested=len(ids),
                 screened=dict(screened), ingest_errors=dict(errors), learned=False)
    if public is not None:
        learn_collect.import_reviews(root, findings)
        stored = {row["source_ref"]: row for row in c.load_store(root)[1]["failures"].values() if row.get("source_ref")}
        accepted = []
        for pair, finding in zip(reviews, findings, strict=True):
            original = stored[finding["source_ref"]]
            if original["error"] != finding["error"] or repo_key(original["repo"]) != repo_key(pair["repo"]):
                excluded["changed historical finding"] += 1
                continue
            ids.append(original["id"])
            accepted.append(dict(pair, oss_failure=original["id"]))
        reviews = accepted
        entry.update(pulls_examined=pulls, review_pairs=len(reviews), flagged_refetched=0,
                     oss_pairs=kept + reviews, excluded=dict(excluded))
        print(f"OSS intake: {considered} failures, {len(picked)} CI pairs, {entry['ingested']} imported, "
              f"{pulls} PRs, {len(reviews)} review pairs, excluded {json.dumps(excluded, sort_keys=True)}", flush=True)
    pairs["results"] += kept + reviews
    c.atomic_create(pairs_path, c.encoded(pairs), replace=True)
    entry["ids"] = sorted(set(ids))
    return entry


def resume_oss(root, entry, public, api):
    failures = c.load_store(root)[1]["failures"]
    allowed = {repo_key(repo) for repo in public}
    excluded = Counter(entry.get("excluded"))
    cache, accepted = {}, []
    for pair in entry.get("oss_pairs", []):
        repo, number = pair["repo"], pair["oss_pr"]
        key = repo, number
        reason = None
        if repo_key(repo) not in allowed:
            reason = "repository no longer eligible"
        else:
            if key not in cache:
                pull = api(f"repos/{repo}/pulls/{number}", missing="HTTP 404") or {}
                reason, comments = pull_context(repo, pull, api)
                cache[key] = pull, reason, {row["html_url"]: row for row in comments}
            pull, reason, comments = cache[key]
            failure = failures[pair["oss_failure"]]
            if not reason and pair.get("finding_url"):
                comment = comments.get(pair["finding_url"]) or {}
                if comment.get("body") != failure["error"] or comment.get("original_commit_id") != pair["before"]:
                    reason = "changed historical finding"
            reason = reason or oss_replay.evidence_reason(failure["error"], tested=bool(pair.get("finding_url") or pair.get("tested")))
        if reason:
            excluded[reason] += 1
        else:
            accepted.append(pair)
    if "oss_pairs" not in entry:
        excluded["unscreened historical evidence"] += len(entry["ids"])
    entry.update(oss_pairs=accepted, excluded=dict(excluded))
    print(f"OSS resume: {len(accepted)} eligible pairs, excluded {json.dumps(excluded, sort_keys=True)}", flush=True)


def screened_recurrences(root, batches, entry, public, api):
    pairs = entry.get("oss_pairs", [])
    if not pairs:
        return []
    failures = c.load_store(root)[1]["failures"]
    current = {pair["oss_failure"] for pair in pairs}
    signatures = {c.signature(failures[ref]) for ref in current}
    earlier = {ref for ref, row in failures.items() if ref not in current and c.signature(row) in signatures}
    matches = {pair["oss_failure"]: pair for batch in batches for pair in batch.get("oss_pairs", [])
               if pair["oss_failure"] in earlier}
    if not matches:
        return []
    history = dict(oss_pairs=list(matches.values()))
    resume_oss(root, history, public, api)
    return [pair["oss_failure"] for pair in history["oss_pairs"]]


def cheap_only(ask):
    drafter = None
    def guarded(model, prompt, shape, work):
        nonlocal drafter
        c.require_cheap(model["model"])
        c.require(model["model"] in learn_daily.cheap_models(),
                  "ci backlog drafts only with bulk models, not " + model["model"])
        if shape is learn_daily.DRAFTS:
            drafter = model["model"]
        else:
            c.require(model["model"] != drafter, "backlog reviewer must differ from the drafter after fallback")
        return ask(model, prompt, shape, work)
    return guarded


def replay_oss(root, ledger, projects, pairs, timeout, recurring_ids=()):
    by_failure = {pair["oss_failure"]: pair for pair in pairs}
    screened_ids = by_failure.keys() | set(recurring_ids)
    counts = Counter(proven=0, rejected=0)
    with c.lesson_lock(root), c.held_store(root):
        store = c.load_store(root)[1]
        for entry in ledger["drafts"].values():
            evidence = entry["evidence"]
            if (entry["verdict"] != "PASS" or not evidence or not set(evidence).issubset(by_failure)
                    or entry.get("replay", {}).get("status") in ("proven", "delivered")):
                continue
            for ref in evidence:
                failure, pair = store["failures"][ref], by_failure[ref]
                tried = set()
                while True:
                    previous = len(tried)
                    try:
                        repo, before, after, path, diagnostic = learn_replay.prepare(
                            failure, pair, projects, timeout, skip=tried)
                        if (path, diagnostic) in tried:
                            break
                        tried.add((path, diagnostic))
                        args = learn_replay.admission_args(store, entry, failure, repo, before, after,
                                                          path, diagnostic, timeout, pair)
                        if args.recurring:
                            members = {key for key, row in store["failures"].items() if c.signature(row) == c.signature(failure)}
                            c.require(members.issubset(screened_ids), "unscreened recurrence")
                        proof = learn_replay.incident_admission.admit(args, root, store)
                        entry["replay"] = dict(status="proven", proof=proof, repo=str(learn_replay.proven_repo(projects, repo)))
                        if failure["kind"] == "review":
                            entry["replay"]["flagged_lines"] = proof["id"]
                        counts["proven"] += 1
                        break
                    except learn_replay.PROPOSAL_ERRORS:
                        counts["rejected"] += 1
                        entry["replay"] = dict(status="rejected", reason="upstream regression test did not prove the fix")
                        if not tried or len(tried) == previous:
                            break
                if entry.get("replay", {}).get("status") == "proven":
                    break
            c.atomic_create(root / learn_daily.LEDGER, c.encoded(ledger), replace=True)
    return dict(counts)


def learn(root, entry, owner, ask, timeout, reserved_rules=None, public=None):
    screened = None
    if public is not None:
        allowed = {repo_key(name) for name in public}
        screened = [pair for pair in entry.get("oss_pairs", ()) if repo_key(pair["repo"]) in allowed]
        eligible = {pair["oss_failure"] for pair in screened}
    stalled, summary = 0, dict(candidates=0)
    while stalled < DRAFT_TRIES:
        before = learn_daily_ledger(root)
        day = f"backlog-{entry['batch']}-{len(before['runs'])}"
        with c.held_store(root):
            if public is not None:
                failures = c.load_store(root)[1]["failures"]
                for key in list(failures.keys() - eligible):
                    del failures[key]
            summary = learn_daily.run(root, day, ask=cheap_only(ask), reserved_rules=reserved_rules,
                                      **({"screened_pairs": screened} if screened is not None else {}))
        c.require("error" not in summary, "drafting failed: " + str(summary.get("error")))
        if not summary.get("candidates"):
            break
        grew = len(learn_daily_ledger(root)["drafts"]) > len(before["drafts"])
        stalled = 0 if grew else stalled + 1
    if screened is None:
        merge_evidence(root, set(entry["ids"]))
    with learn_replay.replay_isolation.source_workspace(root, ".backlog-sources-") as workspace, \
            tempfile.TemporaryDirectory(prefix=".backlog-projects-", dir=root) as empty:
        sources = learn_replay.SourceProjects(Path(empty), workspace, owner, timeout, public or ())
        ledger = learn_daily_ledger(root)
        if screened is None:
            replay = learn_replay.run_in_workspace(root, ledger, sources, root / PAIRS, timeout, history=False, unbounded=True)
        else:
            replay = replay_oss(root, ledger, sources, screened, timeout, entry.get("screened_recurrences", ()))
    entry.update(replay=replay, learned=True, undrafted=summary.get("candidates", 0), **measure(root, set(entry["ids"]) if screened is None else eligible))


def merge_evidence(root, ids):
    ledger = learn_daily_ledger(root)
    for row in learn_candidates.candidates(c.load_store(root)[1]):
        draft = ledger["drafts"].get(row["fingerprint"])
        if not draft or draft.get("replay", {}).get("status") in ("proven", "delivered"):
            continue
        new = sorted(set(row["evidence"]) & ids - set(draft["evidence"]))
        if new:
            draft["evidence"] += new
            if draft.get("replay", {}).get("status") == "rejected":
                del draft["replay"]
                draft.pop("replay_settled", None)
    c.atomic_create(root / learn_daily.LEDGER, c.encoded(ledger), replace=True)


def journal(entry):
    return (f"ci-backlog batch {entry['batch']}: considered {entry.get('considered', entry['fix_pairs'])} failures, imported {entry['ingested']} of {entry['fix_pairs']} fix pairs, "
            f"screened {sum(entry.get('screened', {}).values())} {json.dumps(entry.get('screened', {}), sort_keys=True)}, "
            f"drafted {entry['drafted']} (review PASS {entry['draft_pass']}), proven {entry['proven']}, "
            f"rejected {entry['rejected']}, undrafted {entry.get('undrafted', 0)}, reasons {json.dumps(entry['reject_reasons'], sort_keys=True)}"
            + (f"; open-source PRs examined {entry['pulls_examined']}, review pairs {entry['review_pairs']}" if "review_pairs" in entry else ""))


def oss_pause(ledger, repos):
    repo_digest = sha256(c.encoded(sorted({repo_key(repo) for repo in repos})).encode()).hexdigest()
    pause = ledger.setdefault("oss_pause", {})
    if pause.get("reason") and pause["repos"] == repo_digest:
        return "ci-backlog paused: " + pause["reason"]
    pause.pop("reason", None)
    recent = ledger["batches"][-OSS_ZERO_STREAK:]
    if (len(recent) == OSS_ZERO_STREAK and recent[0]["batch"] > pause.get("after_batch", 0)
            and all(row["learned"] and row.get("proven") == 0 for row in recent)):
        reason = (f"{OSS_ZERO_STREAK} consecutive batches proved 0 lessons "
                  f"(batches {recent[0]['batch']}-{recent[-1]['batch']})")
        pause.update(reason=reason, after_batch=recent[-1]["batch"], repos=repo_digest)
        return "ci-backlog paused: " + reason
    return None


def run(root, owner, size, *, api=api, fetch=failure_ingest.fetch, ask=learn_daily.ask, timeout=300, known=frozenset(), reserved_rules=None, public=None, now=None, catalog=None, optout=None):
    for pattern in LEFTOVERS:
        for leftover in root.glob(pattern):
            shutil.rmtree(leftover)
    path = root / LEDGER
    ledger = json.loads(c.read_text(path)) if path.exists() else dict(seen={}, pages={}, batches=[])
    save = lambda: c.atomic_create(path, c.encoded(ledger), replace=True)
    if ledger.get("paused"):
        now = now or datetime.now(timezone.utc)
        until = ledger.setdefault("resume_after", (now + timedelta(hours=PAUSE_HOURS)).isoformat())
        if now < datetime.fromisoformat(until):
            save()
            return [f"ci-backlog paused until {until}: " + ledger["paused"]]
        del ledger["paused"]
    if ledger.pop("resume_after", None):
        save()
    if catalog:
        if ledger["batches"] and not ledger["batches"][-1]["learned"]:
            entry = ledger["batches"][-1]
            public = recheck_catalog(catalog, optout, entry.get("repos", []), api, now)
        else:
            sample = inspect_sample(catalog, optout, root, size, api, now)
            public = sample["eligible"]
            print("OSS catalog: " + json.dumps(sample, sort_keys=True), flush=True)
    if public is not None and (paused := oss_pause(ledger, public)):
        save()
        return [paused]
    if not ledger["batches"] or ledger["batches"][-1]["learned"]:
        fresh = ingest(root, ledger, owner, size, api, fetch, known, public)
        if public is not None:
            ledger["last_oss_intake"] = {key: fresh.get(key, 0) for key in
                                         ("considered", "fix_pairs", "ingested", "pulls_examined", "review_pairs", "excluded")}
        if not fresh["ingested"] and fresh.get("ingest_errors") and not fresh.get("review_pairs"):
            save()
            raise c.ColdfixError("no failure in the batch could be ingested: " + json.dumps(fresh["ingest_errors"]))
        if (not fresh["fix_pairs"] and not fresh["considered"] and not fresh.get("pulls_examined")
                and not fresh.get("flagged_refetched")):
            save()
            return ["ci-backlog: no new fix pairs left in GitHub CI history"]
        if not fresh["ingested"] and not fresh.get("review_pairs") and not fresh.get("flagged_refetched"):
            save()
            return [f"ci-backlog: no new fix pairs to learn from; considered {fresh['considered']} failures, "
                    f"{fresh['fix_pairs']} fix pairs, screened {json.dumps(fresh['screened'], sort_keys=True)}"]
        if catalog:
            fresh["repos"] = public
        ledger["batches"].append(fresh)
        save()
    elif public is not None:
        resume_oss(root, ledger["batches"][-1], public, api)
        save()
    entry = ledger["batches"][-1]
    if not entry["learned"]:
        if public is not None:
            entry["screened_recurrences"] = screened_recurrences(root, ledger["batches"][:-1], entry, public, api)
        learn(root, entry, owner, ask, timeout, reserved_rules, public)
    lines = [journal(entry)]
    streak = next((index for index, row in enumerate(reversed(ledger["batches"])) if row.get("proven")), len(ledger["batches"]))
    if public is not None:
        streak = min(streak, entry["batch"] - ledger["oss_pause"].get("after_batch", 0))
    if public is not None and (paused := oss_pause(ledger, public)):
        lines.append(paused)
    elif streak >= ZERO_STREAK:
        lines.append(f"ci-backlog: {streak} batches in a row proved 0 (batches {ledger['batches'][-streak]['batch']}"
                     f"-{entry['batch']}); still running")
    save()
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--owner", help="learn from this owner's repositories")
    source.add_argument("--repos", help="JSON list of public open-source repositories to learn from, read-only")
    source.add_argument("--catalog", help="read-only Coldpass live queue catalog")
    parser.add_argument("--size", type=int, default=100)
    parser.add_argument("--skip-store", help="store whose already-collected runs are not selected again")
    parser.add_argument("--optout", help="Coldpass opt-out register; listed repositories are never learned from")
    args = parser.parse_args(argv)
    c.require(0 < args.size <= 200, "batch size must be between 1 and 200")
    c.require(not (args.repos or args.catalog) or args.optout, "OSS selection needs the Coldpass --optout register")
    root = c.ordinary_path(args.store)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    live = c.ordinary_path(args.skip_store) if args.skip_store else root
    with c.lesson_lock(live, name=".learn-daily.lock"), c.lesson_lock(root, name=".backlog.lock"):
        reserved = learn_daily.peer_rules(args.skip_store)
        public = public_repos(args.repos, optout=args.optout) if args.repos else None
        for line in run(root, args.owner, args.size, known=known_runs(args.skip_store), reserved_rules=reserved, public=public,
                        catalog=args.catalog, optout=args.optout):
            print(line, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
