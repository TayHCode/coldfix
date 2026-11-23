---
id: terminal-cloud-init-status
title: Handle terminal cloud-init statuses instead of retrying forever
proof: rerunnable
scope: public
category: ops-and-infra
rank: 20
featured: false
summary: Handle cloud-init’s recoverable-error status separately from success and fatal errors. Bound readiness retries so a guest that never becomes ready cannot hang the build.
---
# Handle terminal cloud-init statuses instead of retrying forever

Treat cloud-init status 2 (recoverable error) as a warning and continue, and fail on any other nonzero status. Bound the guest-agent readiness loop so it cannot retry forever. Run the offline-base tests before pushing.
