---
id: prove-a-process-is-stale
title: Establish that a process is stale before terminating it
proof: judged (opinion)
---
# Establish that a process is stale before terminating it

Before recommending process termination, identify what owns the process and establish that it is stale or safe to stop. Do not use process age or an idle-looking working directory as proof that terminating it is harmless.

## How to check

Check the process tree, its owner or supervisor, current activity, and associated uncommitted work before recommending termination; require evidence that the session can be stopped safely.
