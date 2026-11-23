---
id: update-callers-and-stubs
title: Update every caller and test stub when a function signature changes
proof: rerunnable
scope: public
category: testing
rank: 17
featured: false
---
# Update every caller and test stub when a function signature changes

When you rename or remove a keyword argument, update every call site and every test stub that accepts it, and do not keep passing the removed keyword. Run the affected tests before pushing to confirm the stubs match the new signature.
