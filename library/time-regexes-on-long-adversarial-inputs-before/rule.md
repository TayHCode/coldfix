---
id: time-regexes-on-long-adversarial-inputs-before
title: Time regexes on long adversarial inputs before merging
proof: rerunnable
---
# Time regexes on long adversarial inputs before merging

Any regex applied to unbounded imported strings must be timed against adversarial input, such as repeated separators followed by a filename, before review. Do not let alternatives rescan the whole remaining suffix at each separator, so runtime grows roughly linearly with input size. A 50 KB probe should finish well under a second.
