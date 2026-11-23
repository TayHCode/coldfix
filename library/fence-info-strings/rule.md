---
id: fence-info-strings
title: Recognize Markdown fence info strings before tracking fence state
proof: rerunnable
scope: public
category: data-and-parsing
rank: 23
featured: false
summary: A Markdown fence opener can include an info string such as c++ or python3. A letters-only pattern misses these blocks and can mistake their contents for document headings.
---
# Recognize Markdown fence info strings before tracking fence state

Treat a backtick or tilde fence followed by any info string as an opener, not only lowercase letters, so python3, c++ and similar openers work. A closing fence must use the same character, be at least as long as the opener, and allow only trailing whitespace. Keep parser tests for these openers so a later section heading is still recognized.
