"""Admit one verified incident as a single-evidence lesson proven by replay."""
import argparse
import ast
import collections
import hashlib
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import tokenize

import coldfix as c
import failure_ingest
import regression_adapter
import replay_isolation


class ReplayRejected(c.ColdfixError):
    def __init__(self, message, env):
        super().__init__(message)
        self.env = env


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


def hunks(diff):
    for old_start, old_count, new_start, new_count in re.findall(
            r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", diff, flags=re.M):
        old_count, new_count = int(old_count or 1), int(new_count or 1)
        i1, j1 = int(old_start) - (old_count > 0), int(new_start) - (new_count > 0)
        yield i1, i1 + old_count, j1, j1 + new_count


def confined(hunk, start, end):
    i1, i2, j1, j2 = hunk
    return start <= i1 + 1 and i2 <= end and j2 - j1 <= i2 - i1


def flagged_revert(args, repo):
    flagged = getattr(args, "flagged", None) or dict(path=getattr(args, "flagged_path", None),
                                                     start_line=getattr(args, "flagged_start", None),
                                                     end_line=getattr(args, "flagged_end", None))
    flagged = flagged if isinstance(flagged, dict) else {}
    start, end = flagged.get("start_line"), flagged.get("end_line")
    c.require(isinstance(flagged.get("path"), str) and type(start) is int and type(end) is int and 1 <= start <= end,
              "a review finding is proven only with its flagged lines")

    def budget():
        return c.remaining_timeout(args.timeout, getattr(args, "deadline", None))

    old, new = [re.split(r"(?<=\n)(?=.)", c.privacy_git(repo, "show", revision + ":" + flagged["path"],
                                                         timeout=budget()).decode("utf-8"),
                         flags=re.S) for revision in (args.before, args.after)]
    reverted, changed, kept = [], False, 0
    for i1, i2, j1, j2 in hunks(c.privacy_git(
            repo, "diff", "-U0", "--no-color", "--no-ext-diff", "--no-textconv", args.before, args.after, "--",
            ":(literal)" + flagged["path"], timeout=budget()).decode("utf-8", "replace")):
        hit = i2 > i1 and i1 + 1 <= end and i2 >= start
        c.require(not hit or confined((i1, i2, j1, j2), start, end),
                  "the fix's edit of the finding's flagged lines also adds or changes unflagged lines")
        changed = changed or hit
        reverted += new[kept:j1] + (old[i1:i2] if hit else new[j1:j2])
        kept = j2
    reverted += new[kept:]
    c.require(changed, "the fix does not change the finding's flagged lines")
    return flagged["path"], "".join(reverted), "".join(new)


def module_in_tree(repo, revision, module, deadline, timeout):
    paths = c.privacy_git(repo, "ls-tree", "-r", "--name-only", "-z", revision,
                          timeout=c.remaining_timeout(timeout, deadline)).decode("utf-8", "replace").split("\0")
    return any(part in (module, module + ".py") for path in paths for part in path.split("/"))


def located(cause, args, repo):
    missing = re.fullmatch(r"ModuleNotFoundError: No module named '(\w+)'", cause)
    if missing and not any(module_in_tree(repo, revision, missing[1], getattr(args, "deadline", None), args.timeout)
                           for revision in (args.before, args.after)):
        return cause + " outside this repo"
    return cause


def save_flagged_receipt(root, repo, lesson, row, patch, cause):
    if row["status"] == "INCOMPLETE" and cause:
        row = dict(row, stderr="INCOMPLETE: " + cause + "\n")
    not_run = dict(commit="", tree="", status="INCOMPLETE", exit=None, stdout="", stderr="not run by the flagged-lines replay\n")
    receipt = dict(c.new_record(), version=2, lesson_revision=lesson["id"], lesson_digest=c.lesson_digest(lesson),
                   check_sha256=c.digest(lesson["check_script"]), check_command=lesson["check_command"],
                   repo=str(repo), before=not_run, after=not_run, reversed=row,
                   status=c.replay_state(not_run, not_run, row)[0], test_sha256=lesson["admission"]["test_sha256"],
                   patch_sha256=hashlib.sha256(patch.encode("utf-8")).hexdigest())
    receipt["id"] = c.receipt_id(receipt)
    c.save(root, "receipts", receipt)


def require_flagged_lines_exercised(args, root, repo, script, expected, lesson=None):
    import learn_replay

    path, reverted, new = flagged_revert(args, repo)
    patch = learn_replay.proposed_patch(path, reverted, new)
    with tempfile.TemporaryDirectory(prefix=".flagged-", dir=root) as temporary:
        scratch = Path(temporary)
        check, patch_file = scratch / "check", scratch / "flagged.patch"
        c.atomic_create(check, script)
        patch_file.write_text(patch, encoding="utf-8")
        row = replay_isolation.replay_tree(repo, args.after, scratch / "reverted", "python3 " + shlex.quote(str(check)),
                                           expected, args.timeout, patch_file, deadline=getattr(args, "deadline", None))
    if row["status"] == "INCOMPLETE":
        c.remaining_timeout(args.timeout, getattr(args, "deadline", None))
    if row["status"] != "FAIL":
        cause = located(c.incomplete_cause(row["stderr"], c.load_store(root)[1]) if "INCOMPLETE: " in row["stderr"] else "",
                        args, repo)
        if lesson is not None:
            save_flagged_receipt(root, repo, lesson, row, patch, cause)
        raise ReplayRejected(f"incident not admitted; flagged lines reverted {row['status']}; expected FAIL"
                             + (f"; {cause}" if cause else ""), row.get("env"))


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


SUITE_COMMAND = "python3 -m unittest discover -s tests"
SUITE_SCRIPT = "import os, sys\nos.execv(sys.executable, [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests'])\n"
SUITE_RAN = re.compile(r"^Ran (\d+) tests? in ", re.M)
SUITE_FAILED = re.compile(r"^(?:FAIL|ERROR): (.+)$", re.M)
SUITE_SKIPPED = re.compile(r"^(?:OK|FAILED) \(.*\bskipped=(\d+)", re.M)


def suite_run(root, repo, revision, timeout, deadline):
    with tempfile.TemporaryDirectory(prefix=".suite-", dir=root) as temporary:
        check = Path(temporary) / "suite.py"
        check.write_text(SUITE_SCRIPT, encoding="utf-8")
        row = replay_isolation.replay_tree(repo, revision, Path(temporary) / "tree", ["python3", str(check)], "",
                                           timeout, deadline=deadline)
    ran, failed = SUITE_RAN.findall(row["stderr"]), sorted(SUITE_FAILED.findall(row["stderr"]))
    if row["exit"] is None or len(ran) != 1 or int(ran[0]) == 0 or (row["exit"] == 0) == bool(failed):
        return None
    skipped = SUITE_SKIPPED.findall(row["stderr"])
    return dict(ran=int(ran[0]), failed=failed, skipped=int(skipped[-1]) if skipped else 0)


def suite_counts(run):
    return dict(ran=run["ran"], failed=len(run["failed"]), skipped=run["skipped"])


def full_suite(root, repo, before, after, timeout, deadline=None):
    try:
        fixed = suite_run(root, repo, after, timeout, deadline)
        base = suite_run(root, repo, before, timeout, deadline) if fixed and (fixed["failed"] or fixed["skipped"]) else None
    except (c.ColdfixError, OSError) as error:
        return dict(command=SUITE_COMMAND, status="INCOMPLETE", reason=str(error))
    if fixed is None or (fixed["failed"] or fixed["skipped"]) and base is None:
        return dict(command=SUITE_COMMAND, status="INCOMPLETE", reason="the suite gave no unittest result")
    if fixed["skipped"] >= fixed["ran"]:
        return dict(command=SUITE_COMMAND, status="INCOMPLETE", reason="every suite test was skipped")
    new = sorted((collections.Counter(fixed["failed"]) - collections.Counter(base["failed"] if base else ())).elements())
    newly_skipped = fixed["skipped"] > (base["skipped"] if base else 0)
    return dict(command=SUITE_COMMAND, status="FAIL" if new or newly_skipped else "PASS", new_failures=new,
                after=suite_counts(fixed), **(dict(before=suite_counts(base)) if base else {}))


def generated_controls(repo, before, after, test_path, scratch, timeout, deadline=None):
    def git(checkout, *args, input=None):
        try:
            return c.privacy_git(checkout, *args, input=input, timeout=c.remaining_timeout(timeout, deadline))
        except subprocess.TimeoutExpired:
            raise c.ColdfixError("generated replay control timed out") from None

    changes = git(repo, "diff", "--raw", "--no-abbrev", "--no-renames", "-z", before, after, "--", ".",
                  ":(exclude,literal)" + test_path).split(b"\0")
    documents, removed, added = [], [], []
    for offset in range(0, len(changes) - 1, 2):
        header, path = changes[offset:offset + 2]
        old_mode, mode, _, blob, status = header[1:].split()
        name = Path(os.fsdecode(path))
        suffix = name.suffix.lower()
        if (suffix in {".md", ".markdown", ".mdx", ".rst", ".adoc"}
                or suffix in {"", ".txt"} and name.stem.lower() in {"readme", "changelog", "contributing", "authors", "news"}
                or suffix == ".txt" and any(part.lower() in {"doc", "docs"} for part in name.parts[:-1])):
            documents.append(":(literal)" + str(name))
        if status == b"D":
            removed.append(b"0 " + b"0" * len(blob) + b"\t" + path + b"\0")
        elif status == b"A" or mode == b"120000" or old_mode[:2] != mode[:2]:
            added.append((mode, blob, path))
    if documents:
        patch = git(repo, "diff", "--no-ext-diff", "--no-textconv", "--binary", "--full-index", "--no-renames", before, after, "--", *documents)
        yield "documentation", after, patch, "PASS"
    if removed or added:
        index = scratch / "namespace"
        git(repo, "clone", "--quiet", "--shared", "--no-checkout", "--", str(repo), str(index))
        git(index, "read-tree", before)
        empty = git(index, "hash-object", "-w", "--stdin", input=b"").strip()
        entries = removed + [mode + b" " + (empty if mode.startswith(b"100") else blob) + b"\t" + path + b"\0"
                             for mode, blob, path in added]
        git(index, "update-index", "-z", "--index-info", input=b"".join(entries))
        patch = git(index, "diff", "--no-ext-diff", "--no-textconv", "--cached", "-R", "--binary", "--full-index", "--no-renames", before, "--")
        yield "file presence", before, patch, "FAIL"


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
    observed = failure_ingest.unittest_diagnostics(failure) if named else []
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
    if coldpass is None and failure.get("kind") == "review":
        require_flagged_lines_exercised(args, root, repo, script, expected, lesson=record)
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
                   status="active", timeout=args.timeout, deadline=getattr(args, "deadline", None))
    if executor:
        options['replay_executor'] = executor
    result = c.replay(argparse.Namespace(**options), root, store)
    if result["verification"] != "proven":
        root, store = c.load_store(root)
        receipt = store["receipts"][result["receipt_id"]]
        states = {side: (receipt[side] or {}).get("status") for side in ("before", "after", "reversed")}
        causes = {}
        for side, label in (("before", "red"), ("after", "green"), ("reversed", "reversed")):
            cause = located(c.incomplete_cause((receipt[side] or {}).get("stderr", ""), store), args, args.repo).replace("; ", ", ") \
                if states[side] == "INCOMPLETE" else ""
            if cause:
                causes.setdefault(cause, []).append(label)
        causes = "".join(f"; {', '.join(labels)}: {cause}" for cause, labels in causes.items())
        dispositions = {(receipt[side] or {}).get("env") for side, expected in
                        (("before", "FAIL"), ("after", "PASS"), ("reversed", "FAIL")) if states[side] != expected}
        env = dispositions.pop() if len(dispositions) == 1 else None
        raise ReplayRejected(f"incident not admitted; red {states['before']}, green {states['after']}, "
                             f"reversed-patch {states['reversed']}; expected FAIL, PASS, FAIL{causes}", env)
    result["test_sha256"] = digest
    result["full_suite"] = full_suite(root, repo, args.before, args.after, args.timeout,
                                      getattr(args, "deadline", None))
    return result
