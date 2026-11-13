#!/usr/bin/env python3
"""Test fixture: print a wiki-style overview of a Python repository checkout (file tree, modules, signatures)."""
import ast
from pathlib import Path
import subprocess
import sys


def files(root):
    listed = subprocess.run(["git", "-C", str(root), "ls-files"], capture_output=True, text=True, check=True).stdout
    return sorted(name for name in listed.split("\n") if name)


def first_line(node):
    doc = ast.get_docstring(node)
    return doc.strip().split("\n")[0] if doc else ""


def describe(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    lines = [first_line(tree)] if first_line(tree) else []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            arguments = ast.unparse(node.args) if isinstance(node, ast.FunctionDef) else ""
            kind = "class" if isinstance(node, ast.ClassDef) else "def"
            lines.append(f"- `{kind} {node.name}({arguments})` (line {node.lineno}) {first_line(node)}".rstrip())
    return lines


def main(root):
    root = Path(root)
    names = files(root)
    out = ["# Repository wiki", "", "## Files", ""] + [f"- `{name}`" for name in names]
    for name in names:
        if name.endswith(".py"):
            out += ["", f"## {name}", ""] + describe(root / name)
        elif name == "README.md":
            out += ["", "## README sections", ""] + [f"- {line.lstrip('# ')}" for line in (root / name).read_text(encoding="utf-8").split("\n") if line.startswith("#")]
    print("\n".join(out))


if __name__ == "__main__":
    main(sys.argv[1])
