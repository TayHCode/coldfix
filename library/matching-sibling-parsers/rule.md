---
id: matching-sibling-parsers
title: Use the same finditer loop pattern for parallel reference and footnote definition code
proof: judged
---
# Use the same finditer loop pattern for parallel reference and footnote definition code

When two block processors parse similar definitions, such as link references and footnotes, use the same iteration pattern in both. If one uses `for m in RE.finditer(...)` with a break condition, the other should not use a manual `search` loop with different state handling. Review the sibling processor for matching logic before pushing a change to either one.

## Source

Maintainer review: https://github.com/Python-Markdown/markdown/pull/1678#discussion_r4390254916
