---
id: outbox-age-limit-everywhere
title: Apply the outbox age limit to enrichment failures too
proof: judged
---
# Apply the outbox age limit to enrichment failures too

Every outbox retry path, including enrichment and message-read failures, must check OUTBOX_MAX_AGE so a permanently failing item expires. Do not keep an item unconditionally just because one step failed. Add a test that a permanent enrichment failure is dropped after the max age.
