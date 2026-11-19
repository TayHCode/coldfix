---
id: use-the-configured-ssh-alias
title: Use the configured SSH alias
proof: judged (opinion)
---
# Use the configured SSH alias

When the user specifies a configured SSH alias, use that alias for the connection and diagnosis. Do not infer an authentication failure from a raw-IP attempt that omits the configured key.

## How to check

Check that the SSH command uses the user-specified alias, or explicitly supplies the configured key when using an IP.
