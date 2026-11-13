---
id: revision-rows-for-receipt-shas
title: Create revision rows for SHAs that appear only in stored receipts
proof: judged
---
# Create revision rows for SHAs that appear only in stored receipts

Before scoring, every SHA recorded in a stored receipt for a collected PR must get a revision row, even when that SHA is absent from the current PR commits and retained review events. Receipt-derived rows must count toward rows, label totals, and the unlabeled score count.
