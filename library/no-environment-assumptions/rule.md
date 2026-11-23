---
id: no-environment-assumptions
title: Don't assume the environment
proof: rerunnable
scope: public
category: testing
rank: 12
featured: false
---
# Don't assume the environment

No hardcoded hosts, paths, timezones, checkout directory names, runner pools or versions. Resolve binaries in the repository checkout's context, use explicit timezones for date boundaries, and register tests and exports in the config that discovers them.

## Applies when

Workflows, daemons and scripts, date logic, test-runner configs, and index barrels.

## Reproduce

Run the workflow or script on a different host, user, timezone or checkout directory. Or add a test file and run the repository's test command: it is never picked up.

## Fix

Resolve paths from the repository root, set explicit timezones for date boundaries, pin or discover versions, and register tests and exports in the config that discovers them.

## Verify

Run from a fresh clone in a different directory with `TZ` changed, and confirm the new test appears in the runner's count.

## Encode it higher

A meta-test that fails when a test file is not matched by the runner config.
