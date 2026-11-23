---
id: split-jsonl-on-newlines
title: Parse JSONL at newline record boundaries
proof: rerunnable
scope: public
category: data-and-parsing
rank: 7
featured: true
summary: Parse JSONL at newline record boundaries. Verify that Unicode separators inside strings survive ingestion unchanged.
---
# Parse JSONL at newline record boundaries

Split cached JSONL files on literal newline characters, not str.splitlines(), because splitlines() also breaks on U+0085, U+2028 and U+2029 inside valid JSON strings. Skip empty records after splitting. Before pushing, check that an event containing U+2028 still ingests without a JSONDecodeError.

## What went wrong

Unicode line separators inside valid JSON strings were mistaken for record delimiters. In one production ledger, a
single row carrying a raw U+0085 (NEL) broke every reader, because each one split the file with `str.splitlines()`.

## Fix

Split JSONL records only at newline delimiters and preserve other separators inside strings.
