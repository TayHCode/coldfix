"""Admit one verified incident as a single-evidence lesson proven by replay."""
import argparse
import ast
import hashlib
import re
import shlex
import tokenize

import coldfix as c
import failure_ingest
import regression_adapter
import replay_isolation


def reject_duplicates(store, failure, ignore_lesson_id=None, *, recurring=False, coldpass=False):
    ref = failure["source_ref"]
    c.require(sum(row.get("source_ref") == ref for row in store["failures"].values()) == 1,
            "incident is recorded more than once; a repeated import is not a recurrence")
    group = c.signature(failure)
    evidence = [row for row in store["failures"].values() if c.signature(row) == group]
    if recurring:
        c.require(len(evidence) >= 2 and all(row.get("source_ref") for row in evidence)
                  and len({row["source_ref"] for row in evidence}) == len(evidence),
                  "recurrence requires distinct recorded sources")
    elif not coldpass:
        c.require(len(evidence) == 1, "incident belongs to a recurring failure group; use the recurrence path")
    for lesson in store["lessons"].values():
        if lesson["lesson_id"] == ignore_lesson_id:
            continue
        if lesson.get("admission") and lesson["verification"] != "proven":
            continue
        covered = failure["id"] in lesson["evidence"] or lesson.get("admission", {}).get("source_ref") == ref
        c.require(not covered and all(c.signature(store["failures"][key]) != group for key in lesson["evidence"]),
                "incident already has a lesson")


def same_test(before, after, name):
    try:
        def selected(source):
            tree = ast.parse(source)
            return [ast.dump(node, include_attributes=False) for node in ast.walk(tree)
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name]
        old, new = selected(before), selected(after)
        return len(old) == len(new) == 1 and old == new
    except (SyntaxError, ValueError, UnicodeError):
        return False


def hostname_text(repo, revisions, path, test_patch, rest):
    parts = path.removesuffix(".py").split("/")
    modules = {".".join(parts[index:]) for index in range(len(parts))}
    def strip_admitted_test(match):
        if match[2].rsplit(".", 2)[0] in modules:
            return match[0][:match.start(2) - match.start()] + ")"
        return match[0]
    rest = regression_adapter.UNITTEST_ID.sub(strip_admitted_test, rest)
    try:
        sources = []
        for revision in revisions:
            try:
                sources.append(c.privacy_git(repo, "show", revision + ":" + path).decode("utf-8"))
            except c.ColdfixError:
                sources.append("")
        if not any(sources):
            added = test_patch.split("\n@@", 1)[-1].split("\n")[1:]
            sources = ["".join(line[1:] + "\n" for line in added if line.startswith("+"))]
        literals = [regression_adapter.literal_text(source) for source in sources]
    except (UnicodeError, SyntaxError, tokenize.TokenError):
        return "\n".join([rest, test_patch])
    return "\n".join([rest, regression_adapter.diff_headers(test_patch), *literals])


NO_PYTEST_ASSERTION = "recorded pytest failure has no assertion message"


def pytest_messages(failure, diagnostic):
    import flakiness
    log = flakiness.metadata(failure).get("log_tail") or failure["error"]
    prefix = diagnostic + " - "
    return [re.sub(r"\.\.\.$", "", line.split(prefix, 1)[1].rstrip()) for line in log.splitlines() if prefix in line]


def is_assertion(message):
    return bool(re.match(r"(?:AssertionError|assert)\b.", message))


def admit(args, root, store):
    failure = store["failures"].get(args.failure)
    c.require(failure is not None and failure["version"] == 2 and failure.get("source_ref"),
              "admission requires a recorded version 2 failure with a source reference")
    coldpass = getattr(args, 'coldpass_pair', None)
    if coldpass is not None:
        import coldpass_replay
        import flakiness
        c.require(flakiness.metadata(failure).get('type') == 'coldpass'
                  and coldpass_replay.matches(failure, coldpass), 'identity_mismatch')
        coldpass_replay.validate_screen(coldpass, args.coldpass_entry, args.coldpass_script)
    named = re.fullmatch(r"(?:FAIL|ERROR): test\w*", args.diagnostic)
    observed = re.findall(r"(?:FAIL|ERROR): test\w*", failure["error"]) if named else []
    if coldpass is not None:
        c.require(args.diagnostic == regression_adapter.payload(args.coldpass_script)["diagnostic"], "wrong_failure")
    elif failure.get("kind") == "review":
        c.require(named, "a review finding is proven only by a named unittest from its fix")
    elif failure.get("command") == "transcript correction":
        c.require(named and getattr(args, "test_patch", None), "a transcript correction is proven only by a generated named unittest")
    else:
        c.require(args.diagnostic in observed if named else args.diagnostic in failure["error"],
                  "diagnostic is missing from the recorded failure")
    c.require_cause_level(args.rule)
    recurring = getattr(args, "recurring", False)
    superseded = next((row for row in c.latest_lessons(store).values()
                       if coldpass is not None and row['version'] == 6
                       and row['evidence'] == [failure['id']]), None)
    reject_duplicates(store, failure, superseded['lesson_id'] if superseded else None,
                      recurring=recurring, coldpass=coldpass is not None)
    evidence = sorted(row["id"] for row in store["failures"].values()
                      if c.signature(row) == c.signature(failure)) if recurring else [failure["id"]]
    if coldpass is not None:
        evidence = [failure["id"]]
    repo = c.ordinary_path(args.repo)
    c.require(args.timeout is None or c.math.isfinite(args.timeout) and args.timeout > 0, "timeout must be finite and positive")
    path = c.text(args.test_path, "test path", single_line=True)
    argv = getattr(args, "recorded_argv", None)
    if coldpass is not None:
        script = args.coldpass_script
        expected = args.coldpass_entry["draft"]["expected_failure"]
        test_patch = regression_adapter.payload(script)["patch"]
        digest = coldpass_replay.reviewed_check(coldpass)[2]
    elif argv:
        import flakiness
        entrypoint = regression_adapter.CANONICAL_VERIFY
        if argv == ["./.fleet/verify.sh"]:
            entrypoints = {c.privacy_git(repo, "show", revision + ":.fleet/verify.sh") for revision in (args.before, args.after)}
            c.require(len(entrypoints) == 1 and entrypoints <= set(regression_adapter.VERIFY_RECIPES),
                      "recorded verify entrypoint test inputs cannot be pinned")
            entrypoint = entrypoints.pop()
        else:
            c.require(argv in (["python3", "-m", "unittest", "discover", "-s", "tests"],
                               ["python", "-m", "unittest", "discover", "-s", "tests"]),
                      "recorded command test inputs cannot be pinned")
        metadata = flakiness.metadata(failure)
        recorded = failure_ingest.recorded_command(failure)
        c.require(recorded is not None and shlex.split(recorded) == argv, "recorded command is missing from the receipt")
        log = metadata.get("log_tail") or metadata.get("excerpt") or ""
        markers = [line for line in failure_ingest.log_lines(log)
                   if line.startswith(("FAIL:", "ERROR:", "FAILED (")) and line != failure_ingest.VERIFY_FAILED]
        c.require(markers, "recorded failure has no replayable diagnostic")
        marker = markers[0]
        trees = [c.privacy_git(repo, "rev-parse", "--verify", revision + ":tests").decode("ascii").strip()
                 for revision in (args.before, args.after)]
        c.require(trees[0] == trees[1], "recorded command tests changed between revisions")
        files = c.privacy_git(repo, "ls-tree", "-r", args.before, "--", "tests")
        c.require(files and all(not line.startswith(b"120000 ") for line in files.splitlines()),
                  "recorded command test inputs contain symlinks")
        runs = 1
        if entrypoint != regression_adapter.CANONICAL_VERIFY:
            runs = sum(1 for line in files.splitlines() if re.fullmatch(rb"tests/test[^/]*\.py", line.split(b"\t", 1)[-1]))
        script, expected, digest = regression_adapter.build_command(argv, marker, trees[0], args.diagnostic,
                                                                    entrypoint, runs)
        test_patch = ""
    elif regression_adapter.PYTEST_ID.fullmatch(args.diagnostic):
        c.require(regression_adapter.PYTEST_ID.fullmatch(args.diagnostic)[1] == path, "pytest diagnostic names another test file")
        try:
            sources = [c.privacy_git(repo, "show", revision + ":" + path) for revision in (args.before, args.after)]
        except c.ColdfixError:
            raise c.ColdfixError("pytest test file is missing at a replay revision") from None
        c.require(sources[0] == sources[1], "pytest test changed between revisions")
        inputs = [path.split("/", 1)[0], ":(glob)**/conftest.py", "pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini"]
        c.require(not c.privacy_git(repo, "diff", "--name-only", "--end-of-options", args.before, args.after, "--", *inputs).strip(),
                  "pytest test inputs changed between revisions")
        messages = pytest_messages(failure, args.diagnostic)
        c.require(messages and is_assertion(messages[0]), NO_PYTEST_ASSERTION)
        script, expected, digest = regression_adapter.build_pytest_pair(
            path, [hashlib.sha256(source).hexdigest() for source in sources], args.diagnostic, messages[0])
        test_patch = ""
    elif getattr(args, "test_patch", None):
        test_patch = args.test_patch
        names = sorted(set(regression_adapter.TEST_NAME.findall(test_patch)))
        c.require(names == [args.diagnostic.split(": ", 1)[1]], "generated test must define exactly the diagnosed unittest")
        script, expected, digest = regression_adapter.build(path, test_patch, names, args.diagnostic)
    else:
        try:
            patch = c.repo_patch(repo, args.before, args.after, [":(literal)" + path], args.timeout)
        except c.ColdfixError as error:
            if str(error) != "no change between the replay revisions for the requested paths":
                raise
            before = c.privacy_git(repo, "rev-parse", "--verify", "--end-of-options", args.before + "^{commit}").decode("ascii").strip()
            after = c.privacy_git(repo, "rev-parse", "--verify", "--end-of-options", args.after + "^{commit}").decode("ascii").strip()
            source = c.privacy_git(repo, "show", after + ":" + path)
            c.require(source == c.privacy_git(repo, "show", before + ":" + path), "existing test changed between revisions")
            match = re.fullmatch(r"(?:FAIL|ERROR): (test\w*)", args.diagnostic)
            c.require(match is not None, "existing test diagnostic must name one unittest")
            script, expected, digest = regression_adapter.build_existing(path, hashlib.sha256(source).hexdigest(),
                                                                          match[1], args.diagnostic, exact=True)
            test_patch = ""
        else:
            match = re.fullmatch(r"(?:FAIL|ERROR): (test\w*)", args.diagnostic)
            try:
                commits = [c.privacy_git(repo, "rev-parse", "--verify", "--end-of-options", value + "^{commit}").decode("ascii").strip()
                           for value in (args.before, args.after)]
                old, new = [c.privacy_git(repo, "show", commit + ":" + path) for commit in commits]
            except c.ColdfixError:
                old, new = b"", b""
            if match and same_test(old, new, match[1]):
                hashes = [hashlib.sha256(source).hexdigest() for source in (old, new)]
                script, expected, digest = regression_adapter.build_existing_pair(path, hashes, match[1], args.diagnostic, exact=True)
                test_patch = ""
            else:
                try:
                    test_patch = patch.decode("utf-8")
                except UnicodeError:
                    raise c.ColdfixError("regression test change must be UTF-8 text") from None
                names = sorted(set(regression_adapter.TEST_NAME.findall(test_patch)))
                script, expected, digest = regression_adapter.build(path, test_patch, names, args.diagnostic)
    record = c.next_revision(superseded) if superseded else c.new_record()
    record.update(version=6 if coldpass is not None else 5 if recurring else 4, lesson_id=record["lesson_id"] if superseded else record["id"], previous=record["previous"] if superseded else "", group_id=c.signature(failure), grouping="normalized",
                  evidence=evidence, kind=args.kind, level=args.kind if args.kind != "hook" else "lint",
                  title=args.title, rule=args.rule.replace("\r\n", "\n"), failure=args.lesson_failure.replace("\r\n", "\n"),
                  fix=args.fix.replace("\r\n", "\n"), check_command="python3 {check}", check_script=script,
                  expected_failure=expected, status="proposed", verification="unproven", receipt=None,
                  scope=c.scope_for(evidence, store["failures"]),
                  admission=dict(kind="verified_coldpass_incident" if coldpass is not None else "verified_incident", source_ref=failure["source_ref"],
                                 diagnostic=args.diagnostic, test_paths=[path], test_sha256=digest,
                                 **(dict(binding_sha256=coldpass_replay.companion_key(args.coldpass_entry["coldpass_screen"]))
                                    if coldpass is not None else {})))
    c.public_text("\n".join(record[key] for key in ("title", "rule", "check_command")),
                  [store["failures"][ref] for ref in evidence])
    rest = "\n".join([path, script, args.title, args.rule, args.lesson_failure, args.fix, args.diagnostic])
    counts = c.privacy_counts("\n".join([path, test_patch, script, args.title, args.rule, args.lesson_failure, args.fix,
                                         args.diagnostic]), filename_exempt=True)
    if coldpass is not None:
        data = regression_adapter.payload(script)
        artifact_counts = coldpass_replay.privacy_counts(data['proof'],
                         {key: bytes.fromhex(value) for key, value in data['artifacts'].items()})
        for category, count in artifact_counts.items():
            counts[category] = counts.get(category, 0) + count
    if counts.get("hostname"):
        hosts = c.privacy_counts(hostname_text(repo, (args.before, args.after), path, test_patch, rest), filename_exempt=True)
        artifact_hosts = artifact_counts.get("hostname", 0) if coldpass is not None else 0
        counts = {category: count for category, count in dict(counts, hostname=hosts.get("hostname", 0) + artifact_hosts).items() if count}
    c.require(not counts, "incident cannot be exported; privacy screen blocked: " + c.json.dumps(counts, sort_keys=True))
    authored = ("version", "group_id", "evidence", "kind", "level", "title", "rule", "failure", "fix",
                "check_command", "check_script", "expected_failure", "admission")
    try:
        commits = [c.privacy_git(repo, "rev-parse", "--verify", "--end-of-options", value + "^{commit}").decode("ascii").strip()
                   for value in (args.before, args.after)]
    except c.ColdfixError:
        commits = None

    def replayed_same_revisions(row):
        if not row.get("receipt"):
            return True
        receipt = store["receipts"].get(row["receipt"]) or {}
        return commits is not None and [(receipt.get(side) or {}).get("commit") for side in ("before", "after")] == commits

    unproven = next((row for row in c.latest_lessons(store).values()
                     if row.get("admission") and row["status"] == "proposed" and row["verification"] != "proven"
                     and all(row.get(key) == record[key] for key in authored) and replayed_same_revisions(row)), None)
    if unproven is None:
        if coldpass is not None:
            coldpass_replay.save_companion(root, coldpass, args.coldpass_entry)
        c.save(root, "lessons", record)
        unproven = record
    root, store = c.load_store(root)
    replay_isolation.require_sandbox()
    executor = None if argv else replay_isolation.replay_tree
    options = dict(lesson=unproven["id"], repo=args.repo, before=args.before, after=args.after,
                   status="active", timeout=args.timeout)
    if executor:
        options['replay_executor'] = executor
    result = c.replay(argparse.Namespace(**options), root, store)
    if result["verification"] != "proven":
        root, store = c.load_store(root)
        receipt = store["receipts"][result["receipt_id"]]
        states = {side: (receipt[side] or {}).get("status") for side in ("before", "after", "reversed")}
        raise c.ColdfixError(f"incident not admitted; red {states['before']}, green {states['after']}, "
                             f"reversed-patch {states['reversed']}; expected FAIL, PASS, FAIL")
    result["test_sha256"] = digest
    return result
