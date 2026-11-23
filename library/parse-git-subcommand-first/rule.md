---
id: parse-git-subcommand-first
title: Parse the git subcommand before applying publication restrictions
proof: rerunnable
scope: public
category: git-and-ci
rank: 25
featured: false
---
# Parse the git subcommand before applying publication restrictions

Before applying push or publication restrictions, identify the actual git executable and subcommand, including global options, so read-only commands that merely mention push in their arguments are allowed while publication subcommands stay denied.
