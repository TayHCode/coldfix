---
id: once-only-recall-per-session
title: Isolate once-only recall by session
proof: judged
---
# Isolate once-only recall by session

Isolate once-only recall by a valid session identifier. Preserve distinct session identities and deliver without persistent suppression when the identifier is absent.

## What went wrong

Lossy session filenames collided, and absent session identifiers shared suppression.

## Fix

Hash valid session identities and bypass persistent suppression for absent identities.
