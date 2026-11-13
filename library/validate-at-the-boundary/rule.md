---
id: validate-at-the-boundary
title: Validate at the boundary: reject, don't coerce
proof: rerunnable
---
# Validate at the boundary: reject, don't coerce

Reject, don't coerce: parse dates strictly (a lenient parser normalizes 2026-02-31), never trim passwords, drain request bodies before rejecting, check that a validated object is bound to the identity, version or snapshot it was produced for, check expiry at use time, and check every field and array element, not just the first.

## Applies when

Route handlers, parsers, validation or schema code, and anything accepting external input or evidence and receipt objects.

## Reproduce

Send input that the parser silently normalizes (2026-02-31, a padded password, an expired approval, a malformed second array element, an unbounded body) and observe it accepted.

## Fix

Reject instead of coercing. Parse dates strictly, never trim passwords, drain bodies before rejecting, bind validated objects to the identity and version they were produced for, check expiry at use time, and validate every element.

## Verify

Add a table test with one case per rejected shape and assert a specific error for each.

## Encode it higher

Types: parse into a validated type at the edge so downstream code cannot receive raw input.
