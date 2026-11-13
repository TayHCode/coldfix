---
id: clear-facts-restores-rule
title: Clearing facts restores whole-rule authoring
proof: judged
---
# Clearing facts restores whole-rule authoring

An empty facts list must clear atomic excerpts and restore whole-rule authoring. Remove stale excerpts and invalidate proof when revising the rule.

## What went wrong

Clearing atomic facts retained an invalid empty facts collection and rejected whole-rule revisions.

## Fix

Remove the optional facts field when the authored list is empty.
