---
id: worker-lifecycle-and-receipts
title: Use the documented worker lifecycle and receipt wake path
proof: judged (opinion)
---
# Use the documented worker lifecycle and receipt wake path

Use the repository’s documented worker launch, receipt, and archive lifecycle. Before going idle, ensure every active dependency has a proven automatic receipt or wake path.

## How to check

Check that workers were launched through the documented mechanism, their terminal receipts target the exact parent, and each dependency can wake the parent without polling.
