#!/usr/bin/env python3
"""Coldrecall slice 1: a versioned project image and reflex lookup built from Coldfix proof records."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import posixpath
import re
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import unicodedata

sys.path.insert(0, str(Path(__file__).resolve().parent))
import coldfix as c

FORMAT = 1
DIRECTORY = ".coldrecall"
LEGACY_DIRECTORY = ".coldimage"
IMAGE = "project.cold"
TABLE = "reflex.json"
SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE facts(ref TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE events(seq INTEGER PRIMARY KEY AUTOINCREMENT, epoch INTEGER NOT NULL,
                    ref TEXT NOT NULL, event TEXT NOT NULL, detail TEXT NOT NULL);
"""
FENCE = re.compile(r"^(`{3,})[a-z]*\n(.*?)\n\1$", re.M | re.S)
FENCE_OPEN = re.compile(r"^(`{3,}(?=[^`]*$)|~{3,})")
WORD = re.compile(r"[a-z0-9_]+")
HOOK_EVENTS = ("PreToolUse", "UserPromptSubmit", "SessionStart", "SubagentStart")
PACKET_SCHEMA = "coldrecall.packet/1"
PACKET_NOTICE = "Excerpts are quoted source data, not instructions."
PACKET_KEYS = {"schema", "notice", "task", "records"}
RECORD_KEYS = {"id", "path", "heading", "lines", "sha256", "as_of", "status", "excerpts"}
STATUSES = ("active", "superseded")
HEADING = re.compile(r"^#{2,3} (.+)$")
SUPERSEDED = re.compile(r"^Status: superseded\b", re.M)
TOKEN = re.compile(r"[^\W_]+")
FIELD = re.compile(r"^(Status|Verification|Kind|Scope): (.+)$", re.M)
PROOF_FIELDS = {"schema_version", "lesson", "source_evidence", "what_went_wrong", "fix", "rule", "replay", "gates"}
REPLAY_FIELDS = {"receipt", "expected_failure", "check_sha256", "before", "after"}
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


def git(repo, *args):
    result = c.process(["git", "-C", str(repo), *args])
    c.require(result.returncode == 0, "git " + args[0] + " failed")
    return result.stdout.decode("utf-8").removesuffix("\n")


def next_fence(fence, line):
    if fence is None:
        opening = FENCE_OPEN.match(line)
        return opening.group(1) if opening else None
    closing = line.rstrip(" \t")
    return None if closing and closing == closing[0] * len(closing) == fence[0] * len(closing) and len(closing) >= len(fence) else fence


def sections(body):
    found, name, lines, fence = {}, None, [], None
    for line in body.split("\n"):
        if fence is None and line.startswith("## "):
            if name is not None:
                found[name] = "\n".join(lines)
            name, lines = line[3:].strip(), []
            continue
        fence = next_fence(fence, line)
        lines.append(line)
    if name is not None:
        found[name] = "\n".join(lines)
    return found


def validate_proof(proof):
    version = proof.get("schema_version")
    c.require(type(version) is int and version in (1, 2), "invalid lesson proof version")
    allowed = PROOF_FIELDS | ({"admission"} if version == 2 else set()) | {"facts"}
    c.require(PROOF_FIELDS <= set(proof) <= allowed, "lesson proof keys do not match the schema")
    c.require((version == 1 and "admission" not in proof) or (version == 2 and "admission" in proof),
              "lesson admission fields do not match the schema version")
    identifier = proof["lesson"]
    parts = identifier.split(":") if isinstance(identifier, str) else []
    c.require(len(parts) == 4 and parts[:2] == ["coldfix", "lesson"] and all(parts[2:]),
              "proof record lesson identifier is missing or malformed")
    evidence = proof["source_evidence"]
    c.require(isinstance(evidence, list) and evidence and all(
        isinstance(ref, str) and re.fullmatch(r"coldfix:failure:[0-9a-f]{32}", ref) for ref in evidence)
        and evidence == sorted(set(evidence)),
        "proof record source evidence is invalid")
    for key in ("what_went_wrong", "fix", "rule"):
        c.require(isinstance(proof[key], str) and proof[key].strip(), "proof record " + key + " is missing")
    gates = proof["gates"]
    c.require(isinstance(gates, dict) and set(gates) == {"privacy", "release_check"}
              and gates["privacy"] == "PASS" and gates["release_check"] in ("PASS", "INCOMPLETE"),
              "proof record gates are invalid")
    replay = proof["replay"]
    replay_fields = REPLAY_FIELDS | ({"reversed", "test_sha256"} if version == 2 else set())
    c.require(isinstance(replay, dict) and set(replay) == replay_fields, "proof record replay keys do not match the schema")
    c.require(isinstance(replay["receipt"], str) and re.fullmatch(r"coldfix:replay:[0-9a-f]{32}", replay["receipt"])
              and isinstance(replay["expected_failure"], str) and replay["expected_failure"].strip()
              and isinstance(replay["check_sha256"], str) and SHA256.fullmatch(replay["check_sha256"]),
              "proof record replay header is invalid")
    sides = (("before", "FAIL", 1), ("after", "PASS", 0))
    if version == 2:
        sides += (("reversed", "FAIL", 1),)
    for side, status, exit_code in sides:
        row = replay[side]
        c.require(isinstance(row, dict) and set(row) == {"commit", "tree", "exit", "status"}
                  and row["status"] == status and type(row["exit"]) is int and row["exit"] == exit_code
                  and isinstance(row["commit"], str) and OBJECT_ID.fullmatch(row["commit"])
                  and isinstance(row["tree"], str) and OBJECT_ID.fullmatch(row["tree"]),
                  "proof record " + side + " replay result is invalid")
    c.require(replay["before"]["commit"] != replay["after"]["commit"]
              and replay["before"]["tree"] != replay["after"]["tree"],
              "proof record replay must bind a changed fix")
    if version == 2:
        admission = proof["admission"]
        admission_fields = {"kind", "diagnostic", "test_sha256", "source_ref_sha256", "test_paths_sha256"}
        c.require(isinstance(admission, dict) and set(admission) == admission_fields
                  and admission["kind"] in ("verified_incident", "verified_coldpass_incident"), "proof record admission is invalid")
        c.require(isinstance(admission["diagnostic"], str) and admission["diagnostic"].strip()
                  and not any(ord(char) < 32 or ord(char) == 127 for char in admission["diagnostic"]),
                  "proof record admission diagnostic is invalid")
        digests = (admission["test_sha256"], admission["source_ref_sha256"], replay["test_sha256"])
        c.require(all(isinstance(value, str) and SHA256.fullmatch(value) for value in digests),
                  "proof record admission digest is invalid")
        paths = admission["test_paths_sha256"]
        c.require(isinstance(paths, list) and paths and all(
            isinstance(value, str) and SHA256.fullmatch(value) for value in paths)
            and paths == sorted(set(paths)),
                  "proof record admission test paths are invalid")
        c.require(replay["reversed"]["commit"] == replay["after"]["commit"]
                  and replay["reversed"]["tree"] != replay["after"]["tree"]
                  and replay["test_sha256"] == admission["test_sha256"]
                  and replay["expected_failure"] == "regression:" + replay["test_sha256"][:16],
                  "proof record admission does not match its replay")
    c.validate_facts(proof)


def read_rule(path, body=None):
    text = (c.read_text(path) if body is None else body).replace("\r\n", "\n")
    parts = sections(text)
    record = FENCE.search(parts.get("Lesson record", ""))
    verify = parts.get("Verify", "")
    command = FENCE.search(verify)
    fields = dict(FIELD.findall(verify[:command.start()] + verify[command.end():])) if command else {}
    c.require(record and command and {"Status", "Verification"} <= set(fields), "rule file lacks a lesson record or verify section")
    proof = json.loads(record.group(2), object_pairs_hook=c.unique_object)
    c.require(isinstance(proof, dict), "proof record is not a JSON object")
    validate_proof(proof)
    evidence = parts.get("Evidence", "")
    c.require("\n\n" + proof["rule"] + "\n\n" in text
              and re.findall(r"^Lesson: (coldfix:lesson:[^\s]+)$", evidence, re.M) == [proof["lesson"]]
              and re.findall(r"^Replay: (coldfix:replay:[^\s]+)$", evidence, re.M) == [proof["replay"]["receipt"]]
              and re.findall(r"^- (coldfix:failure:[0-9a-f]{32})$", evidence, re.M) == proof["source_evidence"],
              "rule file evidence does not match its proof record")
    gap = c.proof_record_gap(proof)
    c.require(gap is None, gap or "")
    return proof, fields, command.group(2)


def anchors_for(repo, proof):
    before, after = (proof["replay"][side]["commit"] for side in ("before", "after"))
    try:
        result = c.process(["git", "-C", str(repo), "diff", "--name-only", "-z", before, after])
        c.require(result.returncode == 0, "git diff failed")
        changed = [name for name in result.stdout.decode("utf-8").split("\0") if name]
    except (c.ColdfixError, OSError, UnicodeError):
        changed = []
    return sorted(posixpath.normpath(name) for name in changed)


def read_sources(repo):
    registry = json.loads(c.read_text(repo / ".coldlocker/rules.json"), object_pairs_hook=c.unique_object)
    c.require(isinstance(registry, dict), "rule registry is not a JSON object")
    found, closed, skipped = {}, {}, []
    for name, entry in sorted(registry.items()):
        if isinstance(entry, dict) and (not entry.get("lesson") or entry.get("pending") is not False):
            continue
        path = repo / ".coldlocker/rules" / (name + ".md")
        try:
            c.require(isinstance(entry, dict), "registry entry is not an object")
            path = c.ordinary_path(path)
            c.require(path.is_file(), "registered lesson must be an ordinary file")
            raw = path.read_bytes()
            source_hash = hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()
            hashes = entry.get("hashes")
            c.require(isinstance(hashes, list) and hashes and all(
                isinstance(value, str) and SHA256.fullmatch(value) for value in hashes)
                and source_hash in hashes, "registered lesson hash does not match its source")
            proof, fields, command = read_rule(path, raw.decode("utf-8"))
            c.require(proof["gates"]["release_check"] == "PASS", "lesson release check is incomplete")
        except (c.ColdfixError, OSError, UnicodeError, ValueError):
            skipped.append(name)
            continue
        ref = proof["lesson"]
        if fields["Status"] not in c.LIVE:
            closed[ref] = "lesson " + fields["Status"]
        elif fields["Verification"] != "proven":
            skipped.append(name)
        else:
            title = re.search(r"^# (.+)$", raw.decode("utf-8"), re.M)
            found[ref] = dict(
                ref=ref, lesson_id=ref.split(":")[2], text=proof["rule"], kind=fields.get("Kind", ""),
                scope=fields.get("Scope", ""), source=str(path.relative_to(repo)), evidence=proof["source_evidence"],
                anchors=["path:" + name for name in anchors_for(repo, proof)],
                proven=dict(before=proof["replay"]["before"]["commit"], after=proof["replay"]["after"]["commit"]),
                **({"facts": proof["facts"]} if "facts" in proof else {}),
                state="active", reason="", witness=None,
                _command=command, _replay=proof["replay"]["receipt"], _title=title.group(1) if title else ref, _digest=proof["replay"]["check_sha256"], _failure=proof["replay"]["expected_failure"])
    return found, closed, skipped


def run_witness(repo, fact, head, timeout, *, executor=None):
    command = fact["_command"]
    if c.digest(command.replace("\r\n", "\n") + "\n") != fact["_digest"]:
        return dict(result="untrusted", head=head, detail="check does not match its proof record")
    try:
        done = (executor or c.process)(["sh", "-c", command], cwd=repo, timeout=timeout)
    except subprocess.TimeoutExpired:
        return dict(result="no-verdict", head=head, detail="check timed out")
    except OSError:
        return dict(result="no-verdict", head=head, detail="check could not run")
    status = c.check_status(done.returncode, done.stdout.decode("utf-8", errors="replace"), fact["_failure"])
    if status == "PASS":
        return dict(result="pass", head=head, detail="")
    if status == "FAIL":
        return dict(result="regression", head=head, detail=fact["_failure"])
    return dict(result="no-verdict", head=head, detail="check gave no verdict under the replay contract")


def guarded_paths_gone(repo, fact):
    paths = [name[len("path:"):] for name in fact["anchors"]]
    return bool(paths) and not any((repo / name).exists() for name in paths)


def read_image(path):
    if not path.is_file():
        return 0, None, {}, []
    with closing(sqlite3.connect(Path(os.path.abspath(path)).as_uri() + "?mode=ro", uri=True)) as database:
        meta = dict(database.execute("SELECT key, value FROM meta"))
        epoch = int(meta["epoch"])
        facts = {ref: json.loads(body) for ref, body in database.execute("SELECT ref, body FROM facts")}
        events = database.execute("SELECT epoch, ref, event, detail FROM events ORDER BY seq").fetchall()
    return epoch, meta.get("head"), facts, events


def write_image(directory, epoch, head, facts, events):
    descriptor, temporary = tempfile.mkstemp(prefix=".pending-", dir=directory)
    os.close(descriptor)
    try:
        with closing(sqlite3.connect(temporary)) as database:
            database.executescript(SCHEMA)
            database.executemany("INSERT INTO meta VALUES (?, ?)", [("format", str(FORMAT)), ("epoch", str(epoch)), ("head", head)])
            database.executemany("INSERT INTO facts VALUES (?, ?)", [(ref, c.encoded(fact)) for ref, fact in sorted(facts.items())])
            database.executemany("INSERT INTO events(epoch, ref, event, detail) VALUES (?, ?, ?, ?)", events)
            database.commit()
        os.replace(temporary, directory / IMAGE)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def table_for(epoch, head, facts):
    atoms, anchors = {}, {}
    for ref, fact in sorted(facts.items()):
        if fact["state"] != "active":
            continue
        for item in fact.get("facts", [{"id": None, "text": fact["text"]}]):
            atom_ref = ref + "#" + item["id"] if item["id"] is not None else ref
            atoms[atom_ref] = dict(text=item["text"], witness=fact["witness"], proven=fact["proven"])
            for anchor in fact["anchors"]:
                anchors.setdefault(anchor, []).append(atom_ref)
    return dict(format=FORMAT, epoch=epoch, head=head, atoms=atoms, anchors=anchors)


def compile_image(repo, timeout=None):
    repo = c.ordinary_path(repo)
    repo = c.ordinary_path(git(repo, "rev-parse", "--show-toplevel"))
    directory = repo / DIRECTORY
    directory.mkdir(mode=0o700, exist_ok=True)
    head = git(repo, "rev-parse", "HEAD")
    legacy = repo / LEGACY_DIRECTORY / IMAGE
    source = directory / IMAGE if (directory / IMAGE).is_file() or not legacy.is_file() else legacy
    epoch, image_head, previous, history = read_image(source)
    found, closed, skipped = read_sources(repo)
    facts = {ref: fact for ref, fact in previous.items() if fact["state"] == "retired"}
    events = []
    for ref, fact in found.items():
        if ref in facts:
            continue
        fact["witness"] = run_witness(repo, fact, head, timeout)
        if ref not in previous:
            events.append((ref, "added", fact["source"]))
        elif previous[ref]["witness"]["result"] != fact["witness"]["result"]:
            events.append((ref, "witness", fact["witness"]["result"]))
        if fact["witness"]["result"] == "no-verdict" and guarded_paths_gone(repo, fact):
            fact.update(state="retired", reason="obsolete: witness gave no verdict and every guarded path is gone")
            events.append((ref, "retired", fact["reason"]))
        facts[ref] = {key: value for key, value in fact.items() if not key.startswith("_")}
    held_sources = {".coldlocker/rules/" + name + ".md" for name in skipped}
    held = []
    for ref, fact in previous.items():
        if fact["state"] == "active" and ref not in found and fact["source"] in held_sources:
            facts[ref] = fact
            held.append(ref)
        elif fact["state"] == "active" and ref not in found:
            successor = next((new for new in found if found[new]["lesson_id"] == fact["lesson_id"]), None)
            fact.update(state="retired", reason=closed.get(ref) or ("superseded by " + successor if successor else "source withdrawn"))
            facts[ref] = fact
            events.append((ref, "retired", fact["reason"]))
    changed = bool(events) or facts != previous or image_head != head or not source.is_file()
    if changed or not (directory / IMAGE).is_file():
        epoch += changed
        write_image(directory, epoch, head, facts, history + [(epoch, *event) for event in events])
    table = table_for(epoch, head, facts)
    c.atomic_create(directory / TABLE, c.encoded(table), replace=True)
    return dict(epoch=epoch, head=head, active=sorted(table["atoms"]),
                retired={ref: fact["reason"] for ref, fact in sorted(facts.items()) if fact["state"] == "retired"},
                regressions=sorted(ref for ref, fact in facts.items() if fact["state"] == "active" and fact["witness"]["result"] == "regression"),
                unanchored=sorted(set(table["atoms"]) - {ref for refs in table["anchors"].values() for ref in refs}),
                skipped=skipped, held=sorted(held))


PATCH_PATH = re.compile(r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$", re.MULTILINE)


def candidate_paths(root, cwd, paths, command):
    tokens = list(paths)
    if command and command.lstrip().startswith("*** Begin Patch"):
        tokens += PATCH_PATH.findall(command)
    elif command:
        try:
            tokens += shlex.split(command)
        except ValueError:
            tokens += command.split()
    found = set()
    for token in tokens:
        full = os.path.normpath(os.path.join(cwd, token))
        try:
            token = os.path.relpath(os.path.join(os.path.realpath(os.path.dirname(full)), os.path.basename(full)), root)
        except ValueError:
            continue
        found.add("path:" + token.replace(os.sep, "/"))
    return found


def render(atom, head):
    witness, short = atom["witness"], lambda value: value[:12]
    if witness is None:
        return "[coldrecall] " + atom["text"]
    line = "[coldrecall] " + atom["text"]
    if witness["result"] == "regression":
        line = f"[coldrecall] REGRESSION ({witness['detail']}) seen by its witness at {short(witness['head'])}: " + atom["text"]
    elif witness["result"] == "pass":
        line += " (witness passed at " + short(witness["head"]) + (")" if witness["head"] == head else "; not re-run on this head)")
    else:
        line += f" (witness not run: {witness['detail']}; fact retained)"
    return line


def ranked(table, refs, text, ranking):
    words = set(WORD.findall(text.lower()))

    def overlap(ref):
        return -len(words & set(WORD.findall(table["atoms"][ref]["text"].lower()))) if ranking == "overlap" else 0

    def regression(ref):
        witness = table["atoms"][ref]["witness"]
        return not (witness and witness["result"] == "regression")
    return sorted(refs, key=lambda ref: (regression(ref), overlap(ref), ref))


def table_in(base):
    return next((base / name / TABLE for name in (DIRECTORY, LEGACY_DIRECTORY) if (base / name / TABLE).is_file()), None)


def table_path(cwd, root):
    main = Path(git(cwd, "worktree", "list", "--porcelain").split("\n", 1)[0].removeprefix("worktree "))
    return table_in(root) or table_in(main) or root / DIRECTORY / TABLE


def lookup(repo,paths=(), command=None, text="", everything=False, limit=None, ranking="overlap", delivered=None):
    cwd = os.path.realpath(repo)
    root = Path(git(cwd, "rev-parse", "--show-toplevel"))
    table = json.loads(table_path(cwd, root).read_text(encoding="utf-8"))
    if everything:
        refs = set(table["atoms"])
    else:
        refs = {ref for anchor in candidate_paths(root, cwd, paths, command) for ref in table["anchors"].get(anchor, [])}
    refs = ranked(table, [ref for ref in refs if ref not in (delivered or ())], text, ranking)
    if not refs:
        return [], []
    head = git(root, "rev-parse", "HEAD")
    refs = refs[:limit]
    return refs, [render(table["atoms"][ref], head) for ref in refs]


def tokens(text):
    return [token for token in TOKEN.findall(unicodedata.normalize("NFKC", text).casefold()) if len(token) > 1]


def packet_records(path, text):
    found, start, heading, fence = [], 0, None, None
    lines = text.split("\n")
    for number, line in enumerate(lines):
        if fence is None and HEADING.match(line):
            if heading is not None:
                found.append(dict(heading=heading, start=start, lines=lines[start:number]))
            start, heading = number, HEADING.match(line).group(1).strip()
        else:
            fence = next_fence(fence, line)
    if heading is not None:
        found.append(dict(heading=heading, start=start, lines=lines[start:]))
    return [dict(record, path=path) for record in found]


def packet_as_of(path):
    try:
        stamp = git(path.parent, "log", "-1", "--format=%cI", "--", path.name)
    except (c.ColdfixError, OSError, UnicodeError):
        return None
    return stamp or None


def build_packet(sources, task, record_limit=3, line_limit=10):
    c.require(type(record_limit) is int and record_limit > 0 and type(line_limit) is int and line_limit > 0,
              "packet limits must be positive integers")
    c.require(len({Path(source).name for source in sources}) == len(sources), "packet source basenames must be distinct")
    loaded = []
    for source in sources:
        path = Path(source)
        text = c.read_text(path).replace("\r\n", "\n")
        as_of = packet_as_of(path.resolve())
        loaded += [(record, as_of, [set(tokens(line)) for line in record["lines"]], set(tokens(record["heading"]))) for record in packet_records(path.name, text)]
    records = []
    for score, record, weights, as_of in rank_records(loaded, set(tokens(task)))[:record_limit]:
        ranked = sorted(range(len(weights)), key=lambda index: (-weights[index], index))
        picked = sorted(index for index in ranked[:line_limit] if weights[index])
        keep = sorted({near for index in picked for near in (index - 1, index, index + 1) if 0 <= near < len(weights)})
        start, body = record["start"], "\n".join(record["lines"])
        section_digest = c.digest(body)
        records.append(dict(
            id=c.digest(record["path"] + "\n" + record["heading"] + "\n" + str(start) + "\n" + section_digest)[:8],
            path=record["path"], heading=record["heading"], lines=[start + 1, start + len(weights)], sha256=section_digest, as_of=as_of,
            status="superseded" if SUPERSEDED.search(body) else "active",
            excerpts=[dict(line=start + index + 1, text=record["lines"][index]) for index in keep]))
    return dict(schema=PACKET_SCHEMA, notice=PACKET_NOTICE, task=task, records=records)


def rank_records(loaded, query):
    frequency = {term: sum(1 for _, _, words, head in loaded if any(term in line for line in words) or term in head) for term in query}
    idf = {term: math.log(1 + len(loaded) / count) for term, count in frequency.items() if count}
    pool = []
    for record, as_of, words, head in loaded:
        weights = [sum(idf[term] for term in query.intersection(line) if term in idf) for line in words]
        best = sorted(weights, reverse=True)
        score = sum(idf[term] for term in head if term in idf) + best[0] + sum(best[1:3]) / 2
        if score:
            pool.append((score, record, weights, as_of))
    pool.sort(key=lambda item: (-item[0], item[1]["path"], item[1]["start"]))
    return pool


def validate_packet(packet):
    c.require(isinstance(packet, dict) and set(packet) == PACKET_KEYS, "packet keys do not match the schema")
    c.require(packet["schema"] == PACKET_SCHEMA and packet["notice"] == PACKET_NOTICE and isinstance(packet["task"], str), "packet header is invalid")
    c.require(isinstance(packet["records"], list), "packet records are not a list")
    for record in packet["records"]:
        c.require(isinstance(record, dict) and set(record) == RECORD_KEYS, "packet record keys do not match the schema")
        c.require(all(isinstance(record[key], str) for key in ("id", "path", "heading", "sha256")), "packet record text fields are invalid")
        c.require(record["status"] in STATUSES and (record["as_of"] is None or isinstance(record["as_of"], str)), "packet record status or freshness is invalid")
        first, last = record["lines"] if isinstance(record["lines"], list) and len(record["lines"]) == 2 else (0, -1)
        c.require(all(type(n) is int for n in (first, last)) and 1 <= first <= last, "packet record line range is invalid")
        for excerpt in record["excerpts"] if isinstance(record["excerpts"], list) else [None]:
            c.require(isinstance(excerpt, dict) and set(excerpt) == {"line", "text"} and type(excerpt["line"]) is int
                      and first <= excerpt["line"] <= last and isinstance(excerpt["text"], str), "packet excerpt is invalid")
    return packet


def render_packet(packet, style):
    if style == "json":
        return json.dumps(packet, ensure_ascii=False, separators=(",", ":"))
    out = [f"[coldrecall] {packet['notice']} Task: {packet['task']}"]
    for record in packet["records"]:
        out.append(f"Source {record['path']} lines {record['lines'][0]}-{record['lines'][1]}, section \"{record['heading']}\", "
                   f"status {record['status']}, as of {record['as_of'] or 'unknown'}, sha256 {record['sha256'][:12]}:")
        out.extend(f"  {excerpt['line']}: {excerpt['text']}" for excerpt in record["excerpts"])
    return "\n".join(out)


def fail_open(action):
    try:
        return action()
    except (OSError, ValueError, KeyError, TypeError, AttributeError, c.ColdfixError) as error:
        print("coldrecall: no reflex this time: " + type(error).__name__, file=sys.stderr)
        return [], []


def delivered_path(repo, session):
    return Path(repo) / DIRECTORY / ("delivered-" + c.digest(session) + ".json")


def deployed_log():
    code = Path(__file__).resolve().parent
    return code.parent / "injections.jsonl" if code.with_name(code.name + ".revision").is_file() else None


def record_injection(log, session, event, refs, context):
    row = dict(at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
               session=c.digest(session) if isinstance(session, str) and session.strip() else None,
               event=event if event in HOOK_EVENTS else "other", refs=refs, chars=len(context), bytes=len(context.encode("utf-8")))
    try:
        with open(log, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(row) + "\n")
    except OSError as error:
        print("coldrecall: injection not logged: " + type(error).__name__, file=sys.stderr)


def hook(stdin, stdout, limit=None, ranking="overlap", once=False, log=None):
    try:
        payload = json.load(stdin)
        event = payload.get("hook_event_name") or "PreToolUse"
        repo = payload.get("cwd") or "."
        paths, command, text = [], None, str(payload.get("prompt") or "")
        if event == "PreToolUse":
            arguments = payload["tool_input"]
            paths = [arguments[key] for key in ("file_path", "path", "notebook_path") if isinstance(arguments.get(key), str)]
            command = arguments.get("command") if isinstance(arguments.get("command"), str) else None
            text = " ".join([text] + [value for value in arguments.values() if isinstance(value, str)])
    except (ValueError, KeyError, TypeError, AttributeError):
        return 0

    def pick(seen):
        before = json.loads(seen.read_text(encoding="utf-8")) if seen and seen.is_file() else []
        refs, lines = lookup(repo, paths, command, text, event != "PreToolUse", limit, ranking, before)
        if seen and refs:
            c.atomic_create(seen, json.dumps(before + refs), replace=True)
        return refs, lines

    def find():
        session = payload.get("session_id")
        if not once or not isinstance(session, str) or not session.strip():
            return pick(None)
        cwd = os.path.realpath(repo)
        root = git(cwd, "rev-parse", "--show-toplevel")
        if not table_path(cwd, Path(root)).is_file():
            return [], []
        seen = delivered_path(root, session)
        seen.parent.mkdir(mode=0o700, exist_ok=True)
        with open(str(seen) + ".lock", "a", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return pick(seen)
    refs, lines = fail_open(find)
    if lines:
        context = "\n".join(lines)
        json.dump(dict(hookSpecificOutput=dict(hookEventName=event, additionalContext=context)), stdout)
        log = log or deployed_log()
        if log:
            record_injection(log, payload.get("session_id"), event, refs, context)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    build = commands.add_parser("compile", help="compile the image and reflex table from proof records, running witnesses")
    build.add_argument("--repo", default=".")
    build.add_argument("--timeout", type=float, default=None, help="optional seconds allowed per witness")
    ask = commands.add_parser("lookup", help="print the one-line facts matching an action; always exits 0")
    ask.add_argument("--repo", default=".")
    ask.add_argument("--path", action="append", default=[])
    ask.add_argument("--command")
    ask.add_argument("--text", default="", help="task text used to rank path matches")
    reflex = commands.add_parser("hook", help="hook: read the event on stdin, add matching facts; always exits 0")
    reflex.add_argument("--budget", type=int, default=None, help="most facts added per event; default: all that match")
    reflex.add_argument("--ranking", choices=("ref", "overlap"), default="overlap", help="order facts by reference or by word overlap with the event text")
    reflex.add_argument("--once", action="store_true", help="add each fact at most once per session")
    reflex.add_argument("--log", help="append one JSON line per delivery to this file; "
                        "default: injections.jsonl beside a deployed runtime")
    pack = commands.add_parser("packet", help="print a task-addressable context packet of source excerpts; exits 2 without output if invalid")
    pack.add_argument("--task", required=True)
    pack.add_argument("--source", action="append", required=True)
    pack.add_argument("--format", choices=("json", "prose"), default="json")
    pack.add_argument("--records", type=int, default=3)
    pack.add_argument("--lines", type=int, default=10)
    args = parser.parse_args(argv)
    if args.action == "hook":
        return hook(sys.stdin, sys.stdout, args.budget, args.ranking, args.once, args.log)
    if args.action == "lookup":
        for line in fail_open(lambda: lookup(args.repo, args.path, args.command, text=args.text))[1]:
            print(line)
        return 0
    if args.action == "packet":
        try:
            print(render_packet(validate_packet(build_packet(args.source, args.task, args.records, args.lines)), args.format))
            return 0
        except (c.ColdfixError, OSError, UnicodeError, ValueError) as error:
            print("coldrecall: " + str(error), file=sys.stderr)
            return 2
    try:
        summary = compile_image(args.repo, args.timeout)
        for ref in summary["held"]:
            print("coldrecall: source unreadable, fact kept: " + ref, file=sys.stderr)
        print(c.encoded(summary), end="")
        return 0
    except (c.ColdfixError, OSError, UnicodeError, ValueError, sqlite3.Error) as error:
        print("coldrecall: " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
