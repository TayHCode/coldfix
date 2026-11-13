---
id: carry-parsed-values-forward
title: Carry parsed values forward instead of re-reading the source
proof: judged
---
# Carry parsed values forward instead of re-reading the source

When a later step needs a value that was already parsed while validating a source file, carry that value forward from the first read. Do not re-read the source file at write time, because the file may have changed or be missing on a recheck path. Add a test that exercises the recheck path and checks the written value.
