---
id: systemd-path-escaping
title: Do not wrap PathChanged= values in JSON quotes
proof: rerunnable
---
# Do not wrap PathChanged= values in JSON quotes

systemd's PathChanged= does not strip JSON-style quotes, so the quoting used for Environment= and ExecStart= must not be reused for path settings. Serialize Path* values with systemd escaping (for example \x20 for spaces and \x5c for backslashes, with % doubled) and check the generated unit with systemd-analyze verify before installing it. Keep the empty assignment that clears the original watch paired with a valid absolute replacement.
