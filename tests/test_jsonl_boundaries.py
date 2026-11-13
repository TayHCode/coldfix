import ast
import importlib.util
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "jsonl_boundaries"
FIX = ('split JSONL with .split("\\n") and skip the empty last item, or iterate the text file object, not '
       '.splitlines(): U+0085, U+2028 and U+2029 are legal inside JSON strings, and str.splitlines() cuts records '
       'there (examples: tests/fixtures/jsonl_boundaries)')
RAW_WRITER = re.compile(r"ensure_ascii\s*=\s*False")
RAW_PRODUCER = re.compile(r"stream-json|\.claude/projects|session_index|rollout|codex exec|transcript", re.I)
TEXT_MODE = {"text", "universal_newlines", "encoding", "errors"}


def call_name(node):
    func = node.func if isinstance(node, ast.Call) else None
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def binary_open(node):
    mode = node.args[1] if isinstance(node, ast.Call) and len(node.args) > 1 else next(
        (k.value for k in getattr(node, "keywords", ()) if k.arg == "mode"), None)
    return (call_name(node) == "open" and isinstance(mode, ast.Constant) and isinstance(mode.value, str)
            and "b" in mode.value)


def bytes_process(node):
    func = node.func if isinstance(node, ast.Call) else None
    subprocess = isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "subprocess"
    return (subprocess and call_name(node) in {"run", "check_output"}
            and not any(k.arg in TEXT_MODE or k.arg is None for k in node.keywords))


def is_bytes(node, names):
    if isinstance(node, ast.Constant):
        return isinstance(node.value, bytes)
    if isinstance(node, ast.Name):
        return node.id in names["bytes"]
    if isinstance(node, ast.Subscript):
        return is_bytes(node.value, names)
    if isinstance(node, ast.IfExp):
        return is_bytes(node.body, names) and is_bytes(node.orelse, names)
    if isinstance(node, ast.Attribute):
        return (node.attr in {"stdout", "stderr"} and
                (isinstance(node.value, ast.Name) and node.value.id in names["process"]
                 or isinstance(node.value, ast.Call) and bytes_process(node.value)))
    if not isinstance(node, ast.Call):
        return False
    name = call_name(node)
    if name == "check_output":
        return bytes_process(node)
    if name == "read" and isinstance(node.func, ast.Attribute):
        handle = node.func.value
        return binary_open(handle) or isinstance(handle, ast.Name) and handle.id in names["handle"]
    return name in {"read_bytes", "encode", "b64decode", "urlsafe_b64decode", "standard_b64decode"}


def binary_names(nodes):
    names = {"bytes": set(), "handle": set(), "process": set()}
    bindings = [(t, n.value) for n in nodes if isinstance(n, ast.Assign) for t in n.targets]
    bindings += [(n.target, n.value) for n in nodes if isinstance(n, ast.AnnAssign) and n.value is not None]
    bindings += [(i.optional_vars, i.context_expr) for n in nodes if isinstance(n, (ast.With, ast.AsyncWith))
                 for i in n.items if i.optional_vars is not None]
    values = {}
    for target, value in bindings:
        if isinstance(target, ast.Name):
            values.setdefault(target.id, []).append(value)
    kinds = {"handle": binary_open, "process": lambda v: call_name(v) == "run" and bytes_process(v),
             "bytes": lambda v: is_bytes(v, names)}
    while True:
        before = sum(map(len, names.values()))
        for kind, matches in kinds.items():
            names[kind] |= {name for name, bound in values.items() if all(map(matches, bound))}
        if sum(map(len, names.values())) == before:
            return names


def splits_lines(node, split_names, names):
    return any((isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "splitlines"
                and not is_bytes(n.func.value, names))
               or (isinstance(n, ast.Name) and n.id in split_names) for n in ast.walk(node))


def decodes_bytes(arg, targets):
    return (isinstance(arg, ast.Call) and isinstance(arg.func, ast.Attribute) and arg.func.attr == "decode"
            and isinstance(arg.func.value, ast.Name)
            and arg.func.value.id in targets)


def decodes(node, targets):
    return [n.lineno for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "loads"
            and isinstance(n.func.value, ast.Name) and n.func.value.id == "json" and n.args
            and not decodes_bytes(n.args[0], targets)
            and any(isinstance(a, ast.Name) and a.id in targets for a in ast.walk(n.args[0]))]


def tier(source, package=()):
    raw = RAW_PRODUCER.search(source) or any(RAW_WRITER.search(text) for text in (source, *package))
    return "high" if raw else "low"


def embedded_scripts(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "json.loads(" in node.value:
            try:
                yield node.lineno - 1, ast.parse(node.value)
            except SyntaxError:
                continue


SCOPES = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def scope_nodes(scope):
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, SCOPES):
            stack.extend(ast.iter_child_nodes(node))


def jsonl_splits(source):
    found = []
    module = ast.parse(source)
    for offset, tree in [(0, module), *embedded_scripts(module)]:
        for scope in [n for n in ast.walk(tree) if isinstance(n, SCOPES)]:
            nodes = list(scope_nodes(scope))
            names = binary_names(nodes)
            split_names = {t.id for n in nodes if isinstance(n, ast.Assign) and splits_lines(n.value, (), names)
                           for t in n.targets if isinstance(t, ast.Name)}
            loops = [(n.iter, n.target, n) for n in nodes if isinstance(n, (ast.For, ast.AsyncFor))]
            loops += [(g.iter, g.target, n) for n in nodes
                      if isinstance(n, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)) for g in n.generators]
            for iterable, target, node in loops:
                if splits_lines(iterable, split_names, names):
                    found += [offset + line for line in decodes(node, {t.id for t in ast.walk(target) if isinstance(t, ast.Name)})]
    return sorted(set(found))


def product_sources():
    for path in sorted(ROOT.rglob("*.py")):
        parts = path.relative_to(ROOT).parts
        if parts[0] != "tests" and not any(part.startswith(".") for part in parts):
            yield path


def fixture(name):
    spec = importlib.util.spec_from_file_location(name, FIXTURES / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def package_sources(path):
    return [sibling.read_text(encoding="utf-8") for sibling in path.parent.glob("*.py") if sibling != path]


class JsonlBoundaryTests(unittest.TestCase):
    def test_product_code_splits_jsonl_only_at_newlines(self):
        offenders = [f"{path.relative_to(ROOT)}:{line} ({tier(source, package_sources(path))} tier)"
                     for path in product_sources() for source in [path.read_text(encoding="utf-8")]
                     for line in jsonl_splits(source)]
        self.assertEqual(offenders, [], FIX)

    def test_flags_exactly_the_ruleid_lines_in_the_shipped_before_and_after_fixtures(self):
        fixtures = sorted(FIXTURES.glob("*.py"))
        self.assertEqual({path.name.split("_")[0] for path in fixtures}, {"bad", "good"})
        for path in fixtures:
            with self.subTest(fixture=path.name):
                lines = path.read_text(encoding="utf-8").split("\n")
                marker = "ruleid" if path.name.startswith("bad_") else "ok"
                marked = [number + 1 for number, line in enumerate(lines, 1)
                          if line.strip() == f"# {marker}: jsonl-splitlines"]
                self.assertTrue(marked)
                self.assertEqual(jsonl_splits("\n".join(lines)), marked if marker == "ruleid" else [])

    def test_flagged_fixtures_lose_records_that_their_fixed_versions_keep(self):
        reads = {"corrections_store": lambda module, text: module.load_corrections(text),
                 "receipts_ledger": lambda module, text: module.load_receipts(text),
                 "transcript_resume": lambda module, text: [
                     json.loads(line) for line in module.rewrite_session_ids(text, "s").split("\n")[:-1]]}
        for separator in ("\u0085", "\u2028", "\u2029"):
            notes = ["plain", f"pasted{separator}note"]
            text = "".join(json.dumps({"note": note}, ensure_ascii=False) + "\n" for note in notes)
            for name, read in reads.items():
                with self.subTest(fixture=name, separator=hex(ord(separator))):
                    self.assertEqual([r["note"] for r in read(fixture(f"good_{name}"), text)], notes)
                    self.assertEqual([r["note"] for r in read(fixture(f"good_{name}"), text.rstrip("\n"))], notes)
                    try:
                        lost = [r["note"] for r in read(fixture(f"bad_{name}"), text)]
                    except ValueError:
                        lost = None
                    self.assertNotEqual(lost, notes)

    def test_bytes_splitlines_is_not_flagged(self):
        sources = [
            'with open(p, "rb") as fh:\n    chunk = fh.read(n)\nend = chunk.rfind(b"\\n")\n'
            'for line in chunk[:end].splitlines():\n    json.loads(line)\n',
            'with open(p, mode="rb") as f:\n    raw = f.read()\nfor raw_line in raw.splitlines():\n    json.loads(raw_line)\n',
            'data = open(p, "rb").read()\ncomplete = data[:9] if data else b""\n'
            'for n, line in enumerate(complete.splitlines()):\n    json.loads(line)\n',
            '[json.loads(x) for x in base64.b64decode(s).splitlines() if x.startswith(b"{")]\n',
            'for line in reversed(lines):\n    json.loads(line.decode("utf-8"))\nlines = raw.splitlines()\n',
            'for line in path.read_bytes().splitlines():\n    json.loads(line)\n',
            'out = subprocess.run(cmd, capture_output=True)\nfor line in out.stdout.splitlines():\n    json.loads(line)\n',
            'for line in subprocess.check_output(cmd).splitlines():\n    json.loads(line)\n',
        ]
        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(jsonl_splits(source), [])

    def test_text_splitlines_stays_flagged_next_to_bytes_sources(self):
        sources = [
            'raw = open(p, "rb").read()\nfor line in raw.decode().splitlines():\n    json.loads(line)\n',
            'data = open(p, "rb").read()\ndata = data.decode()\nfor line in data.splitlines():\n    json.loads(line)\n',
            'out = subprocess.run(cmd, capture_output=True, text=True)\nfor line in out.stdout.splitlines():\n'
            '    json.loads(line)\n',
            'out = c.run(cmd)\nfor line in out.stdout.splitlines():\n    json.loads(line)\n',
            'for line in subprocess.check_output(cmd, **opts).splitlines():\n    json.loads(line)\n',
            'data = b"" if flag else text\nfor line in data.splitlines():\n    json.loads(line)\n',
            'data = open(p, "rb").read()\ndata: str = data.decode()\nfor line in data.splitlines():\n    json.loads(line)\n',
            'with open(p) as fh:\n    text = fh.read()\nfor line in text.splitlines():\n    json.loads(line)\n',
            'out = run(cmd)\nfor line in out.stdout.splitlines():\n    json.loads(line)\n',
            'for line in check_output(cmd).splitlines():\n    json.loads(line)\n',
            'for line in text.splitlines():\n    json.loads(decode(line, "rot_13"))\n',
        ]
        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(jsonl_splits(source), [source.count("\n")])

    def test_raw_producers_rank_high_and_default_dumps_ranks_low(self):
        for name in ("bad_corrections_store", "bad_receipts_ledger", "bad_transcript_resume"):
            with self.subTest(fixture=name):
                self.assertEqual(tier((FIXTURES / f"{name}.py").read_text(encoding="utf-8")), "high")
        reader = "for line in text.splitlines():\n    json.loads(line)\n"
        self.assertEqual(tier(reader), "low")
        self.assertEqual(tier(reader, ["json.dumps(row, ensure_ascii=False)"]), "high")

    def test_scripts_embedded_in_strings_are_checked(self):
        embedded = 'CHECK = r"""\nlines = out.splitlines()\nfor line in lines:\n    json.loads(line)\n"""\n'
        self.assertEqual(jsonl_splits(embedded), [4])

    def test_split_names_are_tracked_per_function(self):
        unrelated = ("def a(text):\n    lines = text.splitlines()\n    return lines\n\n"
                     "def b(lines):\n    for line in lines:\n        json.loads(line)\n")
        self.assertEqual(jsonl_splits(unrelated), [])
        same = "def a(text):\n    lines = text.splitlines()\n    for line in lines:\n        json.loads(line)\n"
        self.assertEqual(jsonl_splits(same), [4])


if __name__ == "__main__":
    unittest.main()
