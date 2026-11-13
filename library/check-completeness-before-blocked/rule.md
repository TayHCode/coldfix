---
id: check-completeness-before-blocked
title: Check item completeness before inferring a blocked ending
proof: judged
---
# Check item completeness before inferring a blocked ending

Before emitting a blocked or terminal ending for a run, confirm every selected source table is untruncated, because omitted cancellation diagnostics can make a cancelled run look like a failure. Linked error items must come from completed or failed runs, and generic failed-run events must be suppressed when the item snapshot is truncated. Add a regression test with a truncated limit and a later-sorting cancellation error before pushing.
