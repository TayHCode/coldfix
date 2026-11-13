from failure_ingest import import_records, record


def evidence(bad, fixed, findings, unverified, proof, diff):
    # ruleid: failure-ingest-consumer
    return dict(type="settle-codex-lesson", repo=bad.slug, pr=bad.pr, findings=findings, unverified_findings=unverified, bad_head=bad.head, fixed_head=fixed.head,
                failing_settle=dict(log=bad.source, occurred=bad.occurred),
                fixing_settle=dict(log=fixed.source, occurred=fixed.occurred, outcome="merged" if fixed.merged else "passed"),
                diff=diff, replay=proof)
