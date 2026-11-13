OURS, BASE, THEIRS = "<" * 7, "=" * 7, ">" * 7

MODULE = """import json
{imports}

def run(path):
    with open(path) as stream:
        return json.load(stream)
"""

FIXTURES = {
    "bad": {
        "loop.py": MODULE.format(imports=f"{OURS} HEAD\nimport os\n{BASE}\nimport os\nimport shutil\n"
                                         f"{THEIRS} 1a2b3c4 (Fence tests per candidate)\n"),
        "CHANGELOG.md": f"# Changes\n\n{THEIRS}\n- Fence tests per candidate.\n",
    },
    "good": {
        "loop.py": MODULE.format(imports="import os\nimport shutil\n"),
        "CHANGELOG.md": f"Changes\n{BASE}\n\n- Fence tests per candidate.\n",
    },
}
