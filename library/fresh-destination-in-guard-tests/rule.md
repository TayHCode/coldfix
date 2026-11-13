---
id: fresh-destination-in-guard-tests
title: Keep destination conflicts from masking privacy-screen tests
proof: judged
---
# Keep destination conflicts from masking privacy-screen tests

Use a fresh destination in tests that expect a privacy-screen rejection, so an existing destination does not trigger an earlier guard. Run the test suite before pushing.
