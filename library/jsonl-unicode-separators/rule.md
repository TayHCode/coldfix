---
id: jsonl-unicode-separators
title: Parse JSONL at newline record boundaries
proof: rerunnable
---
# Parse JSONL at newline record boundaries

Parse JSONL at newline record boundaries. Verify that Unicode separators inside strings survive ingestion unchanged.

## What went wrong

Unicode line separators inside valid JSON strings were mistaken for record delimiters. In one production ledger, a
single row carrying a raw U+0085 (NEL) broke every reader, because each one split the file with `str.splitlines()`.

## Fix

Split JSONL records only at newline delimiters and preserve other separators inside strings.
