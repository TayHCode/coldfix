from failure_ingest import import_records, record


def evidence(pair, repos):
    side = lambda attempt, head: dict(head=head, log=Path(attempt.source).name, occurred=attempt.occurred, excerpt=attempt.excerpt)
    # ruleid: failure-ingest-consumer
    return dict(type="verify-fix-pair", repo=pair["slug"], pr=pair["pr"],
                failing_check=side(pair["fail"], pair["x"]), passing_check=side(pair["ok"], pair["y"]),
                diff=repos.evidence_diff(pair["repo"], pair["x"], pair["y"]))
