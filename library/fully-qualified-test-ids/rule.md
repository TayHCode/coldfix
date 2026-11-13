---
id: fully-qualified-test-ids
title: Key test results by fully qualified test ID
proof: rerunnable
---
# Key test results by fully qualified test ID

When parsing or comparing test results, match each result by its fully qualified ID (module, class, and method), not the bare method name. A same-named test in another class must never validate a lesson or a regression. If a bare name is ambiguous, reject it instead of guessing.
