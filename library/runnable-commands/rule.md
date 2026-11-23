---
id: runnable-commands
title: Run every documented command exactly as written
proof: rerunnable
scope: public
category: testing
rank: 18
featured: false
---
# Run every documented command exactly as written

Every documented or emitted command (README, onboarding, fallbacks, subprocess calls) must work as written from a clean state: include required arguments and flags, never hardcode directory names, set the working directory explicitly, include staged and merge-commit changes when diffing (`git diff --name-only HEAD`, `--cached`), and regenerate generated clients before use. Run every command you document, exactly as written, in a fresh clone before pushing.

## Applies when

README, onboarding and runbook code blocks, spawn, exec and subprocess calls, and git invocations in scripts.

## Reproduce

In a fresh clone, run a documented command exactly as written, such as a cleanup step `git worktree remove` with its path argument missing: it exits with a usage error. Or follow a doc link to a file such as CONTRIBUTING.md that does not exist.

## Fix

Correct each command so it includes all required arguments, a documented dependency install step, and non-interactive output (no pager). Add a docs smoke test that extracts runnable fenced shell blocks and executes them, or their `--help` or `--dry-run` form, in a fresh clone in CI.

## Verify

Run the docs smoke test in CI on a fresh clone. It fails on a base where a documented command errors (usage error or missing input), passes after the fix, and also fails if a file linked from the docs does not exist.

## Encode it higher

A CI docs-command smoke test plus a link-existence check.
