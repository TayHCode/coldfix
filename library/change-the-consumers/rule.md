---
id: change-the-consumers
title: When a producer changes, change every consumer in the same change
proof: rerunnable
scope: public
category: data-and-parsing
rank: 27
featured: false
---
# When a producer changes, change every consumer in the same change

When a producer changes (schema, model, route, flag, enum), grep every consumer and validator in the same change. A database schema change needs a migration. A UI call to a backend route needs the route to exist first. Validators must not be stricter than the producer.

## Applies when

Database schemas, API routes, shared types and contracts, and UI fetch calls.

## Reproduce

Change a schema, enum, route or flag in the producer only, then grep for consumers and validators: one still expects the old shape, or the UI calls a route that does not exist yet.

## Fix

Grep every consumer and validator in the same change. Add the migration with the schema change. Land the backend route before the UI call. Keep validators no stricter than the producer.

## Verify

`grep -rn <symbol>` shows no stale consumer, the consumer test suite passes, and the migration diff is clean.

## Encode it higher

A CI check that fails when the schema changes without a migration, plus shared contract types imported by both sides.
