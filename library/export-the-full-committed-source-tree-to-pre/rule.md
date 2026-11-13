---
id: export-the-full-committed-source-tree-to-pre
title: Export the full committed source tree to pre-push snapshots
proof: judged
---
# Export the full committed source tree to pre-push snapshots

When a pre-push check exports a commit snapshot, include the complete committed source tree so unchanged imported modules resolve. Pass only the changed files as check targets. Verify with a test where a pushed file imports an unchanged local module.
