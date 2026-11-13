---
id: docs-sync
title: Change docs in the same commit as the code they describe
proof: rerunnable
---
# Change docs in the same commit as the code they describe

Docs describing behavior (README, runbooks, specs, templates, agent instructions, changelogs, inventories, ledgers) must change in the same commit as the code they describe. Numbers, hashes, byte sizes, dates and "last refreshed" fields must be regenerated from the final state, never typed from memory. Do not document a fix before the code implements it.

## Applies when

Editing `*.md`, `docs/**`, `CHANGELOG*`, runbooks or templates, or changing flags, routes, environment variables, config keys or schemas that docs mention.

## Reproduce

Change a flag, route, environment variable or schema that a README or runbook mentions, commit only the code, then diff the doc: names, counts, hashes and "last refreshed" dates are stale.

## Fix

Edit the doc in the same commit. Regenerate every number, hash, byte size and date from the final tree with the project script, never from memory. Do not document a fix that is not yet in the code.

## Verify

Run the project's doc generator and confirm `git diff` is empty. Grep the docs for each changed flag, route or key name.

## Encode it higher

A CI check that regenerates generated docs and fails on `git diff --exit-code`.
