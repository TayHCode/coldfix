---
id: include-every-tombstone-alias-in-receipt
title: Include every tombstone alias in receipt identity extraction
proof: replayed
---
# Include every tombstone alias in receipt identity extraction

When a removal path tombstones identifiers, the receipt identity extraction must hash every alias that path writes, not a subset. Add one test per alias asserting that a tombstoned identifier still matches its receipt after the reference text is paraphrased. Do not push a removal change until those per-alias tests pass.
