"""Write a repository's proven Coldfix lessons into a marked AGENTS.md and CLAUDE.md block."""
import argparse
import re

import coldfix as c
import transcript_delivery as delivery
from flakiness import detect


BEGIN = "<!-- coldfix:lessons:begin -->"
END = "<!-- coldfix:lessons:end -->"
COPIES = "instruction-copies.json"


def toplevel(repo):
    repo = c.ordinary_path(repo)
    result = c.process(["git", "-C", str(repo), "rev-parse", "--show-toplevel"])
    c.require(result.returncode == 0, "repository must be a Git checkout")
    return c.ordinary_path(result.stdout.decode("utf-8").strip())


def relevant(store, roots, invalidations, vetoed):
    flaky = detect(store)[1]
    found = []
    for row in c.latest_lessons(store).values():
        if (row["lesson_id"] in vetoed or row["status"] not in c.LIVE or row["verification"] != "proven"
                or not row["rule"].strip() or flaky.intersection(row["evidence"])
                or invalidations.get(row["lesson_id"]) == row["id"]):
            continue
        if c.lesson_scope(row, store) != "global":
            proven = c.proven_repos(row, store["lessons"], store["receipts"])
            if (not proven or store["receipts"][row["receipt"]].get("repo_root") != proven[0]
                    or set(roots.split(",")).isdisjoint(proven[0].split(","))):
                continue
        found.append(row)
    return sorted(found, key=lambda row: row["lesson_id"])


def block(lessons):
    entries = []
    for row in lessons:
        c.require(not any(marker in row[key] for marker in (BEGIN, END) for key in ("title", "rule")),
                  "lesson text contains the Coldfix block marker")
        entries.append(f"### {row['title']}\n\n{row['rule'].strip()}\n\n"
                       f"Lesson: coldfix:lesson:{row['lesson_id']}:{row['id']}\n")
    return BEGIN + "\n## Coldfix lessons (replay-proven)\n\n" + "\n".join(entries) + END + "\n"


def validate_block(current):
    c.require(current.count(BEGIN) == current.count(END) <= 1 and current.find(BEGIN) <= current.find(END),
              "existing Coldfix block is malformed; left unchanged")


def updated(current, lessons):
    validate_block(current)
    new = block(lessons) if lessons else ""
    if BEGIN not in current:
        return (current.rstrip("\n") + "\n\n" if current.strip() and new else current) + new
    start, end = current.index(BEGIN), current.index(END) + len(END)
    before, after = current[:start], current[end:]
    if not before or before.endswith("\n"):
        after = after.removeprefix("\n")
    if not new and not after and before.endswith("\n\n"):
        before = before[:-1]
    return before + new + after


def save_copy(path, content):
    if content:
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
        c.atomic_create(path, content, replace=path.exists(), mode=mode)
    else:
        path.unlink()


def withdraw_copies(root, lesson_id):
    registry = root / COPIES
    copies = delivery.load(registry, {})
    pending = []
    changed = False
    for name, ids in list(copies.items()):
        if lesson_id not in ids:
            continue
        try:
            path = c.ordinary_path(name)
            c.require(str(path) == name and path.name in ("AGENTS.md", "CLAUDE.md")
                      and toplevel(path.parent) == path.parent, "unavailable instruction file")
            current = c.read_text(path) if path.exists() else ""
            if BEGIN in current or END in current:
                validate_block(current)
                owned = current[current.index(BEGIN):current.index(END) + len(END)] + "\n"
                refs = re.findall(r"^Lesson: coldfix:lesson:[0-9a-f]{32}:([0-9a-f]{32})$", owned, re.M)
                rows = [delivery.load(root / "lessons" / (ref + ".json"), {}) for ref in refs]
                c.require(owned == block(rows), "modified instruction block")
                kept = [row for row in rows if row["lesson_id"] != lesson_id]
                if len(kept) != len(rows):
                    save_copy(path, updated(current, kept))
        except (OSError, ValueError, KeyError):
            pending.append(name)
            continue
        changed = True
        ids.remove(lesson_id)
        if not ids:
            del copies[name]
    if changed:
        c.atomic_create(registry, c.encoded(copies), replace=True)
    return sorted(pending)


def write(args, root, store):
    with c.lesson_lock(root, name=".transcript-delivery.lock"):
        return write_locked(args, root, store)


def write_locked(args, root, store):
    repo = toplevel(args.repo)
    roots = c.repo_root(repo, "HEAD", None)
    c.require(roots is not None, "repository must be a complete Git checkout with history")
    ledger = delivery.load(root / delivery.DELIVERIES, {})
    lessons = relevant(store, roots, c.read_invalidations(root), delivery.proven_vetoes(ledger))
    terms = c.privacy_sources(argparse.Namespace(denylist=args.denylist, git_repo=[str(repo)], public=False), store)
    counts = c.privacy_counts(block(lessons), {key: value for key, value in terms.items() if key != "private_repo"},
                              provenance=True)
    for category, count in c.privacy_counts("\n".join(row[key] for row in lessons for key in ("title", "rule")), terms).items():
        counts[category] = counts.get(category, 0) + count
    c.require(not counts, "lesson block privacy screen blocked: " + c.encoded(counts).strip())
    planned = []
    for name in ("AGENTS.md", "CLAUDE.md"):
        path = repo / name
        if path.is_symlink() or name == "CLAUDE.md" and not path.exists():
            continue
        current = c.read_text(path) if path.exists() else ""
        planned.append((name, path, current, updated(current, lessons)))
    ids = [row["lesson_id"] for row in lessons]
    registry = root / COPIES
    copies = delivery.load(registry, {})
    original = copies.copy()
    for _, path, _, _ in planned:
        if ids:
            copies[str(path)] = sorted(set(copies.get(str(path), [])) | set(ids))
    if original != copies:
        c.atomic_create(registry, c.encoded(copies), replace=True)
    registered = copies.copy()
    written = []
    for name, path, current, content in planned:
        if content != current:
            save_copy(path, content)
            written.append(name)
        if ids:
            copies[str(path)] = ids
        else:
            copies.pop(str(path), None)
    if copies != registered:
        c.atomic_create(registry, c.encoded(copies), replace=True)
    return {"lesson_count": len(lessons), "lessons": ids, "written": written}
