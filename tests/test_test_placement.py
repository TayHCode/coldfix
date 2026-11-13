import ast
import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "test_placement"
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
RUNNER_FILES = ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", "noxfile.py", "Makefile", "requirements*.txt",
                ".github/workflows/*")


def uses_pytest(root):
    root = Path(root)
    named = any("pytest" in path.read_text(encoding="utf-8", errors="replace").lower()
                for pattern in RUNNER_FILES for path in root.glob(pattern) if path.is_file())
    return named or any(not any(part.startswith(".") for part in path.relative_to(root).parts)
                             for path in root.rglob("conftest.py"))


def base_names(cls):
    return {b.attr if isinstance(b, ast.Attribute) else getattr(b, "id", "") for b in cls.bases}


def referenced_names(node):
    return ({n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            | {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)})


def returns_value(function):
    return any(isinstance(n, ast.Return) and n.value is not None for n in ast.walk(function))


def nested_functions(function):
    stack = list(ast.iter_child_nodes(function))
    while stack:
        node = stack.pop()
        if isinstance(node, FUNCTIONS):
            yield node
        elif not isinstance(node, (ast.ClassDef, ast.Lambda)):
            stack.extend(ast.iter_child_nodes(node))


def misplaced_tests(module, pytest=False):
    classes = [n for n in ast.walk(module) if isinstance(n, ast.ClassDef)]
    if not any(name.endswith("TestCase") for cls in classes for name in base_names(cls)):
        return []
    hits = []
    module_refs = referenced_names(module)
    used_as_base = set().union(*(base_names(c) for c in classes))
    for node in module.body:
        if (isinstance(node, FUNCTIONS) and node.name.startswith("test") and not pytest
                and node.name not in module_refs and not returns_value(node)):
            hits.append((node.lineno, f"{node.name} is a module-level function in a unittest file; unittest never "
                                      "runs it; move it into a TestCase"))
    for cls in classes:
        if any(name.endswith("TestCase") for name in base_names(cls)):
            for method in (m for m in cls.body if isinstance(m, FUNCTIONS)):
                refs = referenced_names(method)
                hits += [(inner.lineno, f"{inner.name} is defined inside {cls.name}.{method.name}; it never runs; "
                                        "dedent it") for inner in nested_functions(method)
                         if inner.name.startswith("test") and inner.name not in refs and not inner.decorator_list]
        elif (cls in module.body and not cls.name.startswith("Test") and base_names(cls) <= {"object"}
              and cls.name not in used_as_base and cls.name not in module_refs):
            hits += [(m.lineno, f"{cls.name}.{m.name} is on a class that is not a TestCase; it never runs; "
                                "subclass unittest.TestCase") for m in cls.body
                     if isinstance(m, FUNCTIONS) and m.name.startswith("test")]
    return sorted(hits)


def fixture_tests(name):
    spec = importlib.util.spec_from_file_location(name, FIXTURES / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return unittest.TestLoader().loadTestsFromModule(module).countTestCases()


class TestPlacementTests(unittest.TestCase):
    def test_coldfix_tests_are_all_collected(self):
        pytest = uses_pytest(ROOT)
        offenders = [f"{path.relative_to(ROOT)}:{line}: {message}" for path in sorted((ROOT / "tests").glob("test*.py"))
                     for line, message in misplaced_tests(ast.parse(path.read_text(encoding="utf-8")), pytest)]
        self.assertEqual(offenders, [])

    def test_flags_exactly_the_ruleid_lines_in_the_shipped_before_and_after_fixtures(self):
        fixtures = sorted(FIXTURES.glob("*.py"))
        self.assertEqual({path.name.split("_")[0] for path in fixtures}, {"bad", "good"})
        for path in fixtures:
            with self.subTest(fixture=path.name):
                lines = path.read_text(encoding="utf-8").split("\n")
                marker = "ruleid" if path.name.startswith("bad_") else "ok"
                marked = [number + 1 for number, line in enumerate(lines, 1)
                          if line.strip() == f"# {marker}: test-placement"]
                self.assertTrue(marked)
                found = [line for line, _ in misplaced_tests(ast.parse("\n".join(lines)))]
                self.assertEqual(found, marked if marker == "ruleid" else [])

    def test_flagged_fixtures_lose_exactly_the_flagged_tests(self):
        for path in sorted(FIXTURES.glob("bad_*.py")):
            with self.subTest(fixture=path.name):
                flagged = len(misplaced_tests(ast.parse(path.read_text(encoding="utf-8"))))
                good = fixture_tests("good_" + path.stem[4:])
                self.assertEqual(fixture_tests(path.stem), good - flagged)

    def test_module_level_tests_count_only_under_unittest(self):
        source = "class A(unittest.TestCase):\n    pass\n\ndef test_orphan():\n    assert True\n"
        self.assertEqual([line for line, _ in misplaced_tests(ast.parse(source))], [4])
        self.assertEqual(misplaced_tests(ast.parse(source), pytest=True), [])

    def test_helpers_named_test_are_not_flagged(self):
        sources = [
            "def test_render(path):\n    assert path\n\nclass A(unittest.TestCase):\n"
            "    def test_all(self):\n        test_render('x')\n",
            "def test_profile():\n    return {'a': 1}\n\nclass A(unittest.TestCase):\n    pass\n",
            "class A(unittest.TestCase):\n    def test_raises(self):\n        with self.assertRaises(KeyError):\n"
            "            @cache.memoize\n            def test():\n                pass\n",
        ]
        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(misplaced_tests(ast.parse(source)), [])

    def test_runner_is_read_from_config_or_conftest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertFalse(uses_pytest(root))
            (root / "pyproject.toml").write_text("[tool.ruff]\n")
            (root / ".github" / "workflows").mkdir(parents=True)
            (root / ".github" / "workflows" / "ci.yml").write_text("run: python -m unittest\n")
            self.assertFalse(uses_pytest(root))
            (root / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
            self.assertTrue(uses_pytest(root))
            (root / "pyproject.toml").write_text("[tool.ruff]\n")
            (root / "requirements-dev.txt").write_text("pytest==8.3.3\n")
            self.assertTrue(uses_pytest(root))
            (root / "requirements-dev.txt").unlink()
            (root / ".github" / "workflows" / "ci.yml").write_text("run: pytest -q\n")
            self.assertTrue(uses_pytest(root))
            (root / ".github" / "workflows" / "ci.yml").unlink()
            (root / "pkg").mkdir()
            (root / "pkg" / "conftest.py").write_text("")
            self.assertTrue(uses_pytest(root))


if __name__ == "__main__":
    unittest.main()
