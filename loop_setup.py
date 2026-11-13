"""Check the automatic loop's prerequisites, then run its first pass: collect GitHub CI failures, scan transcripts
for corrected mistakes and list the candidates that pass found."""
import os
import shlex
import shutil

import coldfix as c
import learn_candidates
import learn_collect
import replay_isolation
import transcript_mistakes

SCAN_LEDGER = "transcript-scan.sqlite"
SCAN_THRESHOLD = 0.6


def setup(args):
    command = shlex.split(os.environ.get("COLDFIX_MODEL_CMD", ""))
    c.require(command and shutil.which(command[0]), "set COLDFIX_MODEL_CMD to a runnable model command (README Requirements)")
    folder = c.ordinary_path(args.transcripts)
    c.require(folder.is_dir(), "--transcripts must be an existing directory")
    c.require(args.limit > 0, "--limit must be positive")
    replay_isolation.require_sandbox()
    root = c.ordinary_path(args.store)
    root.mkdir(mode=0o700, exist_ok=True)
    github, unreachable = learn_collect.collect(root, args.owner)
    c.require(not unreachable, "GitHub unavailable: install gh and set GH_TOKEN, or run gh auth login")
    c.require(not github["github"].get("unlisted"), "GitHub could not list Actions runs for "
              f"{github['github'].get('unlisted')} repositories; give the token Actions read access")
    judge, label = transcript_mistakes.judge("COLDFIX_MODEL_CMD", None)
    try:
        scan = transcript_mistakes.scan(transcript_mistakes.transcripts([folder]), root / SCAN_LEDGER, root,
                                        "COLDFIX_MODEL_CMD", label, judge, args.limit, args.limit, SCAN_THRESHOLD)
    except BlockingIOError:
        raise c.ColdfixError("another setup run is scanning this store")
    except ValueError:
        raise c.ColdfixError("the scan ledger belongs to another model command; use a new --store")
    c.require(not scan["errors"], "transcript scan failed: " + str(scan.get("error")))
    found = learn_candidates.candidates(c.load_store(root)[1])
    first = found[0] if found else None
    return dict(backend=args.backend, github=github["github"], transcripts=scan, candidates=len(found),
                first_candidate=first and dict(headline=first["headline"], evidence=first["evidence"][:3],
                                               repos=first["repos"], transcript=first["transcript"]))
