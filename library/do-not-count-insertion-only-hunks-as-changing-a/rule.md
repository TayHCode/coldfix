---
id: do-not-count-insertion-only-hunks-as-changing-a
title: Do not count insertion-only hunks as changing a reviewed line
proof: rerunnable
---
# Do not count insertion-only hunks as changing a reviewed line

When deciding whether a diff fixes a reviewed line, require the hunk to modify or remove that line; an insertion-only hunk adjacent to it does not count.
