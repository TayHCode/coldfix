---
id: errors-are-states
title: Treat errors as states: PASS, FAIL or INCOMPLETE
proof: rerunnable
---
# Treat errors as states: PASS, FAIL or INCOMPLETE

Do not collapse ambiguous outcomes into success or failure: separate definitive outcomes (4xx) from uncertain ones (timeout, lost response). A checker that exits non-zero must fail the check. Exit when the upstream exits. Every failure branch must still record accounting and receipts and clean up. Return PASS, FAIL or INCOMPLETE from every check; errors and unavailable tools are INCOMPLETE, and only PASS may open a gate or advance state.

## Applies when

HTTP clients and gateways, subprocess wrappers, retry, restore and refresh code, and validators that shell out.

## Reproduce

Stub a gate's checker to raise or report that it is unavailable, then submit: the gate opens. Or feed input that makes a library raise an uncaught error: the checker exits as FAIL instead of INCOMPLETE.

## Fix

Define a verdict type with exactly PASS, FAIL and INCOMPLETE as the checker return type. Wrap every checker call so any exception or missing dependency returns INCOMPLETE with a reason. Gate code matches every verdict exhaustively, and only the PASS arm proceeds. Schedulers keep their cursor and retain events unless the verdict is PASS.

## Verify

Add a test per gate that injects a raising checker and an unavailable checker, then asserts the gate stays closed, the verdict is INCOMPLETE, and the cursor or retained data is unchanged. Run a type checker in CI so a missing match arm fails the build.

## Encode it higher

A shared verdict type plus exhaustive-match type checking in CI, and one fault-injection test per gate.
