---
id: use-the-triggering-invocation-id
title: Use the triggering invocation ID in async failure handlers
proof: judged
---
# Use the triggering invocation ID in async failure handlers

An asynchronous failure handler must identify the invocation that triggered it, preferring the `MONITOR_INVOCATION_ID` passed in by the trigger over the unit's current `InvocationID`. A failed or unreadable journal must return unknown so the stored state is kept, not an empty set. Test that a later run starting before the handler does not hide a new failure from the triggering run.
