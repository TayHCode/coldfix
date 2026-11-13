---
id: normalize-crlf-before-markers
title: Normalize line endings before checking the delivery marker
proof: rerunnable
---
# Normalize line endings before checking the delivery marker

Normalize CRLF to LF before every check that inspects lesson text, including the delivery marker, registry hashes, and titles. Keep the original bytes for exported proofs and source digests. Test with a CRLF lesson file before pushing.
