---
id: compile-from-git-root
title: Resolve the Git root before compiling from a subdirectory
proof: judged
---
# Resolve the Git root before compiling from a subdirectory

Resolve the Git root before compiling lessons from a repository subdirectory. Write the image at that root and execute witnesses there.

## What went wrong

Compiling from a nested directory produced a separate image and missed registered root lessons.

## Fix

Resolve the repository root before reading rules or writing the image.
