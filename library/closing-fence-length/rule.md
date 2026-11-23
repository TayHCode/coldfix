---
id: closing-fence-length
title: Accept closing fences of the same length or longer, with trailing whitespace
proof: rerunnable
scope: public
category: data-and-parsing
rank: 22
featured: false
---
# Accept closing fences of the same length or longer, with trailing whitespace

A Markdown code fence closes on the same fence character repeated at least as many times as the opener, optionally followed by spaces or tabs. Do not require an exact string match with the opener, or later headings and the final record get swallowed. Add regression tests for a longer closer and a closer with trailing whitespace before pushing.
