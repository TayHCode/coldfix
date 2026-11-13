---
id: resolve-tools-before-restricting-path
title: Resolve tools before restricting the child PATH
proof: judged
---
# Resolve tools before restricting the child PATH

When building a sanitized replay environment, resolve the absolute paths of tools such as tsc and node with shutil.which before replacing PATH, pass those absolute paths to the child, and invoke the tools by absolute path. Do not restrict PATH first, because the toolchain then reports as unavailable.
