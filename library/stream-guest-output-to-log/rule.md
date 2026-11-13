---
id: stream-guest-output-to-log
title: Stream guest command output to its log on timeout paths
proof: judged
---
# Stream guest command output to its log on timeout paths

When a replayed guest command has a log file, send the child's output straight to that file rather than copying it after the process returns. A copy made after wait() is skipped on timeout, so the log loses the output that explains the failure. Run the timeout regression test before pushing.
