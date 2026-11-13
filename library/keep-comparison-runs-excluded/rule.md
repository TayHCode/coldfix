---
id: keep-comparison-runs-excluded
title: Comparison runs must stay excluded from staging even when a fix links to them
proof: judged
---
# Comparison runs must stay excluded from staging even when a fix links to them

Exclusion of comparison-purpose runs during staging must hold regardless of fix linkage, so linking a fix to a comparison run must not re-admit it. Apply the purpose filter after gathering linked run IDs, not only before. Run the comparison-rollup isolation tests before pushing any staging change.
