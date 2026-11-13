import ast
import functools
import importlib
import importlib.util
import json
from pathlib import Path
import sys

SKIP = {"test", "tests", "docs", "examples", "scripts", "benchmarks"}


def packages(root):
    return {path.parent for path in root.rglob("*.py") if path.stem == "__init__"}


def top_packages(root):
    found = packages(root)
    for package in sorted(found):
        parts = package.relative_to(root).parts
        if package.parent not in found and not any(p.startswith(".") or p in SKIP for p in parts):
            yield package


@functools.cache
def resolves_outside(name, root):
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, ValueError):
        return False
    if spec is None:
        return False
    locations = [spec.origin] if spec.has_location else list(spec.submodule_search_locations or [])
    return not any(Path(location).resolve().is_relative_to(root) for location in locations)


def provides(module, names):
    try:
        found = importlib.import_module(module)
        return all(name == "*" or hasattr(found, name)
                   or hasattr(found, "__path__") and importlib.util.find_spec(module + "." + name) is not None
                   for name in names)
    except Exception:
        return None


def children(folder):
    modules = {path.stem for path in folder.glob("*.py")}
    subpackages = {path.name for path in packages(folder) if path.parent == folder}
    return (modules | subpackages) - {"__init__", "__main__"}


def bare_internal_imports(package, root):
    top = children(package)
    for module in sorted(package.rglob("*.py")):
        if any(p.startswith(".") or p in SKIP for p in module.relative_to(package).parts[:-1]):
            continue
        own = (top | children(module.parent)) - {package.name}
        installed = {name for name in own if resolves_outside(name, root)}
        where = str(module.relative_to(root))
        try:
            tree = ast.parse(module.read_bytes())
        except (SyntaxError, ValueError):
            yield "unparsable module", where
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                heads, found = {alias.name.split(".")[0] for alias in node.names}, True
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                heads = {node.module.split(".")[0]}
                found = not heads & installed or provides(node.module, [alias.name for alias in node.names])
            else:
                continue
            if heads & (own - installed) or heads & installed and found is False:
                yield "bare import of a module inside its own package", f"{where}:{node.lineno}"
            elif heads & installed and found is None:
                yield "installed module named like its own module could not be inspected", f"{where}:{node.lineno}"


here = Path(__file__).resolve().parent
sys.path[:] = [entry for entry in sys.path if Path(entry or ".").resolve() != here]
root = Path.cwd().resolve()
tops = list(top_packages(root))
found = [finding for package in tops for finding in bare_internal_imports(package, root)]
for reason, where in found:
    print(reason + ":", where, file=sys.stderr)
if any(reason.startswith("bare import of") for reason, _ in found):
    print(json.dumps({"status": "FAIL", "failure": "bare-internal-import"}))
    sys.exit(1)
if not tops or found:
    print(json.dumps({"status": "INCOMPLETE"}))
    sys.exit(2)
print(json.dumps({"status": "PASS"}))
