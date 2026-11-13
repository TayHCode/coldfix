---
id: reject-malformed-facts
title: Reject malformed facts JSON with a controlled error
proof: judged
---
# Reject malformed facts JSON with a controlled error

Reject malformed or excessively nested facts JSON with a controlled CLI input error. Preserve the existing lesson history on rejection.

## What went wrong

Malformed facts JSON escaped the CLI boundary as a traceback.

## Fix

Translate JSON parse and recursion errors into a controlled input error.
