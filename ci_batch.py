#!/usr/bin/env python3
"""Run one bounded batch of historical GitHub CI failures that have a later green run through ingest, draft and replay."""
import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
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

LEDGER = "backlog-batches.json"
PAIRS = learn_daily.BACKLOG_PAIRS
WINDOW = timedelta(days=7)
DIAGNOSTIC = re.compile(r"(?:FAIL|ERROR): test\w*")
RUN_ID = re.compile(r"/actions/runs/(\d+)")
ZERO_STREAK = 3
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
    reasons = Counter()
    for row in mine.values():
        for attempt in row.get("replay_attempts", []):
            if attempt.get("failure") in ids or attempt.get("failure") is None:
                reasons[short(attempt["reason"])] += 1
    return dict(
        drafted=len(mine), draft_pass=sum(row["verdict"] == "PASS" for row in mine.values()),
        proven=sum(proved_here(store, row, ids) for row in mine.values()),
        rejected=sum(row.get("replay", {}).get("status") == "rejected" for row in mine.values()),
        unittest_diagnostics=sum(bool(DIAGNOSTIC.search(store["failures"][ref]["error"])) for ref in ids),
        reject_reasons=dict(reasons.most_common(10)))


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
    named = DIAGNOSTIC.search(record["error"]) or regression_adapter.PYTEST_ID.search(record["error"])
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
        batch = api(f"repos/{repo}/pulls/{number}/comments?per_page=100&page={page}", missing="HTTP 404") or []
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


def refetch_flagged_lines(pairs, api):
    found = 0
    for pair in pairs:
        finding = learn_replay.FINDING.fullmatch(str(pair.get("finding_url", "")))
        if not finding or "path" in pair:
            continue
        try:
            comment = api(f"repos/{finding[1]}/pulls/comments/{finding[3]}", missing="HTTP 404")
            pair.update(flagged_lines(comment or {}, pair.get("before")) or dict(path=None))
        except failure_ingest.SOURCE_ERRORS:
            continue
        found += isinstance(pair["path"], str)
    return found


def review_findings(ledger, repos, size, api=api):
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
                for comment in review_comments(repo, pull["number"], api):
                    user = comment.get("user") or {}
                    before = comment.get("original_commit_id")
                    if (comment.get("in_reply_to_id") or user.get("type") != "User" or user.get("login") == author
                            or not before or before == head or not (comment.get("body") or "").strip()):
                        continue
                    if before not in compares:
                        compares[before] = compare(repo, before, head, api)
                    earlier = [row["sha"] for row in compares[before].get("commits", [])
                               if row["commit"]["committer"]["date"] <= comment["created_at"]]
                    before = earlier[-1] if earlier and earlier[-1] != head else before
                    if before not in compares:
                        compares[before] = compare(repo, before, head, api)
                    diff = compares[before]
                    if (diff.get("status") != "ahead" or not adds_test(diff) or only_tests(diff, True)
                            or comment.get("path") not in {row.get("filename") for row in diff.get("files", [])}):
                        continue
                    pairs.append(dict(repo=repo, before=before, after=head, finding_url=comment["html_url"],
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
    reviews, findings, pulls = review_findings(ledger, repos, size, api) if public is not None else ([], [], 0)
    picked, considered = select(ledger, repos, size, batch, api, known)
    ids, errors, unfetched, screened, kept = [], Counter(), [], Counter(), []
    for row in picked:
        try:
            diff = compare(row["repo"], row["pair"]["before"], row["pair"]["after"], api)
            reason = screen(diff)
            if not reason:
                attempt = api(f"repos/{row['repo']}/actions/runs/{row['id']}")["run_attempt"]
                record = fetch(failure_ingest.repo_name(row["repo"]), row["id"], attempt)
                reason = screen(diff, record)
            if reason:
                screened[reason] += 1
                ledger["seen"][str(row["id"])]["screened"] = reason
                continue
            failure_ingest.import_records(root, [record])
            ids.append(record["id"])
            kept.append(row["pair"])
        except failure_ingest.SOURCE_ERRORS as error:
            errors[str(error)] += 1
            unfetched.append(str(row["id"]))
    tries = ledger.setdefault("fetch_failures", {})
    for number in unfetched:
        tries[number] = tries.get(number, 0) + 1
        if tries[number] < FETCH_TRIES:
            del ledger["seen"][number]
    pairs_path = root / PAIRS
    pairs = json.loads(c.read_text(pairs_path)) if pairs_path.exists() else dict(results=[])
    refetched = refetch_flagged_lines(pairs["results"], api) if public is not None else 0
    pairs["results"] += kept + reviews
    c.atomic_create(pairs_path, c.encoded(pairs), replace=True)
    entry = dict(batch=batch, considered=considered, fix_pairs=len(picked), ingested=len(ids),
                 screened=dict(screened), ingest_errors=dict(errors), learned=False)
    if public is not None:
        learn_collect.import_reviews(root, findings)
        urls = {row["source_ref"] for row in findings}
        ids += [ref for ref, row in c.load_store(root)[1]["failures"].items() if row.get("source_ref") in urls]
        entry.update(pulls_examined=pulls, review_pairs=len(reviews), flagged_refetched=refetched)
    entry["ids"] = sorted(set(ids))
    return entry


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


def learn(root, entry, owner, ask, timeout, reserved_rules=None, public=None):
    stalled, summary = 0, dict(candidates=0)
    while stalled < DRAFT_TRIES:
        before = learn_daily_ledger(root)
        day = f"backlog-{entry['batch']}-{len(before['runs'])}"
        with c.held_store(root):
            if public is not None:
                allowed = {repo_key(name) for name in public}
                failures = c.load_store(root)[1]["failures"]
                for key in [key for key, row in failures.items() if repo_key(row["repo"]) not in allowed]:
                    del failures[key]
            summary = learn_daily.run(root, day, ask=cheap_only(ask), reserved_rules=reserved_rules)
        c.require("error" not in summary, "drafting failed: " + str(summary.get("error")))
        if not summary.get("candidates"):
            break
        grew = len(learn_daily_ledger(root)["drafts"]) > len(before["drafts"])
        stalled = 0 if grew else stalled + 1
    merge_evidence(root, set(entry["ids"]))
    with learn_replay.replay_isolation.source_workspace(root, ".backlog-sources-") as workspace, \
            tempfile.TemporaryDirectory(prefix=".backlog-projects-", dir=root) as empty:
        sources = learn_replay.SourceProjects(Path(empty), workspace, owner, timeout, public or ())
        replay = learn_replay.run_in_workspace(root, learn_daily_ledger(root), sources, root / PAIRS, timeout, history=False, unbounded=True)
    entry.update(replay=replay, learned=True, undrafted=summary.get("candidates", 0), **measure(root, set(entry["ids"])))


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


def run(root, owner, size, *, api=api, fetch=failure_ingest.fetch, ask=learn_daily.ask, timeout=300, known=frozenset(), reserved_rules=None, public=None, now=None):
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
    if not ledger["batches"] or ledger["batches"][-1]["learned"]:
        fresh = ingest(root, ledger, owner, size, api, fetch, known, public)
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
        ledger["batches"].append(fresh)
        save()
    entry = ledger["batches"][-1]
    if not entry["learned"]:
        learn(root, entry, owner, ask, timeout, reserved_rules, public)
    lines = [journal(entry)]
    streak = next((index for index, row in enumerate(reversed(ledger["batches"])) if row.get("proven")), len(ledger["batches"]))
    if streak >= ZERO_STREAK:
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
    parser.add_argument("--size", type=int, default=100)
    parser.add_argument("--skip-store", help="store whose already-collected runs are not selected again")
    parser.add_argument("--optout", help="Coldpass opt-out register; listed repositories are never learned from")
    args = parser.parse_args(argv)
    c.require(0 < args.size <= 200, "batch size must be between 1 and 200")
    c.require(not args.repos or args.optout, "--repos needs the Coldpass --optout register")
    root = c.ordinary_path(args.store)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    live = c.ordinary_path(args.skip_store) if args.skip_store else root
    with c.lesson_lock(live, name=".learn-daily.lock"), c.lesson_lock(root, name=".backlog.lock"):
        reserved = learn_daily.peer_rules(args.skip_store)
        public = public_repos(args.repos, optout=args.optout) if args.repos else None
        for line in run(root, args.owner, args.size, known=known_runs(args.skip_store), reserved_rules=reserved, public=public):
            print(line, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
