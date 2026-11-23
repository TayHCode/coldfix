---
id: reject-linter-error-output
title: Reject usage and error diagnostics in linter output
proof: rerunnable
scope: public
category: git-and-ci
rank: 8
featured: false
---
# Reject usage and error diagnostics in linter output

Before accepting exit code 1 as expected lint findings, scan all combined stdout and stderr for usage or bare `Error:` diagnostics. Accept the undocumented-exit exception only when no such operational diagnostic appears anywhere in the output.
