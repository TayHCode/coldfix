import ast
from pathlib import Path
import shutil
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/failure_ingest_consumers"
JSON_SCHEMA_TYPES = {"object", "array", "string", "number", "integer", "boolean", "null"}
NOT_FAILURE_INGEST = {
    ("vm02-jobs", "vm02-job"): "collect_sources.py",
    ("ingest", "transcript_correction"): "transcript_mistakes.py",
    ("claude-hook-config", "command"): "coldrecall_measure.py",
    ("noul-question", "noul"): "transcript_mistakes.py",
}
FIX = ("a producer changed without its consumer: give each new type= literal a failure_ingest.GROUPING entry "
       "(or, if it never reaches failure-ingest, a (source, type) row in NOT_FAILURE_INGEST naming its module), and keep every "
       "name other modules import from failure_ingest, in the same change")


def modules(root):
    for path in sorted(root.rglob("*.py")):
        parts = path.relative_to(root).parts
        if parts[0] not in ("tests", "build") and not any(part.startswith(".") for part in parts):
            yield path.relative_to(root).as_posix(), ast.parse(path.read_text(encoding="utf-8"))


def taken(tree):
    aliases = {alias.asname or alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
               for alias in node.names if alias.name == "failure_ingest"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "failure_ingest":
            yield from ((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in aliases:
            yield node.lineno, node.attr


def defined(tree):
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names |= {target.id for target in node.targets if isinstance(target, ast.Name)}
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names |= {(alias.asname or alias.name).split(".")[0] for alias in node.names}
    return names


def type_literals(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "dict":
            values = [keyword.value for keyword in node.keywords if keyword.arg == "type"]
        elif isinstance(node, ast.Dict):
            values = [value for key, value in zip(node.keys, node.values) if isinstance(key, ast.Constant) and key.value == "type"]
        else:
            continue
        for value in values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str) and value.value not in JSON_SCHEMA_TYPES:
                yield value.lineno, value.value


def handled(tree):
    registry = {key.value for node in tree.body if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict)
                and any(getattr(target, "id", None) == "GROUPING" for target in node.targets) for key in node.value.keys}
    chain = {comparator.value for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "grouping_content"
             for compare in ast.walk(node) if isinstance(compare, ast.Compare) and isinstance(compare.left, ast.Subscript)
             and getattr(compare.left.slice, "value", None) == "type" for comparator in compare.comparators
             if isinstance(comparator, ast.Constant)}
    return registry | chain


def drift(root, elsewhere=None):
    trees = dict(modules(root))
    consumer = trees["failure_ingest.py"]
    exempt = {(path, kind) for (_, kind), path in (elsewhere or {}).items()}
    unregistered = [f"{path}:{line}: {kind!r} has no failure_ingest.GROUPING entry" for path, tree in trees.items()
                    for line, kind in type_literals(tree) if kind not in handled(consumer) and (path, kind) not in exempt]
    missing = [f"{path}:{line}: failure_ingest has no {name!r}" for path, tree in trees.items() if path != "failure_ingest.py"
               for line, name in taken(tree) if name not in defined(consumer)]
    return sorted(unregistered) + sorted(missing)


class FailureIngestConsumerTests(unittest.TestCase):
    def test_every_producer_type_is_grouped_and_every_imported_name_exists(self):
        self.assertEqual(drift(ROOT, NOT_FAILURE_INGEST), [], FIX)

    def test_types_kept_out_of_the_registry_are_still_produced_under_another_source(self):
        produced = {(path, kind) for path, tree in modules(ROOT) for _, kind in type_literals(tree)}
        self.assertEqual({(path, kind) for (_, kind), path in NOT_FAILURE_INGEST.items()} - produced, set())
        self.assertEqual({kind for _, kind in NOT_FAILURE_INGEST} & handled(ast.parse((ROOT / "failure_ingest.py").read_text())), set())

    def test_flags_exactly_the_ruleid_lines_in_the_shipped_before_and_after_fixtures(self):
        fixtures = sorted(path for path in FIXTURES.iterdir() if path.is_dir())
        self.assertEqual({path.name.split("_")[0] for path in fixtures}, {"bad", "good"})
        for fixture in fixtures:
            with self.subTest(fixture=fixture.name):
                marker = "ruleid" if fixture.name.startswith("bad_") else "ok"
                marked = sorted(f"{path.name}:{number + 1}" for path in fixture.glob("*.py")
                                for number, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1)
                                if line.strip() == f"# {marker}: failure-ingest-consumer")
                self.assertTrue(marked)
                flagged = sorted(":".join(message.split(":")[:2]) for message in drift(fixture))
                self.assertEqual(flagged, marked if marker == "ruleid" else [])

    def test_a_builder_in_a_module_that_never_imports_failure_ingest_is_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copy(FIXTURES / "good_registry/failure_ingest.py", root)
            (root / "builders.py").write_text('def evidence(item):\n    return dict(type="new-evidence", repo=item.repo)\n')
            (root / "importer.py").write_text("from builders import evidence\nfrom failure_ingest import record\n")
            self.assertEqual(drift(root), ["builders.py:2: 'new-evidence' has no failure_ingest.GROUPING entry"])
            self.assertEqual(drift(root, {("elsewhere", "new-evidence"): "builders.py"}), [])
            self.assertEqual(drift(root, {("elsewhere", "new-evidence"): "importer.py"}),
                             ["builders.py:2: 'new-evidence' has no failure_ingest.GROUPING entry"])


if __name__ == "__main__":
    unittest.main()
