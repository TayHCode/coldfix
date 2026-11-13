---
id: verify-the-right-thing
title: Bind every receipt to the tree that ships
proof: judged
---
# Bind every receipt to the tree that ships

Verification must run against the exact head being claimed: use the full pushed range (before..after), not the last commit; scan the whole workspace diff, not only changed-file lists; regenerate counts after the final rebase; and only cite commits and hashes that exist in a clean checkout. Bind every receipt to the tree hash of what ships and recheck it at merge or deploy; ancestry proves nothing. Checkers must run the product's real entrypoint.

## Applies when

`.github/workflows/**`, scripts named verify, validate, check, audit or scan, and evidence docs that cite SHAs or test counts.

## Reproduce

Take a receipt produced before a squash merge. Its recorded head SHA differs from the squash commit on main, yet a gate that accepts ancestry passes it. Or run a checker with a test-only shortcut that skips the product's real network path: it reports a fault the product does not have.

## Fix

Route all verification through one verify entrypoint. It records the commit SHA and the tree hash (`git rev-parse <sha>^{tree}`) of what it tested and invokes the product's real entrypoint and network path. The merge or deploy gate recomputes the tree hash of the revision being shipped and requires an exact match. Otherwise the receipt is stale and verify reruns.

## Verify

In a fresh clone, for each receipt, `git cat-file -e <sha>^{commit}` succeeds and `test "$(git rev-parse <sha>^{tree})" = "$(git rev-parse <shipping-rev>^{tree})"`. This fails for a receipt made on an older head even when that head is an ancestor, and passes after verify reruns on the shipping tree. Add a checker test asserting it uses the same entrypoint and network settings as production.

## Encode it higher

A single verify entrypoint that emits a tree-bound receipt, a gate that compares tree hashes, and a checker parity test.
