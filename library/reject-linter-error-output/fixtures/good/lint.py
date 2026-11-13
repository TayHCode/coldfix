import re

DIAGNOSTIC = re.compile(r"^(usage:|Error:)", re.M)


def has_findings(exit_code, output):
    return exit_code == 1 and not DIAGNOSTIC.search(output)
