---
id: example-config-covers-every-key
title: Add every new rules key to rules.example.toml in the same change
proof: judged
---
# Add every new rules key to rules.example.toml in the same change

When you add a top-level key to the rules schema, add it to rules.example.toml in the same change, because a test requires the example to cover every accepted key. Give the example a safe default (for example an empty string that means off) so it still loads. Run the rules key test before pushing.
