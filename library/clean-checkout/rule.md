---
id: clean-checkout
title: Keep worktrees and runtime state out of git status
proof: rerunnable
scope: public
category: git-and-ci
rank: 16
featured: false
---
# Keep worktrees and runtime state out of git status

Each repository's own committed `.gitignore` must ignore `/.worktrees/` and every runtime-state, snapshot, budget or host-config path the tool writes inside the checkout. `git status --porcelain` must be empty after a run in a fresh clone.

## Applies when

Any repository where agents create worktrees inside the checkout, or where code or scripts write state, database copies, snapshots, receipts or settings inside the repository tree.

## Reproduce

In a fresh clone, run `mkdir -p .worktrees/probe && : > .worktrees/probe/x && git status --porcelain --untracked-files=all`. The output contains `?? .worktrees/`, so the checkout is dirty.

## Fix

Add `/.worktrees/` to the repository's own `.gitignore`, plus each in-repo runtime path (state files, database copies, snapshots, generated settings). Move runtime state the repository does not need to track to a state directory outside the checkout.

## Verify

In CI on a fresh clone, never relying on global excludes: `mkdir -p .worktrees/probe && : > .worktrees/probe/x && test -z "$(git status --porcelain --untracked-files=all)"`. Run the tool's normal command once, then repeat the test.

## Encode it higher

A CI step in each repository's verify workflow, plus the `.gitignore` line in the repository template.
