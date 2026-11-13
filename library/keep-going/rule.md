---
id: keep-going
title: Keep going until done or truly blocked
proof: judged
---
# Keep going until done or truly blocked

Once the owner has authorized the work, continue until the stated done condition is met with evidence or a concrete blocker needs an owner decision. Do not stop to ask permission again. Do not invent time, budget, worker or audit-round caps the owner did not set, and do not add audit rounds beyond the project's rule. Explicit owner instructions to stop, pause or hold always win.

## Applies when

Coordinators, orchestrators and long-running build or improvement loops.

## Reproduce

The agent ends its turn asking for input before the work is done, or adds a worker or time cap nobody set and waits on choices it did not need.

## Fix

Add a stop hook that rejects ending a turn unless the final message contains either DONE with verification evidence against the stated done condition, or BLOCKED naming the specific owner decision or external dependency. An explicit owner stop or hold always ends work immediately; if work remains, report BLOCKED. Remove invented caps from code and config and keep only limits the owner set.

## Verify

Hook tests with transcript fixtures: a transcript ending "Want me to continue?" without DONE or BLOCKED is rejected; "BLOCKED: need owner choice between A and B" is allowed; an explicit owner stop with unfinished work reports BLOCKED. A lint rejects literal worker or audit-round upper bounds unless an inline citation points to the owner instruction that set them, and review checks that the citation really authorizes the cap.

## Encode it higher

A stop hook in the shared agent config, plus the cap lint.
