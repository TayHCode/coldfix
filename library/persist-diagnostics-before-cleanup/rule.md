---
id: persist-diagnostics-before-cleanup
title: Persist setup-failure diagnostics before deleting the guest
proof: judged
scope: public
category: ops-and-infra
rank: 30
featured: false
summary: Copy the failed command and its output out of the sandbox before removing it. Exercise the setup-failure path and check that the log survives cleanup.
---
# Persist setup-failure diagnostics before deleting the guest

Before deleting a guest or sandbox, write the failed command, captured stdout and stderr, and the partial measurement record to the output directory. An exception in a setup step must not skip this persistence, so run it on every failure path before cleanup. Cover a mocked transfer failure in a test that asserts the log and receipt exist before deletion.

## Bad

```python
guest.remove()
guest.copy_log(destination)
```

## Good

```python
try:
    guest.copy_log(destination)
finally:
    guest.remove()
```
