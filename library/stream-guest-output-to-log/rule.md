---
id: stream-guest-output-to-log
title: Stream guest command output to its log on timeout paths
proof: rerunnable
scope: public
category: ops-and-infra
rank: 19
featured: false
summary: Redirect a subprocess’s output to its log before waiting. If the subprocess times out, the log still contains the output that preceded the timeout.
---
# Stream guest command output to its log on timeout paths

When a replayed guest command has a log file, send the child's output straight to that file rather than copying it after the process returns. A copy made after wait() is skipped on timeout, so the log loses the output that explains the failure. Run the timeout regression test before pushing.
