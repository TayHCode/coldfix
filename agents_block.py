"""Write a repository's proven Coldfix lessons into a marked AGENTS.md and CLAUDE.md block."""
import argparse

import coldfix as c
from flakiness import detect


BEGIN = "<!-- coldfix:lessons:begin -->"
END = "<!-- coldfix:lessons:end -->"


def toplevel(repo):
    repo = c.ordinary_path(repo)
    result = c.process(["git", "-C", str(repo), "rev-parse", "--show-toplevel"])
    c.require(result.returncode == 0, "repository must be a Git checkout")
    return c.ordinary_path(result.stdout.decode("utf-8").strip())


def relevant(store, roots, invalidations):
    flaky = detect(store)[1]
    found = []
    for row in c.latest_lessons(store).values():
        if (row["status"] not in c.LIVE or row["verification"] != "proven"
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


def updated(current, lessons):
    c.require(current.count(BEGIN) == current.count(END) <= 1 and current.find(BEGIN) <= current.find(END),
              "existing Coldfix block is malformed; left unchanged")
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


def write(args, root, store):
    repo = toplevel(args.repo)
    roots = c.repo_root(repo, "HEAD", None)
    c.require(roots is not None, "repository must be a complete Git checkout with history")
    lessons = relevant(store, roots, c.read_invalidations(root))
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
    written = []
    for name, path, current, content in planned:
        if content != current and not content:
            path.unlink()
            written.append(name)
        elif content != current:
            mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
            c.atomic_create(path, content, replace=path.exists(), mode=mode)
            written.append(name)
    return {"lesson_count": len(lessons), "lessons": [row["lesson_id"] for row in lessons], "written": written}
