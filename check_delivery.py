#!/usr/bin/env python3
"""Deliver replay-proven CI lessons as checks in their source repository. A lesson whose proving test is on the
default branch is recorded as delivered; its rule reaches agents there through `coldfix agents`. A generated proving test goes to a private repository in a batched pull
request built in a fresh clone, and is merged only when that repository's required checks are green. The proving test is
the one named in the lesson's diagnostic, found at its recorded path or, when it moved, anywhere on the default branch.
A lesson in an archived repository is retired. With --issues, each lesson still waiting 24 hours after it was proven gets
one issue in that private repository, closed once the lesson is delivered or retired. Prints one JSON summary line; the
stall check reads its `overdue` count, which leaves out retired and tracked lessons."""
import argparse
import ast
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import quote

import coldfix as c
from failure_ingest import LOG_COLOR
from learn_daily import LEDGER
import learn_replay
import regression_adapter
import replay_repos

RECORDS = "check-deliveries.json"
CLONE_URL = "https://github.com/{}.git"
OVERDUE = timedelta(hours=24)
STALE = timedelta(days=7)
SETTLED = ("delivered", "retired")
CHECK_ORIGIN = {"existing-test": "already_enforced_by_fix", "bot-pr": "added_by_coldfix"}
DEF = re.compile(r"\s*(?:async\s+)?def (test\w*)")
RED = {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}
GREEN = {"SUCCESS", "NEUTRAL", "SKIPPED"}
PROVING_TEST = re.compile(r"(?:(?:FAIL|ERROR): |(?P<pytest>FAILED )[^\s:]+\.py::(?:(?P<owner>[A-Za-z_]\w*)::)?)"
                          r"(?P<test>test\w*)")


class Unavailable(Exception):
    pass


def run(command, cwd=None):
    try:
        return subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=300,
                              env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
    except (OSError, subprocess.SubprocessError) as error:
        raise Unavailable(command[0] + " did not run: " + type(error).__name__) from None


def gh(*args):
    return run(["gh", *args])


def done(result, what):
    if result.returncode:
        raise Unavailable(what + " failed: " + result.stderr.strip()[-200:])
    return result.stdout


def gh_json(*args):
    return json.loads(done(gh(*args), "gh " + " ".join(args[:2])))


def git(*args):
    return done(run(["git", *map(str, args)]), "git")


def records(root):
    path = root / RECORDS
    return json.loads(c.read_text(path)) if path.exists() else {}


DELIVERED_FIELDS = ("title", "rule", "failure", "fix", "check_script", "check_command")


def transcript_candidates(root, store, moved):
    path = root / learn_replay.TRANSCRIPT_REPLAY
    if not path.exists():
        return
    routes = json.loads(c.read_text(path), object_pairs_hook=c.unique_object)
    c.require(isinstance(routes, dict), "invalid transcript replay ledger")
    for ref, route in routes.items():
        if not isinstance(route, dict) or route.get("status") != "proven":
            continue
        receipt_id = route.get("receipt_id")
        receipt = store["receipts"].get(receipt_id) if isinstance(receipt_id, str) else None
        if not receipt or receipt["status"] != "PASS" or (receipt.get("reversed") or {}).get("status") != "FAIL":
            continue
        lesson = store["lessons"].get(receipt["lesson_revision"])
        if (not lesson or lesson["lesson_id"] != route.get("lesson_id") or lesson["receipt"] != receipt_id
                or lesson["verification"] != "proven" or ref not in lesson["evidence"]):
            continue
        failure = store["failures"][ref]
        source_ref = failure.get("source_ref", "")
        if (failure["command"] != "transcript correction" or not source_ref.startswith("transcript:")
                or (lesson.get("admission") or {}).get("source_ref") != source_ref):
            continue
        repo = moved.get(ref) or failure["repo"]
        if replay_repos.SLUG.fullmatch(repo):
            yield lesson, {}, repo


def proven(root, store, ledger):
    moved = replay_repos.load(root)
    latest = c.latest_lessons(store)
    invalidations = c.read_invalidations(root)
    found, failed = {}, set()
    candidates = []
    for entry in ledger["drafts"].values():
        state = entry.get("replay", {})
        if state.get("status") in ("proven", "delivered"):
            candidates.append((store["lessons"][state["proof"]["id"]], entry, None))
    candidates.extend(transcript_candidates(root, store, moved))
    for lesson, entry, transcript_repo in candidates:
        state = entry.get("replay", {})
        current = latest[lesson["lesson_id"]]
        if (current["status"] in c.ENDED or current["verification"] != "proven"
                or invalidations.get(current["lesson_id"]) == current["id"]
                or any(current[key] != lesson[key] for key in DELIVERED_FIELDS)):
            continue
        receipt = store["receipts"][lesson["receipt"]]
        suite = state.get("proof", {}).get("full_suite")
        if (suite or {}).get("status") == "FAIL":
            failed.add((lesson["lesson_id"], receipt["before"]["commit"], receipt["after"]["commit"]))
            continue
        repo = transcript_repo
        if repo is None:
            slugs = [moved.get(ref) or store["failures"][ref]["repo"] for ref in lesson["evidence"]]
            checkout = Path(state["repo"]).name.casefold()
            repo = next((slug for slug in slugs if slug.split("/")[-1].casefold() == checkout), slugs[0])
        elif lesson["lesson_id"] in found:
            continue
        try:
            payload = regression_adapter.payload(lesson["check_script"])
        except ValueError:
            payload = {}
        found[lesson["lesson_id"]] = dict(
            lesson_id=lesson["lesson_id"], id=lesson["id"], created=lesson["created"], title=lesson["title"],
            rule=lesson["rule"], failure=lesson["failure"], fix=lesson["fix"], payload=payload,
            cause=entry.get("draft", {}).get("cause", ""), prevention=entry.get("draft", {}).get("prevention", ""),
            full_suite=suite, drafter=entry.get("drafter", ""),
            scope=c.lesson_scope(lesson, store), before=receipt["before"]["commit"], after=receipt["after"]["commit"],
            repo=repo, from_transcript=transcript_repo is not None)
    return [row for row in found.values() if (row["lesson_id"], row["before"], row["after"]) not in failed]


def waiting(reason, **extra):
    return dict(state="waiting", reason=reason, **extra)


def repository(slug, repositories):
    if slug not in repositories:
        repositories[slug] = gh_json("repo", "view", slug, "--json", "visibility,isArchived,defaultBranchRef")
    return repositories[slug]


def require_private(slug):
    if repository(slug, {})["visibility"] != "PRIVATE":
        raise Unavailable(slug + " is no longer private, so Coldfix will not push or open a pull request there")


def required_checks(slug, info):
    branch = quote(info["defaultBranchRef"]["name"], safe="")
    required = gh("api", f"repos/{slug}/branches/{branch}/protection/required_status_checks", "--jq", ".contexts[]")
    if required.returncode and "HTTP 404" in required.stderr:
        return set()
    return set(done(required, "required checks lookup").splitlines()) - {""}


def required_rollup(slug, info, rollup):
    names = required_checks(slug, info)
    found = [item for item in rollup if (item.get("name") or item.get("context")) in names]
    missing = names - {item.get("name") or item.get("context") for item in found}
    return found + [dict(name=name, state="PENDING") for name in sorted(missing)]


def unwritable(slug, info):
    if info["isArchived"]:
        return "repository is archived"
    if info["visibility"] != "PRIVATE":
        return "public repository needs the owner's approval"
    if not required_checks(slug, info):
        return "repository has no required checks"
    return None


def file_at(slug, path, commit):
    result = gh("api", "-H", "Accept: application/vnd.github.raw", f"repos/{slug}/contents/{quote(path)}?ref={commit}")
    if result.returncode and "HTTP 404" in result.stderr:
        return None
    return done(result, "file lookup")


def on_default_branch(slug, branch, path, proving):
    head = done(gh("api", f"repos/{slug}/commits/{quote(branch, safe='')}", "--jq", ".sha"), "default branch lookup").strip()
    try:
        tree = ast.parse(file_at(slug, path, head) or "")
    except (SyntaxError, ValueError):
        return None
    if not proving["pytest"]:
        nodes = ast.walk(tree)
    elif proving["owner"]:
        nodes = [node for owner in tree.body if isinstance(owner, ast.ClassDef) and owner.name == proving["owner"] for node in owner.body]
    else:
        nodes = tree.body
    defined = any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == proving["test"] for node in nodes)
    return head if defined else None


def defined_tests(root, slug, branch, clones):
    if slug not in clones:
        with tempfile.TemporaryDirectory(prefix=".check-delivery-", dir=root) as work:
            clone = Path(work) / "clone"
            git("clone", "--quiet", "--depth", "1", "--branch", branch, "--", CLONE_URL.format(slug), clone)
            found = run(["git", "-C", str(clone), "grep", "-I", "-E", r"^\s*(async\s+)?def test\w*", "--", "*.py"])
        if found.returncode not in (0, 1):
            raise Unavailable("git grep failed: " + found.stderr.strip()[-200:])
        index = {}
        for line in found.stdout.splitlines():
            path, _, text = line.partition(":")
            index.setdefault(DEF.match(text)[1], set()).add(path)
        clones[slug] = index
    return clones[slug]


def moved(root, slug, branch, proving, clones):
    found = [(commit, path) for path in sorted(defined_tests(root, slug, branch, clones).get(proving["test"], ()))
             for commit in [on_default_branch(slug, branch, path, proving)] if commit]
    return found[0] if len(found) == 1 else (None, None)


def generated_source(payload):
    path, patch = payload["path"], payload.get("patch", "")
    if "/" in path or not path.startswith("test_coldfix_proof_"):
        return None
    added = [line[1:] for line in patch.partition("\n@@")[2].split("\n")[1:] if line.startswith("+")]
    source = "\n".join(added) + "\n"
    return source if learn_replay.new_file_patch(path, source) == patch else None


def verdict(rollup):
    results = [(item.get("conclusion") or item.get("state") or "").upper() for item in rollup]
    if RED.intersection(results):
        return "red"
    return "green" if "SUCCESS" in results and GREEN.issuperset(results) else "pending"


def lesson_path(key):
    return "tests/test_coldfix_lesson_" + key[:12] + ".py"


def pull_request_body(lessons):
    lines = ["Coldfix proved these lessons by replay. Each regression test failed before its fix and passed after it.", ""]
    for lesson in lessons:
        key = lesson["lesson_id"]
        lines.append(f'- `{lesson_path(key)}` ({key}): {lesson["rule"]} '
                     f'(red `{lesson["before"][:12]}`, green `{lesson["after"][:12]}`)')
    builders = sorted({lesson["drafter"] for lesson in lessons if lesson.get("drafter")})
    return "\n".join([*lines, "", *(f"Builder-Model: {model}" for model in builders), "", "## Claim", "",
                      "Test-only regression pins for fixes already on main. No bug-fix claim; "
                      "a base-fail repro is not expected.", "", "## Testing", "",
                      "The repository checks run the added tests. Coldfix requires green checks before merging.",
                      "Later additions cite their replay commits in the commit message.",
                      "", "## Risk", "", "Normal. Adds regression tests.", "",
                      "## How to revert", "", "Revert the squash commit.", ""])


PR_FIELDS = "number,state,url,headRefName,headRefOid,baseRefName,isCrossRepository,changedFiles,files"
LESSON_REF = re.compile(r"coldfix/lesson-[0-9a-f]{12}")


def open_lesson_prs(slug):
    return [pr for pr in gh_json("pr", "list", "--repo", slug, "--state", "open", "--limit", "1000", "--json", PR_FIELDS)
            if LESSON_REF.fullmatch(pr["headRefName"]) and not pr["isCrossRepository"]]


def pr_record(pr, key, batch):
    return dict(state="open", route="bot-pr", pr=pr["number"], url=pr["url"],
                head=pr["headRefOid"], path=lesson_path(key), batch=batch)


def clone_branch(slug, branch, work, expected=None):
    clone = Path(work) / "clone"
    git("clone", "--quiet", "--depth", "1", "--branch", branch, "--", CLONE_URL.format(slug), clone)
    if expected and git("-C", clone, "rev-parse", "HEAD").strip() != expected:
        raise Unavailable("lesson pull request head moved")
    return clone


def push_tests(clone, slug, head, paths, title, expected):
    identity = gh_json("api", f"repos/{slug}/commits?committer=web-flow&per_page=1", "--jq", ".[0].commit.committer")
    if not identity:
        raise Unavailable("repository has no GitHub automation commit to supply its identity")
    git("-C", clone, "add", "--", *paths)
    git("-C", clone, "-c", "user.name=" + identity["name"], "-c", "user.email=" + identity["email"],
        "commit", "--quiet", "-m", title)
    sha = git("-C", clone, "rev-parse", "HEAD").strip()
    require_private(slug)
    if expected is None:
        tree = git("-C", clone, "rev-parse", "HEAD^{tree}").strip()
        for branch in (head, "coldfix/lesson-" + tree[:12]):
            ref = "refs/heads/" + branch
            if not git("-C", clone, "ls-remote", "origin", ref):
                head = branch
                break
            git("-C", clone, "fetch", "--quiet", "--depth", "1", "origin", ref)
            if git("-C", clone, "rev-parse", "FETCH_HEAD^{tree}").strip() == tree:
                return git("-C", clone, "rev-parse", "FETCH_HEAD").strip(), branch
        else:
            raise Unavailable("existing lesson branches have different files")
    ref = "refs/heads/" + head
    git("-C", clone, "push", "--quiet", "--force-with-lease=" + ref + ":" + (expected or ""),
        "origin", "HEAD:" + ref)
    return sha, head


def lesson_branch(lesson):
    key = lesson["lesson_id"]
    if lesson.get("retry_closed"):
        key = hashlib.sha256(("batch:" + key).encode()).hexdigest()
    return "coldfix/lesson-" + key[:12]


def publish(root, slug, base, candidates, sources, pr, known_empty=False):
    rows, members = {}, {}
    if pr:
        if pr["changedFiles"] != len(pr["files"]):
            raise Unavailable("lesson pull request file list is incomplete")
        by_path = {lesson_path(key): key for key in sources}
        for file in pr["files"]:
            path = file["path"]
            key = by_path.get(path)
            if (pr["baseRefName"] != base or key is None or file.get("deletions", 0)
                    or file_at(slug, path, pr["headRefOid"]) != sources[key]):
                break
            members[key] = sources[key]
        else:
            if members:
                rows.update({key: pr_record(pr, key, len(members) > 1) for key in members})
        if not rows and not (known_empty and not pr["files"] and pr["baseRefName"] == base):
            return {key: waiting("a pull request on the lesson branch is not the lesson test",
                                 route="bot-pr", url=pr["url"]) for key in candidates}
    pending = {}
    for key in candidates:
        if key in rows:
            continue
        if not pr:
            head = lesson_branch(candidates[key])
            prior = {row["state"]: row for row in gh_json("pr", "list", "--repo", slug, "--head", head,
                                                        "--state", "all", "--json", PR_FIELDS)}
            if "OPEN" in prior:
                raise Unavailable("a lesson pull request opened during delivery")
            if "MERGED" in prior:
                rows[key] = pr_record(prior["MERGED"], key, False)
                continue
            if "CLOSED" in prior:
                rows[key] = waiting("bot pull request was closed unmerged", route="bot-pr", url=prior["CLOSED"]["url"])
                continue
        pending[key] = sources[key]
    if not pending:
        return rows
    head = pr["headRefName"] if pr else lesson_branch(candidates[next(iter(pending))])
    expected = pr["headRefOid"] if pr else None
    title = "test: add Coldfix lesson regression tests"
    with tempfile.TemporaryDirectory(prefix=".check-delivery-", dir=root) as work:
        clone = clone_branch(slug, head if pr else base, work, expected)
        added = []
        for key, source in pending.items():
            path = lesson_path(key)
            target = c.ordinary_path(clone / path)
            if not target.parent.is_dir() or target.exists():
                rows[key] = waiting("repository has no free tests/ path for the lesson test")
                continue
            target.write_text(source, encoding="utf-8")
            members[key] = source
            added.append(path)
        if not added:
            return rows
        message = title
        if pr:
            message += "\n\n" + "\n".join(
                f'{lesson_path(key)}: red {candidates[key]["before"]}, green {candidates[key]["after"]}'
                for key in pending if lesson_path(key) in added)
        sha, head = push_tests(clone, slug, head, added, message, expected)
        if pr:
            pr = dict(pr, headRefOid=sha)
        else:
            body = Path(work) / "body.md"
            body.write_text(pull_request_body([candidates[key] for key in members]), encoding="utf-8")
            require_private(slug)
            url = done(gh("pr", "create", "--repo", slug, "--base", base, "--head", head, "--title", title,
                          "--body-file", str(body)), "gh pr create").strip().split("\n")[-1]
            pr = dict(number=int(url.rsplit("/", 1)[1]), url=url, headRefOid=sha)
    rows.update({key: pr_record(pr, key, known_empty or len(members) > 1) for key in members})
    return rows


def check_output(slug, head, name):
    runs = gh_json("api", f"repos/{slug}/commits/{head}/check-runs?check_name={quote(name)}&filter=latest&per_page=100")
    return "\n".join(f'{(run.get("output") or {}).get("summary") or ""}\n{(run.get("output") or {}).get("text") or ""}'
                     for run in runs["check_runs"] if run.get("conclusion") == "failure")


def failed_paths(slug, rollup, paths, head):
    failed = set()
    for check in rollup:
        conclusion = (check.get("conclusion") or "").upper()
        if conclusion in {"CANCELLED", "TIMED_OUT"}:
            print(f"check_delivery {slug}: skipped {conclusion.lower()} check {check.get('name')}: "
                  "not a lesson test failure", file=sys.stderr)
            continue
        if conclusion not in {"FAILURE", "ERROR"}:
            continue
        job = re.fullmatch(r"https://github\.com/" + re.escape(slug) + r"/actions/runs/\d+/job/(\d+)",
                           check.get("detailsUrl") or "", re.I)
        if job:
            output = done(gh("run", "view", "--repo", slug, "--job", job[1], "--log-failed"), "failed check log")
            failure = r"(?m)(?:^|\s)(?:FAILED|ERROR|FAIL):? .*\b{}\b"
        else:
            output = check_output(slug, head, check["name"])
            failure = r"could not discover unittest cases in \S*\b{0}\.py\b|\b{0}\.py::[\w.]+: base=\d+ head=[1-9]\d* \(regression\)"
        output = LOG_COLOR.sub("", output)
        for path in paths:
            if re.search(failure.format(re.escape(Path(path).stem)), output):
                failed.add(path)
    return failed


def obsolete_members(members, lessons):
    return {key for key, row in members.items() if key not in lessons or row["revision"] != lessons[key]["id"]}


def pull_status(slug, number):
    return gh_json("pr", "view", str(number), "--repo", slug, "--json",
                   "state,headRefOid,headRefName,baseRefName,mergeCommit,mergeStateStatus,statusCheckRollup,createdAt")


def settle(root, members, lessons, write, now, pr=None, failed=None):
    record = next(iter(members.values()))
    number, slug = str(record["pr"]), record["repo"]
    info = repository(slug, {})
    pr = pr or pull_status(slug, number)
    if pr["state"] == "MERGED":
        for key, row in members.items():
            if key in lessons:
                proving = PROVING_TEST.fullmatch(lessons[key]["payload"]["diagnostic"])
                if on_default_branch(slug, info["defaultBranchRef"]["name"], row["path"], proving):
                    members[key] = dict(row, state="delivered", commit=pr["mergeCommit"]["oid"])
            else:
                members[key] = waiting("lesson is no longer proven", route="bot-pr", url=row["url"])
        return members
    reason = "bot pull request was closed unmerged"
    if pr["state"] == "OPEN":
        if not write or any(row["head"] != pr["headRefOid"] for row in members.values()):
            return members
        batch = any(row.get("batch") for row in members.values())
        on_default = pr["baseRefName"] == info["defaultBranchRef"]["name"]
        if batch and not on_default:
            return members
        blocked = unwritable(slug, info)
        required = required_rollup(slug, info, pr["statusCheckRollup"])
        checks = verdict(required)
        obsolete = obsolete_members(members, lessons)
        if not blocked and on_default and (obsolete or batch and checks == "red"):
            rejected = {key: "lesson replay proof changed" for key in obsolete}
            if checks == "red":
                if failed is None:
                    failed = failed_paths(slug, required, [row["path"] for row in members.values()], pr["headRefOid"])
                for key, row in members.items():
                    if row["path"] in failed:
                        rejected[key] = "repository checks failed on the lesson test"
            if not rejected:
                return members
            head = pr["headRefName"]
            if not LESSON_REF.fullmatch(head):
                raise Unavailable("unexpected lesson pull request branch")
            paths = sorted(members[key]["path"] for key in rejected)
            with tempfile.TemporaryDirectory(prefix=".check-delivery-", dir=root) as work:
                clone = clone_branch(slug, head, work, pr["headRefOid"])
                for path in paths:
                    c.ordinary_path(clone / path).unlink()
                sha, _ = push_tests(clone, slug, head, paths, "test: drop Coldfix lesson tests", pr["headRefOid"])
            return {key: waiting(rejected[key], route="bot-pr", url=row["url"], rejected_revision=row["revision"],
                                 pr=row["pr"], head=sha) if key in rejected else dict(row, head=sha)
                    for key, row in members.items()}
        if obsolete:
            return members
        if (not batch and (not blocked or blocked == "repository has no required checks")
                and now - c.time_key(pr["createdAt"]) >= STALE):
            reason = "bot pull request made no progress for 7 days"
            comment = "This lesson pull request made no progress for 7 days, so Coldfix is closing it without merging."
        elif blocked or not on_default:
            return members
        elif checks == "red":
            reason = "repository checks failed on the lesson test"
            comment = "A check failed on the lesson test, so Coldfix is closing this without merging."
        else:
            if checks == "green" and pr["mergeStateStatus"] in ("CLEAN", "UNSTABLE"):
                done(gh("pr", "merge", number, "--repo", slug, "--squash", "--delete-branch",
                        "--match-head-commit", record["head"]), "gh pr merge")
            return members
        done(gh("pr", "close", number, "--repo", slug, "--delete-branch", "--comment", comment), "gh pr close")
    return {key: dict(waiting(reason, route="bot-pr", url=row["url"]),
                      **({"rejected_revision": row["revision"]} if row.get("batch") else {}))
            for key, row in members.items()}


def attempt(root, lesson, write, repositories, clones, previous):
    payload = lesson["payload"]
    if lesson["scope"] == "global":
        return waiting("cross-repo lesson needs a Coldverify check", route="coldverify")
    test = PROVING_TEST.fullmatch(payload.get("diagnostic", ""))
    if not payload.get("path") or not test:
        return waiting("lesson has no test to deliver")
    slug = lesson["repo"].casefold()
    info = repository(slug, repositories)
    branch = (info["defaultBranchRef"] or {}).get("name")
    if not branch:
        return waiting("repository has no default branch")
    commit = on_default_branch(slug, branch, payload["path"], test)
    if commit:
        return dict(state="delivered", route="existing-test", commit=commit, path=payload["path"])
    commit, path = moved(root, slug, branch, test, clones)
    if commit:
        return dict(state="delivered", route="existing-test", commit=commit, path=path)
    source = generated_source(payload)
    if info["isArchived"]:
        return dict(state="retired", reason="repository is archived")
    if source is None:
        return waiting("proving test is missing from the default branch")
    rejected = previous.get("rejected_revision")
    if rejected == lesson["id"]:
        return {key: value for key, value in previous.items() if key != "day"}
    if previous.get("route") == "bot-pr" and not rejected and previous.get("reason") in (
            "bot pull request was closed unmerged", "repository checks failed on the lesson test",
            "bot pull request made no progress for 7 days"):
        return waiting("bot pull request was closed unmerged", route="bot-pr", url=previous["url"])
    blocked = unwritable(slug, info) or (None if write else "bot pull requests are not enabled")
    if blocked:
        if previous.get("retry_closed"):
            return waiting(blocked, retry_closed=True)
        return waiting(blocked)
    return dict(state="candidate", source=source)


def issue_body(lesson, row):
    test = PROVING_TEST.fullmatch(lesson["payload"].get("diagnostic", ""))
    step = ("add a Coldverify check for this lesson class; Coldverify owns cross-repo checks."
            if row.get("route") == "coldverify" else "clear the reason above, or retire the lesson: "
            "`coldfix lesson --revise <its latest revision id> --status retired`.")
    return "\n".join([
        "Coldfix proved this lesson by replay, but cannot deliver it as a check without the owner.",
        "",
        "- **Lesson:** " + lesson["rule"],
        "- **Repository:** " + lesson["repo"],
        "- **Proving test:** `" + (test["test"] if test else "none") + "` in `" + str(lesson["payload"].get("path")) + "`",
        "- **Waiting because:** " + row["reason"],
        "- **Next step:** " + step,
        "",
        "Retry at once with `learning-service deliver-checks " + lesson["lesson_id"] + "`. Coldfix closes this issue "
        "when the lesson is delivered, retired or no longer proven; closing it by hand only makes Coldfix file it again.",
    ])


def tracked(row):
    return bool(row.get("issue")) and not row.get("issue_closed")


def untracked(row):
    return {name: value for name, value in row.items() if name not in ("issue", "issue_repo", "issue_closed")}


def track(lesson, row, now, issues, repositories):
    if tracked(row):
        number, repo = str(row["issue"]), row["issue_repo"]
        if lesson is None or row["state"] in SETTLED:
            done(gh("issue", "close", number, "--repo", repo, "--comment", "The lesson is " +
                    (row["state"] if lesson else "no longer proven") + ", so Coldfix is closing this."), "gh issue close")
            return dict(row, issue_closed=True)
        if done(gh("issue", "view", number, "--repo", repo, "--json", "state", "--jq", ".state"),
                "gh issue view").strip() == "OPEN":
            return row
        row = untracked(row)
    if lesson is None or row["state"] != "waiting" or now - c.time_key(lesson["created"]) < OVERDUE:
        return row
    if repository(issues, repositories)["visibility"] != "PRIVATE":
        raise Unavailable("issue repository " + issues + " is not private")
    title = "Lesson waiting on the owner: " + lesson["lesson_id"] + " in " + lesson["repo"]
    listed = gh_json("issue", "list", "--repo", issues, "--state", "open", "--search", lesson["lesson_id"] + " in:title",
                     "--json", "number,title")
    number = next((issue["number"] for issue in listed if issue["title"] == title), None)
    if number is None:
        url = done(gh("issue", "create", "--repo", issues, "--title", title, "--body", issue_body(lesson, row)),
                   "gh issue create").strip().split("\n")[-1]
        number = int(url.rsplit("/", 1)[1])
    return dict(untracked(row), issue=number, issue_repo=issues)


def origins(lessons, rows, since=None):
    checks = Counter(CHECK_ORIGIN[row["route"]] for row in rows.values() if row.get("state") == "delivered"
                     and (since is None or c.time_key(row["at"]) >= since))
    return dict(already_enforced_by_fix=checks["already_enforced_by_fix"], added_by_coldfix=checks["added_by_coldfix"],
                prose_only=sum(rows.get(lesson["lesson_id"], {}).get("state") != "delivered" for lesson in lessons))


def deliver(root, lessons, now, write=False, only=None, issues=None):
    lessons = [lesson for lesson in lessons if not lesson.get("from_transcript")]
    path = root / RECORDS
    rows = records(root)
    today = now.date().isoformat()
    repositories, clones, errors = {}, {}, 0
    by_id = {lesson["lesson_id"]: lesson for lesson in lessons}
    candidates = defaultdict(dict)
    sources_by_repo = defaultdict(dict)

    def save(changes):
        for key, row in changes.items():
            old = rows.get(key, {})
            lesson = by_id.get(key)
            if row == old:
                continue
            if row["state"] == "open" and old.get("batch") and old.get("pr") == row["pr"]:
                row = dict(row, batch=True)
            revision, repo = (lesson["id"], lesson["repo"]) if lesson else (old["revision"], old["repo"])
            new = dict(row, revision=revision, repo=repo,
                       **{name: old[name] for name in ("issue", "issue_repo", "issue_closed") if name in old})
            if new["state"] == "delivered":
                new["at"] = now.isoformat()
            if new["state"] == "waiting":
                new["day"] = today
            rows[key] = new
        c.atomic_create(path, c.encoded(rows), replace=True)

    def report(key, error):
        nonlocal errors
        errors += 1
        print("check_delivery " + key + ": " + str(error), file=sys.stderr)

    recoverable = (Unavailable, c.ColdfixError, OSError, ValueError, KeyError, TypeError)
    for key, lesson in by_id.items():
        old = rows.get(key, {})
        state = old.get("state")
        if state == "open" or only not in (None, key) or state in SETTLED:
            continue
        if state == "waiting":
            resumed = old["reason"] == "bot pull requests are not enabled"
            replaced = old["reason"] == "lesson replay proof changed"
            if old["day"] == today and not only and not (write and (resumed or replaced)):
                continue
        try:
            new = attempt(root, lesson, write, repositories, clones, old)
            if new["state"] == "candidate":
                slug = lesson["repo"].casefold()
                candidates[slug][key] = dict(lesson, retry_closed=old.get("retry_closed", False))
                sources_by_repo[slug][key] = new["source"]
            else:
                save({key: new})
        except recoverable as error:
            report(key, error)

    opened = defaultdict(dict)
    for key, row in rows.items():
        if row["state"] == "open":
            opened[row["repo"].casefold(), row["pr"]][key] = row

    updated = set()
    checked = {}
    for slug, pending in candidates.items():
        try:
            prs = open_lesson_prs(slug)
            if len(prs) > 1:
                raise Unavailable("multiple open lesson pull requests need consolidation")
            pr = prs[0] if prs else None
            sources = sources_by_repo[slug]
            if pr:
                ref = (slug, pr["number"])
                members = opened.get(ref, {})
                obsolete = obsolete_members(members, by_id)
                if obsolete or any(row.get("batch") for row in members.values()):
                    status = pull_status(slug, pr["number"])
                    failed = None
                    required = required_rollup(slug, repositories[slug], status["statusCheckRollup"])
                    if status["headRefOid"] == pr["headRefOid"] and verdict(required) == "red":
                        failed = failed_paths(slug, required, [row["path"] for row in members.values()],
                                              status["headRefOid"])
                    checked[ref] = (status, failed)
                    if failed or obsolete:
                        continue
                paths = {file["path"] for file in pr["files"]}
                for key, lesson in by_id.items():
                    payload = lesson["payload"]
                    if (key not in sources and lesson["repo"].casefold() == slug and lesson_path(key) in paths
                            and lesson["scope"] != "global" and payload.get("path")
                            and PROVING_TEST.fullmatch(payload.get("diagnostic", ""))):
                        source = generated_source(payload)
                        if source is not None:
                            sources[key] = source
            known_empty = bool(pr and not pr["files"] and any(
                row.get("rejected_revision") and row["repo"].casefold() == slug
                and row.get("pr") == pr["number"] and row.get("head") == pr["headRefOid"] for row in rows.values()))
            changes = publish(root, slug, repositories[slug]["defaultBranchRef"]["name"], pending, sources, pr, known_empty)
            updated.update((slug, row["pr"]) for row in changes.values() if row["state"] == "open")
            save(changes)
        except recoverable as error:
            report(slug, error)

    for ref, members in opened.items():
        if ref in updated or (only is not None and only not in members and ref not in checked):
            continue
        try:
            save(settle(root, members, by_id, write, now, *checked.get(ref, ())))
        except recoverable as error:
            report(str(ref), error)

    if issues:
        for key, lesson in by_id.items():
            row = rows.get(key)
            if not row or only not in (None, key):
                continue
            try:
                tracked_row = track(lesson, row, now, issues, repositories)
                if tracked_row != row:
                    rows[key] = tracked_row
                    c.atomic_create(path, c.encoded(rows), replace=True)
            except recoverable as error:
                report(key, error)
    live = {lesson["lesson_id"] for lesson in lessons}
    for key, row in list(rows.items()):
        if issues and only is None and key not in live and tracked(row):
            try:
                rows[key] = track(None, row, now, issues, repositories)
            except Unavailable as error:
                errors += 1
                print("check_delivery " + key + ": " + str(error), file=sys.stderr)
                continue
            c.atomic_create(path, c.encoded(rows), replace=True)
    current = [rows.get(lesson["lesson_id"], {}) for lesson in lessons]
    states = Counter(row.get("state", "waiting") for row in current)
    return dict(proven=len(lessons), delivered=states["delivered"], open=states["open"], waiting=states["waiting"],
                retired=states["retired"], tracked=sum(map(tracked, current)),
                **origins(lessons, {lesson["lesson_id"]: row for lesson, row in zip(lessons, current)}),
                overdue=sum(row.get("state") not in SETTLED and not tracked(row)
                            and now - c.time_key(lesson["created"]) >= OVERDUE for lesson, row in zip(lessons, current)),
                errors=errors, reasons=dict(Counter(row["reason"] for row in current if row.get("state") == "waiting")))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", required=True)
    parser.add_argument("--write", action="store_true", help="open and merge pull requests in private repositories")
    parser.add_argument("--lesson", help="retry only this lesson id now")
    parser.add_argument("--issues", metavar="OWNER/REPO",
                        help="private repository for one issue per lesson that waits on the owner")
    args = parser.parse_args(argv)
    root = c.ordinary_path(args.store)
    ledger_path = root / LEDGER
    ledger = json.loads(c.read_text(ledger_path)) if ledger_path.exists() else dict(drafts={})
    with c.lesson_lock(root, name=".check-delivery.lock"):
        summary = deliver(root, proven(root, c.load_store(root)[1], ledger), datetime.now(timezone.utc), args.write,
                          args.lesson, args.issues)
    print(json.dumps(dict(check_delivery=summary), sort_keys=True))
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
