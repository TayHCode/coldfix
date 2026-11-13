---
id: tests-must-test
title: A test must exercise the failure it names
proof: rerunnable
---
# A test must exercise the failure it names

A test must exercise the failure it names (a "malformed 2xx" test needs a malformed body), avoid racing deadlines, assert specific values rather than aggregates, keep the imports it uses, and be registered where the runner finds it.

## Applies when

Test files and test-runner configuration.

## Reproduce

Revert the code under test so it returns nothing: the test still passes. Or define a test where the runner never collects it: the suite reports success without running it.

## Fix

Make the test feed the exact failure it names, assert specific values, avoid racing deadlines, keep its imports, and put it where the runner collects it.

## Verify

Mutate the code under test and confirm the test fails. Run the test alone and in the full suite, and confirm the runner's count includes it.

## Encode it higher

A mutation spot-check in CI for changed files, or a lint that bans assertion-free and uncollected tests.
