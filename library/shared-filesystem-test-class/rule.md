---
id: shared-filesystem-test-class
title: Put filesystem-behavior tests in the shared fake_filesystem_test class
proof: rerunnable
---
# Put filesystem-behavior tests in the shared fake_filesystem_test class

Tests of os or path behavior that should match the real filesystem belong in the shared fake_filesystem_test class, so they run against both the fake and real filesystems. Do not add them to fake_filesystem_unittest_test.py, which covers only the unittest base class. Move and adapt such a test for both backends before pushing.

## Source

Maintainer review: https://github.com/pytest-dev/pyfakefs/pull/1349#discussion_r4241453974
