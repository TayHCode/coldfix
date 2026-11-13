---
id: hook-tests-stay-offline
title: Git hook install tests must not contact Codex
proof: judged
---
# Git hook install tests must not contact Codex

Installer tests that only verify git hooks must disable Codex trust and fail if the Codex app server is launched.
