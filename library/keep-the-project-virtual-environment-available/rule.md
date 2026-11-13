---
id: keep-the-project-virtual-environment-available
title: Keep the project virtual environment available in snapshots
proof: judged
---
# Keep the project virtual environment available in snapshots

When checking exported commit snapshots, preserve access to the checkout's `.venv` or `venv` so tools resolve the same installed dependencies. Include deleted or dirty pushed files in the snapshot check rather than dropping them because they are absent from the working tree.
