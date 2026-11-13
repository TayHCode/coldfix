#!/usr/bin/env python3
"""Deliver replay-proven CI lessons as checks in their source repository. A lesson whose proving test is on the
default branch is recorded as delivered; its rule reaches agents there through `coldfix agents`. A generated proving test goes to a private repository as a one-file pull
request built in a fresh clone, and is merged only when that repository's required checks are green. The proving test is
the one named in the lesson's diagnostic, found at its recorded path or, when it moved, anywhere on the default branch.
A lesson in an archived repository is retired. With --issues, each lesson still waiting 24 hours after it was proven gets
one issue in that private repository, closed once the lesson is delivered or retired. Prints one JSON summary line; the
stall check reads its `overdue` count, which leaves out retired and tracked lessons."""
import argparse
import ast
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import quote

import coldfix as c
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


def proven(root, store, ledger):
    moved = replay_repos.load(root)
    latest = c.latest_lessons(store)
    invalidations = c.read_invalidations(root)
    found = {}
    for entry in ledger["drafts"].values():
        state = entry.get("replay", {})
        if state.get("status") not in ("proven", "delivered"):
            continue
        lesson = store["lessons"][state["proof"]["id"]]
        current = latest[lesson["lesson_id"]]
        if (current["status"] in c.ENDED or current["verification"] != "proven"
                or invalidations.get(current["lesson_id"]) == current["id"]
                or any(current[key] != lesson[key] for key in DELIVERED_FIELDS)):
            continue
        slugs = [moved.get(ref) or store["failures"][ref]["repo"] for ref in lesson["evidence"]]
        checkout = Path(state["repo"]).name.casefold()
        receipt = store["receipts"][lesson["receipt"]]
        try:
            payload = regression_adapter.payload(lesson["check_script"])
        except ValueError:
            payload = {}
        found[lesson["lesson_id"]] = dict(
            lesson_id=lesson["lesson_id"], id=lesson["id"], created=lesson["created"], title=lesson["title"],
            rule=lesson["rule"], failure=lesson["failure"], fix=lesson["fix"], payload=payload,
            scope=c.lesson_scope(lesson, store), before=receipt["before"]["commit"], after=receipt["after"]["commit"],
            repo=next((slug for slug in slugs if slug.split("/")[-1].casefold() == checkout), slugs[0]))
    return list(found.values())


def waiting(reason, **extra):
    return dict(state="waiting", reason=reason, **extra)


def repository(slug, repositories):
    if slug not in repositories:
        repositories[slug] = gh_json("repo", "view", slug, "--json", "visibility,isArchived,defaultBranchRef")
    return repositories[slug]


def require_private(slug):
    if repository(slug, {})["visibility"] != "PRIVATE":
        raise Unavailable(slug + " is no longer private, so Coldfix will not push or open a pull request there")


def unwritable(slug, info):
    if info["isArchived"]:
        return "repository is archived"
    if info["visibility"] != "PRIVATE":
        return "public repository needs the owner's approval"
    branch = quote(info["defaultBranchRef"]["name"], safe="")
    required = gh("api", f"repos/{slug}/branches/{branch}/protection/required_status_checks", "--jq", ".contexts | length")
    if required.returncode and "HTTP 404" in required.stderr or int(done(required, "required checks lookup")) == 0:
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


def pull_request_body(lesson, path):
    return "\n".join([
        "Coldfix proved this lesson by replay. This adds its regression test so the same mistake fails this "
        "repository's checks before it can land again.",
        "",
        "- **Lesson:** " + lesson["rule"],
        "- **What went wrong:** " + lesson["failure"],
        "- **Fix that passed:** " + lesson["fix"] + " (`" + lesson["after"][:12] + "`)",
        "- **Proof:** the test fails on `" + lesson["before"][:12] + "` and passes on `" + lesson["after"][:12]
        + "` (lesson `" + lesson["lesson_id"] + "`).",
        "",
        "## Testing",
        "",
        "This pull request's own checks run the new test on the default branch. Coldfix merges it only when every "
        "check is green, and closes it when a check fails.",
        "",
        "## Risk",
        "",
        "Normal: adds one test file and changes no product code.",
        "",
        "## How to revert",
        "",
        "Delete `" + path + "`.",
        "",
    ])


def pull_request(root, slug, base, lesson, source):
    short = lesson["lesson_id"][:12]
    head, path = "coldfix/lesson-" + short, "tests/test_coldfix_lesson_" + short + ".py"
    prior = {row["state"]: row for row in gh_json("pr", "list", "--repo", slug, "--head", head, "--state", "all",
                                                    "--json", "number,state,url,headRefOid,baseRefName,files")}
    adopted = prior.get("OPEN") or prior.get("MERGED")
    if adopted:
        if adopted["state"] == "OPEN" and (adopted["baseRefName"] != base
                                           or [row["path"] for row in adopted["files"]] != [path]
                                           or file_at(slug, path, adopted["headRefOid"]) != source):
            return waiting("a pull request on the lesson branch is not the lesson test", route="bot-pr", url=adopted["url"])
        return dict(state="open", route="bot-pr", pr=adopted["number"], url=adopted["url"],
                    head=adopted["headRefOid"], path=path)
    if "CLOSED" in prior:
        return waiting("bot pull request was closed unmerged", route="bot-pr", url=prior["CLOSED"]["url"])
    title = "test: " + lesson["title"]
    with tempfile.TemporaryDirectory(prefix=".check-delivery-", dir=root) as work:
        clone = Path(work) / "clone"
        git("clone", "--quiet", "--depth", "1", "--branch", base, "--", CLONE_URL.format(slug), clone)
        target = c.ordinary_path(clone / path)
        if not target.parent.is_dir() or target.exists():
            return waiting("repository has no free tests/ path for the lesson test")
        target.write_text(source, encoding="utf-8")
        git("-C", clone, "add", "--", path)
        git("-C", clone, "-c", "user.name=Coldfix", "-c", "user.email=coldfix@users.noreply.github.com",
            "commit", "--quiet", "-m", title)
        sha = git("-C", clone, "rev-parse", "HEAD").strip()
        require_private(slug)
        git("-C", clone, "push", "--quiet", "origin", "+HEAD:refs/heads/" + head)
        body = Path(work) / "body.md"
        body.write_text(pull_request_body(lesson, path), encoding="utf-8")
        require_private(slug)
        url = done(gh("pr", "create", "--repo", slug, "--base", base, "--head", head, "--title", title,
                      "--body-file", str(body)), "gh pr create").strip().split("\n")[-1]
    return dict(state="open", route="bot-pr", pr=int(url.rsplit("/", 1)[1]), url=url, head=sha, path=path)


def settle(record, lesson, write, now):
    number, slug = str(record["pr"]), record["repo"]
    info = repository(slug, {})
    pr = gh_json("pr", "view", number, "--repo", slug, "--json",
                 "state,headRefOid,mergeCommit,mergeStateStatus,statusCheckRollup,createdAt")
    if pr["state"] == "MERGED":
        proving = PROVING_TEST.fullmatch(lesson["payload"]["diagnostic"])
        if on_default_branch(slug, info["defaultBranchRef"]["name"], record["path"], proving):
            return dict(record, state="delivered", commit=pr["mergeCommit"]["oid"])
        return record
    if pr["state"] != "OPEN":
        return waiting("bot pull request was closed unmerged", route="bot-pr", url=record["url"])
    if pr["headRefOid"] != record["head"] or not write:
        return record
    blocked = unwritable(slug, info)
    if (not blocked or blocked == "repository has no required checks") and now - c.time_key(pr["createdAt"]) >= STALE:
        done(gh("pr", "close", number, "--repo", slug, "--delete-branch", "--comment",
                "This lesson test made no progress for 7 days, so Coldfix is closing it without merging."), "gh pr close")
        return waiting("bot pull request made no progress for 7 days", route="bot-pr", url=record["url"])
    if blocked:
        return record
    checks = verdict(pr["statusCheckRollup"])
    if checks == "red":
        done(gh("pr", "close", number, "--repo", slug, "--delete-branch", "--comment",
                "A check failed on the lesson test, so Coldfix is closing this without merging."), "gh pr close")
        return waiting("repository checks failed on the lesson test", route="bot-pr", url=record["url"])
    if checks != "green" or pr["mergeStateStatus"] != "CLEAN":
        return record
    done(gh("pr", "merge", number, "--repo", slug, "--squash", "--delete-branch",
            "--match-head-commit", record["head"]), "gh pr merge")
    return record


def attempt(root, lesson, write, repositories, clones):
    payload = lesson["payload"]
    if lesson["scope"] == "global":
        return waiting("cross-repo lesson needs a Coldverify check", route="coldverify")
    test = PROVING_TEST.fullmatch(payload.get("diagnostic", ""))
    if not payload.get("path") or not test:
        return waiting("lesson has no test to deliver")
    slug = lesson["repo"]
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
    blocked = unwritable(slug, info) or (None if write else "bot pull requests are not enabled")
    if blocked:
        return waiting(blocked)
    return pull_request(root, slug, branch, lesson, source)


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
    path = root / RECORDS
    rows = records(root)
    today = now.date().isoformat()
    repositories, clones, errors = {}, {}, 0
    for lesson in lessons:
        key = lesson["lesson_id"]
        old = rows.get(key, {})
        state = old.get("state")
        if only not in (None, key):
            continue
        try:
            new = old
            if not (state in SETTLED or state == "waiting" and old["day"] == today and not only):
                new = settle(old, lesson, write, now) if state == "open" else \
                    attempt(root, lesson, write, repositories, clones)
                new = dict(new, revision=lesson["id"], repo=lesson["repo"],
                           **{name: old[name] for name in ("issue", "issue_repo", "issue_closed") if name in old})
                if new["state"] == "delivered":
                    new["at"] = now.isoformat()
                if new["state"] == "waiting":
                    new["day"] = today
                rows[key] = new
                c.atomic_create(path, c.encoded(rows), replace=True)
            if issues:
                tracked_row = track(lesson, new, now, issues, repositories)
                if tracked_row != new:
                    rows[key] = tracked_row
                    c.atomic_create(path, c.encoded(rows), replace=True)
        except (Unavailable, c.ColdfixError, OSError, ValueError, KeyError, TypeError) as error:
            errors += 1
            print("check_delivery " + key + ": " + str(error), file=sys.stderr)
            continue
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
