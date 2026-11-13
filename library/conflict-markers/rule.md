---
id: conflict-markers
title: Never commit unresolved merge markers
proof: rerunnable
---
# Never commit unresolved merge markers

Never commit unresolved merge markers (`<<<<<<<`, `=======`, `>>>>>>>`), especially in changelogs and generated docs.

## Applies when

Any file after a merge or rebase.

## Reproduce

Merge or rebase conflicting branches and commit without checking: the markers remain, often in changelogs and generated docs.

## Fix

Reject opening and closing conflict boundaries of seven or more characters, including orphaned ones. Ignore equals-only lines on their own, which are common banners in docs.

## Verify

Run the checker over every tracked file from the repository root. Exit 0 is PASS, 1 is FAIL and 2 is INCOMPLETE.

## Encode it higher

A CI step that runs the checker before the test suites.
