---
id: flag-node-modules-for-review
title: Keep node_modules at the root or tracked in git flagged for manual review
proof: judged
---
# Keep node_modules at the root or tracked in git flagged for manual review

A node_modules directory at the repository root, or one tracked in git, must still count as a dependency artifact that needs manual review, even when no package manifest or lockfile is visible. Set the node ecosystem scan's artifacts flag from whether node_modules exists on disk, and run the node ecosystem tests before pushing changes to that scan.
