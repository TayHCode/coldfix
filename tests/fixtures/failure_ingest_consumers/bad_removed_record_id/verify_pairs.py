# ruleid: failure-ingest-consumer
from failure_ingest import import_records, record, record_id


def evidence(pair, repos):
    side = lambda attempt, head: dict(head=head, log=Path(attempt.source).name, occurred=attempt.occurred, excerpt=attempt.excerpt)
    return dict(type="verify-fix-pair", repo=pair["slug"], pr=pair["pr"],
                failing_check=side(pair["fail"], pair["x"]), passing_check=side(pair["ok"], pair["y"]),
                diff=repos.evidence_diff(pair["repo"], pair["x"], pair["y"]))


def source_ref(pair):
    return f"verify-pair:{pair['slug']}:{pair['pr']}:{pair['x'][:12]}:{pair['y'][:12]}"


def event(pair, repos):
    return dict(kind="test", repo=pair["slug"], command=c.fleet_label("verify"), error=c.encoded(evidence(pair, repos)).strip(),
                agent="unknown", thread="", occurred=pair["fail"].occurred, source_ref=source_ref(pair))


def run(root, projects, settle, dry_run=False, repos=None):
    attempts = collect(projects, settle)
    if repos is None:
        dirs = {str(p.parent) for p in Path(projects).glob("*/.git")} | {str(p.parent) for p in Path(projects).glob("*/.worktrees/*/.git")}
        repos = Repos(sorted(dirs))
    pairs, rejected = find_pairs(attempts, repos)
    ingested = dict(imported=0, skipped=0)
    if pairs and not dry_run:
        path = c.ordinary_path(root)
        known = c.load_store(path)[1]["failures"]
        fresh = [pair for pair in pairs if record_id(source_ref(pair)) not in known]
        ingested = import_records(path, [record(event(pair, repos)) for pair in fresh])
        ingested["skipped"] += len(pairs) - len(fresh)
    return dict(attempts=len(attempts), pairs=len(pairs), ingested=ingested["imported"], already_present=ingested["skipped"],
                rejected={reason: rejected[reason] for reason in REASONS if rejected[reason]},
                sources=[source_ref(pair) for pair in pairs])
