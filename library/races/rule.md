---
id: races
title: Guard read-then-write on shared state
proof: judged
scope: public
category: concurrency-and-state
rank: 29
featured: false
summary: Use a conditional update to claim shared work. Two workers can both read a pending job before either writes, so only the worker whose update changes the row may proceed.
---
# Guard read-then-write on shared state

Read-then-write on shared rows needs a transaction or compare-and-set (`update ... where state = old`). Async fetches that set UI state need a stale-response guard. Deploy and automation triggers must re-check that the PR or state is still current. Multi-worker claims need atomic ownership.

## Applies when

ORM find-then-update code, UI effects that fetch, workflow-triggered workflows, and worker or claim loops.

## Reproduce

Run two workers, two requests, or a fast second UI fetch against the same row: both read the old state and both write.

## Fix

Use a transaction or compare-and-set. Add a stale-response guard to async UI fetches. Make automation triggers re-check that the PR or state is still current. Give claims atomic ownership.

## Verify

A concurrency test that runs two actors against one row and asserts exactly one wins.

## Encode it higher

Architecture: one compare-and-set claim primitive used by all workers, and a review lint that bans read-then-update.

## Bad

```sql
SELECT state FROM jobs WHERE id = ?;
UPDATE jobs SET state = 'claimed' WHERE id = ?;
```

## Good

```sql
UPDATE jobs SET state = 'claimed' WHERE id = ? AND state = 'pending' RETURNING id;
```
