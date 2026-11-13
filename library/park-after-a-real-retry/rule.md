---
id: park-after-a-real-retry
title: Park a retry only after a distinct retry attempt fails
proof: judged
---
# Park a retry only after a distinct retry attempt fails

Record each failed run before deciding on a retry, and do not count a second triage call on the same completed row as a new failure. Park a cell only after a separate retry attempt has actually failed. Cover the sweep-then-finish race for a single completion with a test before pushing.
