---
id: exclude-author-activity
title: Exclude the PR author's own events when counting maintainer activity
proof: rerunnable
---
# Exclude the PR author's own events when counting maintainer activity

When computing maintainer activity for stalled-PR detection, drop events whose login matches the PR author before counting. Author updates must not reset the inactivity clock, as the README states. Add a test where the maintainer-author posts a comment and the PR remains a candidate.
