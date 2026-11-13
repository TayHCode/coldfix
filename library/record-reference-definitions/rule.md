---
id: record-reference-definitions
title: Record reference definitions even when non-reference text follows them
proof: rerunnable
---
# Record reference definitions even when non-reference text follows them

Do not stop recording link reference definitions because non-reference text follows them without a blank line, or because the next line is indented. Only a definition that spans multiple lines should be deferred to a later pass. Before pushing, check that a reference followed directly by a paragraph still resolves its links and renders the paragraph.

## Source

Maintainer review: https://github.com/Python-Markdown/markdown/pull/1678#discussion_r4390408250
