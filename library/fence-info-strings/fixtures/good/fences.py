import re

OPENER = re.compile(r"^(`{3,}|~{3,})(.*)$")


def headings(markdown):
    found, fence = [], None
    for line in markdown.split("\n"):
        if fence:
            if line.startswith(fence[0] * len(fence)) and not line.lstrip(fence[0]).strip():
                fence = None
        elif OPENER.match(line):
            fence = OPENER.match(line).group(1)
        elif line.startswith("# "):
            found.append(line)
    return found
