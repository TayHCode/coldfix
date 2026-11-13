---
id: pre-push-uses-pushed-tree
title: Pre-push checks must select targets from the pushed tree, not the working tree
proof: judged
---
# Pre-push checks must select targets from the pushed tree, not the working tree

The pre-push fast checks must decide which files to check from the pushed commit's tree, not from what exists in the working tree. A file deleted locally but still committed must still be checked. Run the pre-push hook tests before pushing.
