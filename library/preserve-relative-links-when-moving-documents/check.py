import json
import os
import re
import sys

FAILURE = "broken-relative-link"
LINK = re.compile(r"\]\(([^)\s]+)\)")


def broken_links(root):
    documents = [os.path.join(folder, name) for folder, _, names in os.walk(root) for name in names if name.endswith(".md")]
    if not documents:
        raise FileNotFoundError("no Markdown documents")
    broken = []
    for document in documents:
        with open(document, encoding="utf-8") as stream:
            targets = LINK.findall(stream.read())
        for target in targets:
            path = target.split("#", 1)[0]
            if not path or re.match(r"^[a-z][a-z0-9+.-]*:", path) or path.startswith("/"):
                continue
            if not os.path.exists(os.path.join(os.path.dirname(document), path)):
                broken.append([os.path.relpath(document, root), target])
    return broken


def main():
    try:
        broken = broken_links(os.getcwd())
    except Exception as error:
        print(json.dumps({"status": "INCOMPLETE", "reason": repr(error)}))
        return 2
    print(json.dumps({"status": "FAIL", "failure": FAILURE, "links": broken} if broken else {"status": "PASS"}))
    return 1 if broken else 0


sys.exit(main())
