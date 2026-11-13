---
id: state-safety
title: Persist state atomically and retry interrupted effects safely
proof: rerunnable
---
# Persist state atomically and retry interrupted effects safely

Persist state atomically and in the right order: write a temporary file then rename it, or use a transaction or compare-and-set. A claim, act, finalize sequence must survive the process dying between steps, and multi-instance services cannot rely on in-memory state. Mark "done", "merged" or "processed" only after the real operation succeeded. When a base or head advances, refresh derived records. Persist decisions and completion markers through one owner.

## Applies when

Lock, claim, lease, queue, ledger, cache, spool or token-store code, file writes to state paths, and database updates after reads.

## Reproduce

Interrupt a state write halfway and retry: the truncated file blocks recovery. Or make a side effect fail once, restore it, and retry: a completion marker written before the effect suppresses the retry.

## Fix

Route each state write through one atomic writer that writes and validates a temporary file before renaming it. Let one owner persist a completion marker only after the effect succeeds, and keep a retryable state on failure. Persist each decision before a retry can change its outcome.

## Verify

Add fault-injection tests at each operation boundary. An interrupted write must leave the previous state intact and retry to the exact expected state. A failed effect must be retried and happen exactly once after recovery.

## Encode it higher

One atomic-writer helper and one retryable-operation helper, each with fault-injection tests.
