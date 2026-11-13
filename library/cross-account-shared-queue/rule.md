---
id: cross-account-shared-queue
title: Make the host-wide queue accessible across Unix accounts
proof: judged
---
# Make the host-wide queue accessible across Unix accounts

Do not rely on mkdir or open creation modes for the shared waiters directory and lock file, because the umask filters them and a first account can lock out others. Set the shared directory to sticky world-writable and the lock file to world-readable-writable explicitly, and tolerate PermissionError when removing stale tickets. Run the queue tests under a restrictive umask before pushing.
