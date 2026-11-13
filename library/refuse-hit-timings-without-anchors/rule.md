---
id: refuse-hit-timings-without-anchors
title: Refuse hit timings when no fact is anchored to a path
proof: judged
---
# Refuse hit timings when no fact is anchored to a path

Benchmarks must not fall back to the miss path as the hit path when the table has no anchors. If no fact is anchored, fail before writing the report instead of emitting hit timings with facts_per_hit at zero.
