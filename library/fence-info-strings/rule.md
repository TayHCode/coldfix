---
id: fence-info-strings
title: Recognize Markdown fence info strings before tracking fence state
proof: rerunnable
---
# Recognize Markdown fence info strings before tracking fence state

Treat a backtick or tilde fence followed by any info string as an opener, not only lowercase letters, so python3, c++ and similar openers work. A closing fence must use the same character, be at least as long as the opener, and allow only trailing whitespace. Keep parser tests for these openers so a later section heading is still recognized.
