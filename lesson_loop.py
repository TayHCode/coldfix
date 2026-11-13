"""One event-driven proof-gated delivery batch using the existing lesson commands."""
import argparse
import json

import coldfix as c


def run(args, root, store):
    from flakiness import detect

    c.require(args.timeout is None or c.math.isfinite(args.timeout) and args.timeout > 0,
              "timeout must be finite and positive")
    fixes = {}
    key = None
    if args.fixes:
        try:
            fixes = json.loads(c.read_text(args.fixes), object_pairs_hook=c.unique_object)
            c.require(isinstance(fixes, dict), "invalid fixes mapping")
            for key, row in fixes.items():
                c.require(c.GROUP.fullmatch(key) and isinstance(row, dict) and {"repo", "before", "after", "check_file", "expected_failure", "kind"} <= set(row)
                    <= {"repo", "before", "after", "check_file", "expected_failure", "kind", "failure", "fix", "rule"}, "invalid fix fields")
                c.require(row["kind"] in c.KINDS, "invalid fix kind")
                for field, value in row.items():
                    c.text(value, field, single_line=field not in ("failure", "fix", "rule"))
                c.ordinary_path(row["repo"])
                c.text(c.read_text(row["check_file"]), "check script")
        except (c.ColdfixError, OSError, ValueError, TypeError, RecursionError):
            affected = [lesson for lesson in c.latest_lessons(store).values()
                        if key and c.signature(store["failures"][lesson["evidence"][0]]) == key]
            if affected:
                invalidations = c.read_invalidations(root)
                for lesson in affected:
                    invalidations[lesson["lesson_id"]] = lesson["id"]
                c.atomic_create(root / "invalidations.json", c.encoded(invalidations), replace=True)
                requests = [dict(group_id=key, replay=dict(id=lesson["id"], status="INCOMPLETE",
                            verification="unproven")) for lesson in affected]
                from lesson_delivery import deliver
                _, pending = deliver(args, root, store, requests)
                pending_path = root / "pending.json"
                previous = json.loads(c.read_text(pending_path))["pending"] if pending_path.exists() else []
                c.atomic_create(pending_path, c.encoded(dict(version=1,
                    pending=[row for row in previous if row["group_id"] != key] + pending)), replace=True)
            raise c.ColdfixError("invalid fixes mapping; nothing imported; affected lessons held pending") from None
    imported = c.ingest(args, root, store)
    root, store = c.load_store(root)
    flaky = detect(store)[1]
    flaky_groups = {c.signature(store["failures"][ref]) for ref in flaky}
    grouped = c.group(args, root, store)
    groups = c.propose(argparse.Namespace(group=None, grouping="normalized", kind=None,
        title=None, rule=None, rule_file=None, check_command=None), root, store)["groups"]
    covered = {c.signature(store["failures"][ref]) for lesson in c.latest_lessons(store).values()
               for ref in lesson["evidence"]}
    invalidations = c.read_invalidations(root)
    for lesson in c.latest_lessons(store).values():
        if lesson["verification"] == "proven" and lesson["id"] != invalidations.get(lesson["lesson_id"]):
            invalidations.pop(lesson["lesson_id"], None)
    def save_invalidations():
        c.atomic_create(root / "invalidations.json", c.encoded(invalidations), replace=True)
    save_invalidations()
    requests = []
    for lesson in c.latest_lessons(store).values():
        proposal = store["proposals"].get(lesson["lesson_id"])
        key = c.signature(store["failures"][lesson["evidence"][0]])
        if key in flaky_groups and lesson["status"] not in c.ENDED:
            continue
        fix = fixes.get(key) if lesson["status"] not in c.ENDED else None
        revision_blocked = lesson["lesson_id"] in invalidations
        changed = fix and (lesson["kind"] != fix["kind"] or lesson["expected_failure"] != fix["expected_failure"]
                           or lesson.get("failure", "") != fix.get("failure", lesson.get("failure", "")).replace("\r\n", "\n")
                           or lesson.get("fix", "") != fix.get("fix", lesson.get("fix", "")).replace("\r\n", "\n")
                           or lesson["rule"] != fix.get("rule", lesson["rule"]).replace("\r\n", "\n")
                           or lesson["check_script"] != c.read_text(fix["check_file"]).replace("\r\n", "\n"))
        if fix and lesson["verification"] == "proven":
            receipt = store["receipts"][lesson["receipt"]]
            try:
                identity = ("rev-parse", "--path-format=absolute", "--git-common-dir")
                changed = changed or c.privacy_git(c.ordinary_path(fix["repo"]), *identity) != c.privacy_git(c.ordinary_path(receipt["repo"]), *identity)
                changed = changed or any(c.privacy_git(c.ordinary_path(fix["repo"]), "rev-parse", "--verify",
                    "--end-of-options", fix[side] + "^{commit}").decode("ascii").strip() != receipt[side]["commit"]
                    for side in ("before", "after"))
            except c.ColdfixError:
                changed = True
        if fix and (lesson["verification"] != "proven" or changed or revision_blocked):
            invalidations[lesson["lesson_id"]] = lesson["id"]
            save_invalidations()
            try:
                revised = c.lesson(argparse.Namespace(group=None, from_proposal=None, revise=lesson["id"],
                    kind=fix["kind"], level=fix["kind"] if fix["kind"] != "hook" else "lint", status="proposed",
                    title=None, failure=fix.get("failure"), fix=fix.get("fix"), rule=fix.get("rule"), rule_file=None, check_command="python3 {check}",
                    check_file=fix["check_file"], expected_failure=fix["expected_failure"]), root, store)
                root, store = c.load_store(root)
                lesson = store["lessons"][revised["id"]]
                result = c.replay(argparse.Namespace(lesson=lesson["id"], repo=fix["repo"],
                    before=fix["before"], after=fix["after"], status="active", timeout=args.timeout), root, store)
                root, store = c.load_store(root)
                lesson = store["lessons"][result["id"]]
                if lesson["verification"] == "proven":
                    invalidations.pop(lesson["lesson_id"], None)
                    save_invalidations()
                    revision_blocked = False
            except c.ColdfixError:
                revision_blocked = True
        receipt = store["receipts"].get(lesson["receipt"])
        proof = dict(status=receipt["status"] if receipt and not revision_blocked else "INCOMPLETE",
                     id=lesson["id"], lesson_id=lesson["lesson_id"],
                     receipt_id=lesson["receipt"], verification=lesson["verification"] if not revision_blocked else "unproven")
        requests.append(dict(proposal_id=proposal["id"] if proposal else None, group_id=key,
            title=lesson["title"], rule=lesson["rule"], kind=lesson["kind"],
            check_command=lesson["check_command"], replay=proof))
    for group in groups:
        key = group["group_id"]
        if not group["recurring"] or key in covered or key in flaky_groups:
            continue
        fix = fixes.get(key)
        existing = sorted((p for p in store["proposals"].values() if all(c.signature(store["failures"][ref]) == key for ref in p["evidence"])),
                          key=lambda p: (p["created"], p["id"]))
        if existing:
            proposal = existing[-1]
        else:
            draft = argparse.Namespace(group=key, grouping="normalized",
                kind=fix["kind"] if fix else "test", title="Prevent recurring failure " + key[:12],
                rule=fix.get("rule") if fix and fix.get("rule") else "Require a regression check for recurring failure " + key +
                     " before accepting changes.",
                rule_file=None, check_command="python3 {check}" if fix else "python3 -m unittest discover")
            try:
                result = c.propose(draft, root, store)
            except c.ColdfixError:
                requests.append(dict(proposal_id=None, group_id=key, title=draft.title,
                    rule=draft.rule, kind=draft.kind, check_command=draft.check_command,
                    replay=dict(status="INCOMPLETE", reason="Proposal validation blocked draft; retained pending")))
                continue
            root, store = c.load_store(root)
            proposal = store["proposals"][result["proposal_id"]]
        request = dict(proposal_id=proposal["id"], group_id=key, title=proposal["title"],
                       rule=proposal["rule"], kind=proposal["kind"], check_command=proposal["check_command"],
                       replay=dict(status="INCOMPLETE", reason="No authorized fix revisions and check supplied"))
        if fix:
            try:
                authored = c.lesson(argparse.Namespace(group=key, grouping="normalized", from_proposal=None, revise=None,
                    kind=fix["kind"], level=fix["kind"] if fix["kind"] != "hook" else "lint", status="proposed",
                    proposal_origin=proposal["id"], title=proposal["title"], failure=fix.get("failure"), fix=fix.get("fix"), rule=fix.get("rule") or proposal["rule"], rule_file=None, check_command="python3 {check}",
                    check_file=fix["check_file"], expected_failure=fix["expected_failure"]), root, store)
                root, store = c.load_store(root)
                result = c.replay(argparse.Namespace(lesson=authored["id"], repo=fix["repo"],
                    before=fix["before"], after=fix["after"], status="active", timeout=args.timeout), root, store)
                result.pop("_exit")
                request.update(kind=fix["kind"], check_command="python3 {check}", replay=result)
                root, store = c.load_store(root)
            except c.ColdfixError:
                request["replay"] = dict(status="INCOMPLETE", reason="Lesson validation blocked replay; retained pending")
        requests.append(request)
    from lesson_delivery import deliver
    delivered, pending = deliver(args, root, store, requests)
    c.atomic_create(root / "pending.json", c.encoded(dict(version=1, pending=pending)), replace=True)
    return dict(imported_records=imported["imported_records"], groups=grouped["groups"],
                proposals=len(requests), delivered=delivered, pending=len(pending), pending_list="pending.json")
