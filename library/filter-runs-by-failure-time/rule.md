---
id: filter-runs-by-failure-time
title: Filter workflow runs by failure time, not creation time
proof: rerunnable
---
# Filter workflow runs by failure time, not creation time

When collecting workflow runs for a time window, do not exclude runs by creation time before the window, because a rerun can fail long after it was created. Gather candidates from a lookback that covers reruns, then apply the event-time cutoff on updated_at. Check truncation separately for each conclusion listing.
