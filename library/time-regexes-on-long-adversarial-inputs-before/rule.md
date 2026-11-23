---
id: time-regexes-on-long-adversarial-inputs-before
title: Time regexes on long adversarial inputs before merging
proof: rerunnable
scope: public
category: data-and-parsing
rank: 10
featured: false
summary: Time regular expressions on long strings that almost match. Repeated separators followed by a failing suffix expose patterns that repeatedly scan the same input.
---
# Time regexes on long adversarial inputs before merging

Any regex applied to unbounded imported strings must be timed against adversarial input, such as repeated separators followed by a filename, before review. Do not let alternatives rescan the whole remaining suffix at each separator, so runtime grows roughly linearly with input size. A 50 KB probe should finish well under a second.
