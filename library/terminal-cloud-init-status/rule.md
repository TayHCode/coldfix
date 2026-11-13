---
id: terminal-cloud-init-status
title: Handle terminal cloud-init statuses instead of retrying forever
proof: judged
---
# Handle terminal cloud-init statuses instead of retrying forever

Treat cloud-init status 2 (recoverable error) as a warning and continue, and fail on any other nonzero status. Bound the guest-agent readiness loop so it cannot retry forever. Run the offline-base tests before pushing.
