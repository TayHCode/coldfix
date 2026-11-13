---
id: split-jsonl-on-newlines
title: Split cached JSONL on newlines, not splitlines()
proof: judged
---
# Split cached JSONL on newlines, not splitlines()

Split cached JSONL files on literal newline characters, not str.splitlines(), because splitlines() also breaks on U+0085, U+2028 and U+2029 inside valid JSON strings. Skip empty records after splitting. Before pushing, check that an event containing U+2028 still ingests without a JSONDecodeError.
