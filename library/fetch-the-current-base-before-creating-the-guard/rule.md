---
id: fetch-the-current-base-before-creating-the-guard
title: Fetch the current base before creating the guard worktree
proof: replayed
---
# Fetch the current base before creating the guard worktree

When a tick merges several PRs, the base SHA changes after each merge, so fetch the current base ref from origin before creating each PR's base worktree. Fetching the PR head does not bring in the new squash commit. Add a test where a second PR is checked against the commit left by merging the first.
