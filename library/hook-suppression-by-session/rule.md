---
id: hook-suppression-by-session
title: Isolate hook suppression by session
proof: judged
---
# Isolate hook suppression by session

A once-only hook run with a missing or blank session id must deliver every time and must not write persistent suppression state; delivered state is keyed by a digest of the session id.

## What went wrong

The once option wrote suppression state even when the host session id was missing or blank, and sanitized session ids that could collide, so later sessions could be suppressed.

## Fix

Deliver without suppression when the session id is not a non-blank string, and name the delivered-state file by a digest of the session id.
