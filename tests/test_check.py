import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import coldfix

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "library"))
import rerun

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "coldfix.py"
SCANS = ("conflict-markers", "jsonl-unicode-separators", "tests-must-test", "authz-everywhere", "clean-checkout",
         "cross-account-shared-queue", "errors-are-states", "no-environment-assumptions",
         "preserve-relative-links-when-moving-documents", "secrets-and-private-data", "systemd-path-escaping",
         "time-regexes-on-long-adversarial-inputs-before", "fence-info-strings", "closing-fence-length",
         "normalize-crlf-before-markers", "filter-runs-by-failure-time", "fully-qualified-test-ids",
         "reject-linter-error-output", "validate-at-the-boundary", "verify-the-right-thing", "parse-git-subcommand-first",
         "runnable-commands", "hook-suppression-by-session", "once-only-recall-per-session", "inline-review-edits-after-merge",
         "resolve-tools-before-restricting-path", "stream-guest-output-to-log", "terminal-cloud-init-status",
         "fetch-the-current-base-before-creating-the-guard", "pre-push-uses-pushed-tree", "update-callers-and-stubs",
         "reject-malformed-facts", "use-the-triggering-invocation-id")
PYTHON_SCANS = {"jsonl-unicode-separators", "tests-must-test", "authz-everywhere", "cross-account-shared-queue",
                "errors-are-states", "no-environment-assumptions", "time-regexes-on-long-adversarial-inputs-before",
                "hook-suppression-by-session", "once-only-recall-per-session", "inline-review-edits-after-merge",
                "resolve-tools-before-restricting-path", "stream-guest-output-to-log", "terminal-cloud-init-status",
                "fetch-the-current-base-before-creating-the-guard", "update-callers-and-stubs", "reject-malformed-facts"}
OPEN, CLOSE = "<" * 7, ">" * 7


class CheckTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tree = Path(temporary.name) / "tree"
        self.tree.mkdir()

    def write(self, files):
        for name, text in files.items():
            (self.tree / name).parent.mkdir(parents=True, exist_ok=True)
            (self.tree / name).write_text(text)

    def check(self, *args, env=None):
        result = subprocess.run([sys.executable, str(CLI), "check", *args], cwd=self.tree,
                                capture_output=True, text=True, timeout=120, env=env)
        self.assertNotIn("Traceback", result.stderr)
        return result

    def lessons(self, env=None):
        result = self.check("--json", env=env)
        return result.returncode, {row["lesson"]: row for row in json.loads(result.stdout)["lessons"]}

    def assert_scan(self, lesson, bad, good, hits):
        self.write(bad)
        code, rows = self.lessons()
        self.assertEqual((code, rows[lesson]["status"], rows[lesson]["hits"]), (1, "FAIL", hits))
        shutil.rmtree(self.tree)
        self.tree.mkdir()
        self.write(good)
        code, rows = self.lessons()
        self.assertEqual((code, rows[lesson]["status"], rows[lesson]["hits"]), (0, "PASS", []))

    def test_module_level_unittest_test_is_a_hit(self):
        self.assert_scan(
            "tests-must-test",
            {"tests/test_flags.py": "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        pass\n\n\n"
                                    "def test_never_runs():\n    assert False\n",
             "tests/test_orphan.py": "import unittest\n\n\ndef test_orphan():\n    test_orphan()\n\n\n"
                                     "def helper():\n    test_orphan()\n\n\n"
                                     "class Helper:\n    def test_helper(self):\n        test_orphan()\n\n\n"
                                     "class T(unittest.TestCase):\n    def setUp(self):\n        test_orphan()\n"},
            {"tests/test_flags.py": "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        pass\n\n\n"
                                    "def tests_for(paths):\n    return paths\n",
             "tests/test_shared.py": "import unittest\n\n\ndef test_shared():\n    assert True\n\n\n"
                                     "class T(unittest.TestCase):\n    def test_shared(self):\n        test_shared()\n",
             "tests/test_pytest.py": "import unittest\nimport pytest\n\n\nclass T(unittest.TestCase):\n    pass\n\n\n"
                                     "def test_collected_by_pytest():\n    pass\n",
             "tests/test_mock.py": "from unittest import mock\n\n\ndef test_pytest_style():\n    assert mock\n"},
            ["tests/test_flags.py:9", "tests/test_orphan.py:4"])

    def test_pytest_repository_has_no_uncollected_module_tests(self):
        self.write({"conftest.py": "",
                    "tests/test_flags.py": "import unittest\n\n\nclass T(unittest.TestCase):\n    pass\n\n\n"
                                           "def test_collected_by_pytest():\n    pass\n"})
        row = self.lessons()[1]["tests-must-test"]
        self.assertEqual((row["status"], row["hits"]), ("PASS", []))

    def test_unresolved_merge_marker_is_a_hit(self):
        self.assert_scan(
            "conflict-markers",
            {"CHANGELOG.md": f"# Changes\n{OPEN} HEAD\nours\n=======\ntheirs\n{CLOSE} topic\n", "orphan.txt": f"a\n{CLOSE}\n"},
            {"CHANGELOG.md": "# Changes\n=======\nbanner only\n<<<< four is fine\n"},
            ["CHANGELOG.md:2", "CHANGELOG.md:6", "orphan.txt:2"])

    def test_jsonl_split_with_splitlines_is_a_hit(self):
        self.assert_scan(
            "jsonl-unicode-separators",
            {"ingest.py": "import json\n\n\ndef read(text):\n    rows = []\n    for line in text.splitlines():\n"
                          "        rows.append(json.loads(line))\n    return rows\n\n\n"
                          "def read_all(text):\n    return [json.loads(row) for row in text.splitlines() if row]\n",
             "aliased.py": "import json as j\n\n\ndef read(text):\n    lines = text.splitlines()\n"
                           "    return [j.loads(line) for line in lines]\n",
             "fragment.py": "def read(text):\n    return [json.loads(x) for x in text.splitlines()]\n",
             "numbered.py": "import json\n\n\ndef read(content):\n    for number, line in enumerate(content.splitlines(), 1):\n"
                            "        yield number, json.loads(line)\n",
             "latest.py": "import json\n\n\ndef last(text):\n    for line in reversed(text.splitlines()):\n"
                          "        return json.loads(line)\n"},
            {"ingest.py": "import json\nimport yaml\n\n\ndef read(text):\n    return [json.loads(line) for line in text.split('\\n') if line]\n\n\n"
                          "def names(text):\n    return [line.strip() for line in text.splitlines()]\n\n\n"
                          "def other(text):\n    return [yaml.loads(line) for line in text.splitlines()]\n\n\n"
                          "def split(text):\n    lines = text.splitlines()\n    return lines\n\n\n"
                          "def parse(lines):\n    return [json.loads(line) for line in lines]\n\n\n"
                          "def reassigned(text, records):\n    lines = text.splitlines()\n    lines = records\n"
                          "    return [json.loads(line) for line in lines]\n\n\n"
                          "def numbered(text):\n    return [(n, json.loads(line)) for n, line in enumerate(text.split('\\n'), 1)]\n\n\n"
                          "def indexed(text, metadata):\n    return [json.loads(metadata[i]) for i, line in enumerate(text.splitlines())]\n"},
            ["aliased.py:5", "fragment.py:2", "ingest.py:6", "ingest.py:12", "latest.py:5", "numbered.py:5"])

    def test_path_prefix_check_without_a_segment_boundary_is_a_hit(self):
        self.assert_scan(
            "authz-everywhere",
            {"access.py": 'PUBLIC = "/static"\n\n\ndef is_public(path):\n    return path.startswith(PUBLIC)\n',
             "rbac.ts": 'if (pathname.startsWith("/parent")) return "parent";\n',
             "admin.py": 'def admin(path):\n    return path.startswith(("/admin/", "/api/admin"))\n'},
            {"access.py": 'PUBLIC = "/static"\n\n\ndef is_public(path):\n    return path == PUBLIC or path.startswith(PUBLIC + "/")\n\n\n'
                          'def comment(line):\n    return line.startswith("/*") or line.startswith("/")\n',
             "rbac.ts": 'if (pathname === "/parent" || pathname.startsWith("/parent/")) return "parent";\n'},
            ["access.py:5", "admin.py:2", "rbac.ts:1"])

    def test_worktree_folder_missing_from_the_committed_gitignore_is_a_hit(self):
        self.assert_scan(
            "clean-checkout",
            {".gitignore": "__pycache__/\n", ".worktrees/probe/x": ""},
            {".gitignore": "__pycache__/\n/.worktrees/\n", ".worktrees/probe/x": ""},
            [".gitignore:1"])
        shutil.rmtree(self.tree / ".worktrees")
        (self.tree / ".gitignore").unlink()
        self.assertEqual(self.lessons()[1]["clean-checkout"]["status"], "PASS")

    def test_shared_mode_left_to_the_umask_is_a_hit(self):
        self.assert_scan(
            "cross-account-shared-queue",
            {"queue.py": "import os\n\n\ndef queue(root):\n    waiters = root / 'waiters'\n    waiters.mkdir(mode=0o777, exist_ok=True)\n\n\n"
                         "def lock(path):\n    return os.open(path, os.O_CREAT | os.O_RDWR, 0o666)\n\n\n"
                         "def mixed(root):\n    waiters = root / 'w'\n    waiters.mkdir(mode=0o777)\n    (root / 'p').chmod(0o600)\n\n\n"
                         "def tree(path):\n    Path(path).mkdir(0o777)\n\n\n"
                         "def other(path, fd):\n    handle = os.open(path, os.O_CREAT, 0o777)\n    os.fchmod(fd, 0o600)\n    return handle\n\n\n"
                         "def keywords(a, b):\n    os.mkdir(path=a, mode=0o777)\n    os.chmod(path=b, mode=0o600)\n\n\n"
                         "def shifted(path):\n    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o666)\n    os.fchmod(fd + 1, 0o600)\n"},
            {"queue.py": "import os\nimport webbrowser\n\n\ndef queue(root):\n    waiters = root / 'waiters'\n"
                         "    waiters.mkdir(mode=0o1777, exist_ok=True)\n    waiters.chmod(0o1777)\n\n\n"
                         "def lock(path):\n    handle = os.open(path, os.O_CREAT | os.O_RDWR, 0o666)\n    os.fchmod(handle, 0o666)\n"
                         "    return handle\n\n\ndef private(path):\n    os.makedirs(path, 0o755)\n    webbrowser.open(path, 2)\n\n\n"
                         "def flags(path):\n    return os.open(path, 2)\n\n\n"
                         "def locked(path):\n    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o666)\n"
                         "    with os.fdopen(fd, 'r+') as handle:\n        os.fchmod(handle.fileno(), 0o666)\n\n\n"
                         "def same(a):\n    os.mkdir(path=a, mode=0o777)\n    os.chmod(path=a, mode=0o777)\n"},
            ["queue.py:6", "queue.py:10", "queue.py:15", "queue.py:20", "queue.py:24", "queue.py:30", "queue.py:35"])

    def test_gate_that_opens_on_anything_but_fail_is_a_hit(self):
        self.assert_scan(
            "errors-are-states",
            {"gate.py": "def gate_opens(check):\n    return check() != 'FAIL'\n\n\n"
                        "def advance(check):\n    if check() != 'FAIL':\n        return True\n    return False\n",
             "gate.ts": "export function ok(run) {\n  return run.conclusion !== 'failure';\n}\n"
                        "export function go(s) {\n  if (s !== 'failed') return true;\n  return false;\n}\n"},
            {"gate.py": "def gate_opens(check):\n    return check() == 'PASS'\n\n\ndef order(rows):\n"
                        "    return sorted(rows, key=lambda row: row != 'FAIL')\n\n\ndef note(status):\n"
                        "    if status != 'FAIL':\n        print(status)\n",
             "gate.ts": "export function ok(run) {\n  return run.conclusion === 'success';\n}\n"},
            ["gate.py:2", "gate.py:6", "gate.ts:2", "gate.ts:5"])

    def test_test_file_the_runner_never_discovers_is_a_hit(self):
        case = "import unittest\n\n\nclass FlagTests(unittest.TestCase):\n    def test_yes(self):\n        pass\n"
        self.assert_scan(
            "no-environment-assumptions",
            {"tests/check_flags.py": case, "tests/test_ok.py": case},
            {"tests/test_flags.py": case, "tests/flags_test.py": case, "tests/flags_check.py": case,
             "setup.cfg": "[tool:pytest]\npython_files = *_check.py\n",
             "tests/base.py": "import unittest\n\n\nclass Base(unittest.TestCase):\n    def setUp(self):\n        pass\n",
             "tests/run_me.py": case + "\n\nif __name__ == '__main__':\n    unittest.main()\n"},
            ["tests/check_flags.py:4"])

    def test_vitest_spec_outside_every_include_is_a_hit(self):
        spec = "import { it } from 'vitest';\n\nit('reviews', () => {});\n"
        config = "import { defineConfig } from 'vitest/config';\n\nexport default defineConfig({\n  test: {\n    include: [%s],\n" \
                 "    coverage: { include: ['src/**'] },\n  },\n});\n"
        tree = {"tests/integration/review.integration.spec.ts": spec, "tests/unit/a.test.ts": spec,
                "tests/fixtures/sample.test.ts": spec, "src/plain.spec.ts": "export const x = 1;\n"}
        self.assert_scan(
            "no-environment-assumptions",
            {**tree, "vitest.integration.config.ts": config % "'tests/integration/other.integration.spec.ts'",
             "vitest.config.ts": config % "'tests/unit/**/*.test.ts'"},
            {**tree, "vitest.integration.config.ts": config % "'tests/integration/**/*.integration.spec.ts'",
             "vitest.config.ts": config % "'tests/unit/**/*.test.ts'"},
            ["tests/integration/review.integration.spec.ts:1"])

    def test_vitest_config_with_computed_include_or_repeating_extglob_is_not_guessed(self):
        spec = "import { it } from 'vitest';\n"
        for include in ("...globs", "'tests/+(unit|integration).spec.ts'", "'tests/!(unit).spec.ts'"):
            with self.subTest(include):
                shutil.rmtree(self.tree)
                self.tree.mkdir()
                self.write({"vitest.config.ts": "export default { test: { include: [%s] } };\n" % include,
                            "tests/unitunit.spec.ts": spec})
                self.assertEqual(self.lessons()[1]["no-environment-assumptions"]["status"], "PASS")

    def test_vitest_include_with_a_character_class_still_finds_an_unmatched_test(self):
        spec = "import { it } from 'vitest';\n"
        self.write({"vitest.config.ts": "export default { test: { include: ['tests/[ab]*.spec.ts', 'e2e/**/*.spec.ts'] } };\n",
                    "tests/alpha.spec.ts": spec, "e2e/flow/x.spec.ts": spec, "tests/miss.spec.ts": spec})
        row = self.lessons()[1]["no-environment-assumptions"]
        self.assertEqual((row["status"], row["hits"]), ("FAIL", ["tests/miss.spec.ts:1"]))

    def test_failure_handler_reading_the_current_invocation_is_a_hit(self):
        handler = ("import subprocess\n\n\ndef invocation(unit):\n"
                   "    return subprocess.run(['systemctl', 'show', '-p', 'InvocationID', unit]).stdout\n\n\n"
                   "def failures(unit):\n    return subprocess.run(['journalctl', '_SYSTEMD_INVOCATION_ID=' + invocation(unit)])\n")
        fixed = handler.replace("def invocation(unit):\n", "def invocation(unit):\n"
                                "    if os.environ.get('MONITOR_INVOCATION_ID'):\n"
                                "        return os.environ['MONITOR_INVOCATION_ID']\n")
        self.assert_scan(
            "use-the-triggering-invocation-id",
            {"ops/alert.py": handler, "status.py": "print('InvocationID')\n",
             "ops/note.py": "# MONITOR_INVOCATION_ID is available here\n" + handler},
            {"ops/alert.py": "import os\n" + fixed, "status.py": "print('InvocationID')\n"},
            ["ops/alert.py:5", "ops/note.py:6"])

    def test_relative_link_to_a_missing_file_is_a_hit(self):
        self.assert_scan(
            "preserve-relative-links-when-moving-documents",
            {"README.md": "# Project\n\nRead the [guide](docs/guide.md).\n", "docs/setup.md": "[config](../config.md#port)\n\n"
                          "[up]: ../../outside.md\n", "docs/config.md": "# Config\n"},
            {"README.md": "# Project\n\nRead the [setup](docs/setup.md), [site](https://example.com/x.md), [top](#project),\n"
                          "[mail](mailto:?subject=setup) and ![logo](docs/logo%20dark.png).\n\n`[code](missing.md)`\n\n"
                          "```md\n[fenced](missing.md)\n```\n",
             "docs/setup.md": "[config](../docs/config.md#port)\n\n[ref]: config.md\n", "docs/config.md": "# Config\n",
             "docs/logo dark.png": ""},
            ["README.md:3", "docs/setup.md:1", "docs/setup.md:3"])

    def test_committed_credential_is_a_hit(self):
        token = "ghp_" + "A1b2" * 9
        self.assert_scan(
            "secrets-and-private-data",
            {"config.py": 'TOKEN = "' + token + '"\n', "blob.bin": "\0T=" + token + "\n", token + ".txt": "T=" + token + "\n",
             "s3cr3tvalue.txt": "https://bot:" + "s3cr3tvalue" + "@example.com\n", "deploy.md": "key:\n\n-----BEGIN " + "OPENSSH PRIVATE KEY-----\n",
             "remote.txt": "https://bot:" + "s3cr3tvalue" + "@example.com/repo.git\n"},
            {"config.py": 'import os\n\nTOKEN = os.environ["GITHUB_TOKEN"]\nHOST = r"(?:https?://|ssh://(?:git@)?)([\\w.-]+)"\n',
             "remote.txt": "https://user:" + "${TOKEN}@example.com/repo.git\n"},
            ["blob.bin:1", "config.py:1", "deploy.md:3", "[redacted].txt:1", "remote.txt:1", "[redacted].txt:1"])

    def test_quoted_systemd_path_is_a_hit(self):
        self.assert_scan(
            "systemd-path-escaping",
            {"units.py": "import json\n\n\ndef path_changed(path):\n    return 'PathChanged=' + json.dumps(path)\n",
             "install.sh": "python3 - <<'EOF'\nimport json\nquote = lambda value: json.dumps(value)\n"
                           "text = 'ExecStart=' + quote(root)\nconf = '[Path]\\nPathChanged=\\nPathChanged=' + quote(root + '/x')\nEOF\n",
             "watch.path": '[Path]\nPathModified="/srv/site data"\n'},
            {"units.py": "def path_changed(path):\n    return 'PathChanged=' + path.replace(' ', '\\\\x20')\n",
             "install.sh": "python3 - <<'EOF'\nimport json\nquote = lambda value: json.dumps(value)\n"
                           "text = 'ExecStart=' + quote(root)\nconf = '[Path]\\nPathChanged=\\nPathChanged=' + escape(root + '/x')\nEOF\n",
             "test_units.py": "def test_unquoted(text):\n    assert 'PathChanged=\"' not in text\n",
             "watch.path": "[Path]\nPathModified=/srv/site\\x20data\n"},
            ["install.sh:5", "units.py:5", "watch.path:2"])

    def test_nested_unbounded_quantifier_is_a_hit(self):
        self.assert_scan(
            "time-regexes-on-long-adversarial-inputs-before",
            {"paths.py": 'import re\n\nPYTHON_FILE = re.compile(r"(?:\\S+/)*\\S+\\.py\\b")\nWORDS = re.match(r"(a+)+$", "")\n'
                         'BLOCKS = re.compile(r"(?:.+\\n)+X", re.DOTALL)\nLINES = re.search(r"(?:.+\\n)+X", "", flags=re.S | re.M)\n'},
            {"paths.py": 'import re\n\nPYTHON_FILE = re.compile(r"(?<!\\S)\\S*\\.py(?!\\S)")\n'
                         'HOST = re.compile(r"(?:[\\w-]+\\.)*example")\nBLOCK = re.sub(r"^x(\\n.*)*", "", "")\n'
                         'ON = re.search(r"(.*(?:\\n(?:[ #].*|))*)", "")\nPATH = re.compile(r"/[\\w.-]+(?:/[\\w.-]+)*")\n'
                         'PLAIN = re.compile(r"(?:.+\\n)+X")\nCOUNTED = re.sub(r"(?:.+\\n)+X", "", "", 16)\n'},
            ["paths.py:3", "paths.py:4", "paths.py:5", "paths.py:6"])

    def test_repeat_anchored_to_input_start_runs_at_most_once(self):
        self.assert_scan(
            "time-regexes-on-long-adversarial-inputs-before",
            {"card.py": 'import re\n\nLOOSE = re.compile(r"^(?:HIERARCH +)?(?:[ -<>-~]+ ?)+$", re.I)\n'
                        'INNER = re.compile(r"(?:^(?:a+)+$)+")\n'
                        'SCOPED = re.compile(r"(?m:(?:^[a\\n]+)+$)")\n'
                        'BRANCHY = re.compile(r"(?:^(a|aa)+)+$")\n'},
            {"card.py": 'import re\n\nKEYWORD = re.compile(r"^(?:HIERARCH +)?(?:^[ -<>-~]+ ?)+$", re.I)\n'
                        'START = re.compile(r"(?:\\A\\w+ ?)+$")\n'},
            ["card.py:3", "card.py:4", "card.py:5", "card.py:6"])

    def test_fence_opener_that_allows_only_letters_is_a_hit(self):
        self.assert_scan(
            "fence-info-strings",
            {"fences.py": 'import re\n\nOPENER = re.compile(r"^(`{3,}|~{3,})[a-z]*$")\n', "md.js": "const OPEN = /^```[a-z]+$/;\n"},
            {"fences.py": 'import re\n\nOPENER = re.compile(r"^(`{3,}|~{3,})(.*)$")\nINFO = re.compile(r"^```[^`]*$")\n',
             "md.js": "const OPEN = /^```/;\n"},
            ["fences.py:3", "md.js:1"])

    def test_closing_fence_compared_for_equality_is_a_hit(self):
        self.assert_scan(
            "closing-fence-length",
            {"fences.py": "def close(line, fence):\n    if line == fence:\n        return True\n    return line.rstrip() != fence\n\n\nOPEN = '```'\n",
             "fences.ts": "if (line.trim() === opener) open = false; // ~~~\n",
             "parser.py": "from constants import FENCE\n\n\ndef closes(line, fence):\n    return line == fence\n"},
            {"fences.py": "def close(line, fence):\n    if fence is None or fence == None:\n        return False\n"
                          "    return line.startswith(fence[0] * len(fence)) and not line.lstrip(fence[0]).strip()\n",
             "fences.ts": "if (fence[0] === '~') tilde = true; // ```\n",
             "security.py": "def same(security_fence, expected_fence):\n    return security_fence == expected_fence\n"},
            ["fences.py:2", "fences.py:4", "fences.ts:1", "parser.py:5"])

    def test_newline_marker_check_without_crlf_handling_is_a_hit(self):
        self.assert_scan(
            "normalize-crlf-before-markers",
            {"marker.py": 'MARKER = "<!-- delivered -->"\n\n\ndef is_delivered(text):\n    return text.startswith(MARKER + "\\n")\n',
             "title.js": "const titled = (t) => t.startsWith('# Title\\n');\n",
             "cr.py": 'CR = "\\r"\n\n\ndef marked(text):\n    return text.startswith("<!-- m -->\\n")\n'},
            {"marker.py": 'MARKER = "<!-- delivered -->"\n\n\ndef is_delivered(text):\n'
                          '    return text.replace("\\r\\n", "\\n").startswith(MARKER + "\\n")\n',
             "title.js": "const titled = (t) => t.startsWith('# ');\n",
             "block.py": 'def joined(before):\n    return before if before.endswith("\\n") else before + "\\n"\n'},
            ["cr.py:5", "marker.py:5", "title.js:1"])

    def test_run_window_on_creation_time_is_a_hit(self):
        self.assert_scan(
            "filter-runs-by-failure-time",
            {"runs.py": 'def failures_since(runs, since):\n    return [run for run in runs if run["conclusion"] == "failure" '
                        'and run["created_at"] >= since]\n',
             "runs.ts": "const recent = runs.filter((r) => r.conclusion === 'failure' && since <= r.createdAt);\n"},
            {"runs.py": 'def failures_since(runs, since):\n    return [run for run in runs if run["conclusion"] == "failure" '
                        'and run["updated_at"] >= since]\n\n\ndef newest(rows):\n    return sorted(rows, key=lambda row: row["created_at"])\n',
             "issues.py": 'def fresh(items, since):\n    return [i for i in items if i["created_at"] >= since]\n'},
            ["runs.py:2", "runs.ts:1"])

    def test_test_result_matched_by_bare_method_name_is_a_hit(self):
        self.assert_scan(
            "fully-qualified-test-ids",
            {"results.py": 'def passed(results, test_id):\n    method = test_id.rsplit(".", 1)[-1]\n'
                           '    return any(name.rsplit(".", 1)[-1] == method and outcome == "ok" for name, outcome in results.items())\n',
             "match.js": "const same = (testId, name) => name.split('.').pop() === testId.split('.').pop();\n",
             "match.py": 'def same(name, method):\n    return name.rsplit(".", 1)[-1] == method\n'},
            {"results.py": 'def passed(results, test_id):\n    return results.get(test_id) == "ok"\n\n\n'
                           'def extension(path):\n    return path.rsplit(".", 1)[-1] == "py"\n',
             "ext.py": 'def same(filename, extension):\n    return filename.rsplit(".", 1)[-1] == extension\n'},
            ["match.js:1", "match.py:2", "results.py:3"])

    def test_lint_exit_code_accepted_without_reading_output_is_a_hit(self):
        self.assert_scan(
            "reject-linter-error-output",
            {"lint.py": "def has_findings(exit_code, output):\n    return exit_code == 1\n\n\ndef ran(proc):\n    return proc.returncode in (0, 1)\n",
             "lint_mixed.py": 'import logging\n\n\ndef load():\n    logging.error("Error: config missing")\n\n\n'
                              "def has_findings(proc):\n    return proc.returncode == 1\n"},
            {"lint.py": 'import re\n\nDIAGNOSTIC = re.compile(r"^(usage:|Error:)", re.M)\n\n\n'
                        "def has_findings(exit_code, output):\n    return exit_code == 1 and not DIAGNOSTIC.search(output)\n",
             "grep.py": "def matched(proc):\n    return proc.returncode == 1\n",
             "lint_checked.py": 'def has_findings(proc):\n    if "usage:" in proc.stdout or "Error:" in proc.stderr:\n'
                                '        raise RuntimeError(proc.stderr)\n    return proc.returncode == 1\n',
             "lint_module.py": 'import re\n\nDIAGNOSTIC = re.compile(r"usage:|error:", re.I)\n\n\ndef has_findings(proc):\n'
                               "    if DIAGNOSTIC.search(proc.stderr):\n        raise RuntimeError(proc.stderr)\n"
                               "    return proc.returncode == 1\n"},
            ["lint.py:2", "lint.py:6", "lint_mixed.py:9"])

    def test_trimmed_password_is_a_hit(self):
        self.assert_scan(
            "validate-at-the-boundary",
            {"login.py": 'def login(form):\n    password = form["password"].strip()\n    return check(form["user"], password)\n',
             "login.ts": "const pass = req.body.password.trim();\n"},
            {"login.py": 'def login(form):\n    user = form["user"].strip()\n    return check(user, form["password"])\n',
             "login.ts": "const pass = req.body.password;\n"},
            ["login.py:2", "login.ts:1"])

    def test_workflow_that_checks_only_the_last_commit_is_a_hit(self):
        steps = "on: push\njobs:\n  scan:\n    runs-on: ubuntu-latest\n    steps:\n"
        self.assert_scan(
            "verify-the-right-thing",
            {".github/workflows/check.yml": steps + "      - run: git diff --name-only HEAD~1 HEAD | ./scan\n"
                                                    "      - run: git log -1 --format=%H HEAD^\n"},
            {".github/workflows/check.yml": steps + '      - run: git diff --name-only "${{ github.event.before }}" "${{ github.sha }}"\n',
             "scripts/last.sh": "git diff HEAD~1\n"},
            [".github/workflows/check.yml:6", ".github/workflows/check.yml:7"])

    def test_push_found_by_substring_is_a_hit(self):
        self.assert_scan(
            "parse-git-subcommand-first",
            {"guard.py": 'import shlex\n\n\ndef denied(command):\n    return "git push" in command or "push" in shlex.split(command)\n',
             "guard.js": "const denied = (cmd) => cmd.includes('push'); // git\n"},
            {"guard.py": "import shlex\n\n\ndef denied(command):\n    words = shlex.split(command)\n"
                         '    return words[:1] == ["git"] and next((w for w in words[1:] if not w.startswith("-")), None) == "push"\n',
             "events.py": 'def wanted(event):\n    return "push" in event\n'},
            ["guard.js:1", "guard.py:5"])

    def test_diff_of_unstaged_changes_only_is_a_hit(self):
        self.assert_scan(
            "runnable-commands",
            {"changed.py": "import subprocess\n\n\ndef changed():\n"
                           '    return subprocess.run(["git", "diff", "--name-only"], capture_output=True, text=True).stdout.split()\n',
             "README.md": "Check changed files:\n\n```sh\ngit diff --name-only\ngit diff --name-only -- src/\n```\n"},
            {"changed.py": "import subprocess\n\n\ndef changed():\n"
                           '    return subprocess.run(["git", "diff", "--name-only", "HEAD"], capture_output=True, text=True).stdout.split()\n',
             "README.md": '```sh\ngit diff --name-only HEAD\ngit diff --name-only --cached\ngit diff --name-only "$BASE" HEAD\n```\n'},
            ["README.md:4", "README.md:5", "changed.py:5"])

    def test_unparsable_python_is_incomplete_not_a_pass(self):
        self.write({"broken.py": "def (:\n", "tests/test_broken.py": "class (:\n"})
        code, rows = self.lessons()
        self.assertEqual(code, 2)
        self.assertEqual({lesson: rows[lesson]["status"] for lesson in SCANS},
                         {lesson: "INCOMPLETE" if lesson in PYTHON_SCANS else "PASS" for lesson in SCANS})
        self.assertIn("cannot parse broken.py, tests/test_broken.py", rows["jsonl-unicode-separators"]["reason"])
        self.assertIn("  ValueError('cannot parse tests/test_broken.py')", self.check().stdout.splitlines())

    def test_pytest_marker_does_not_hide_an_unparsable_test(self):
        self.write({"conftest.py": "", "tests/test_broken.py": "class (:\n"})
        self.assertEqual(self.lessons()[1]["tests-must-test"]["status"], "INCOMPLETE")

    def test_symlinked_config_is_not_a_pytest_marker(self):
        outside = self.tree.parent / "pyproject.toml"
        outside.write_text("[tool.pytest.ini_options]\n")
        (self.tree / "pyproject.toml").symlink_to(outside)
        self.write({"tests/test_flags.py": "import unittest\n\n\nclass T(unittest.TestCase):\n    pass\n\n\n"
                                           "def test_never_runs():\n    pass\n"})
        row = self.lessons()[1]["tests-must-test"]
        self.assertEqual((row["status"], row["hits"]), ("FAIL", ["tests/test_flags.py:8"]))

    def test_symlinked_files_are_not_followed(self):
        outside = self.tree.parent / "outside.py"
        outside.write_text("import json\nimport unittest\n\n\nclass T(unittest.TestCase):\n    pass\n\n\n"
                           "def test_x():\n    return [json.loads(x) for x in ''.splitlines()]\n")
        for name in ("test_link.py", "link.py"):
            (self.tree / name).symlink_to(outside)
        code, rows = self.lessons()
        self.assertEqual((code, {rows[lesson]["status"] for lesson in SCANS}), (0, {"PASS"}))

    def test_lesson_fixtures_fail_on_bad_and_pass_on_good(self):
        fixtures = ROOT / "library" / "tests-must-test" / "fixtures"
        for side, expected in (("bad", ("FAIL", ["tests/test_flags.py:11"])), ("good", ("PASS", []))):
            with self.subTest(side):
                shutil.rmtree(self.tree)
                shutil.copytree(fixtures / side, self.tree)
                subprocess.run(["git", "init", "-q"], cwd=self.tree, check=True)
                row = self.lessons()[1]["tests-must-test"]
                self.assertEqual((row["status"], row["hits"]), expected)

    def test_scans_never_run_the_tree_under_test(self):
        sentinel = Path(self.tree.parent) / "ran"
        touch = f"open({str(sentinel)!r}, 'w').close()\n"
        self.write({name: touch for name in ("json.py", "ast.py", "re.py", "os.py", "subprocess.py", "sitecustomize.py",
                                             "conftest.py", "tests/__init__.py", "tests/test_x.py")})
        self.write({"README.md": f"```sh\ntouch {sentinel}\n```\n", "Makefile": f"all:\n\ttouch {sentinel}\n"})
        monitor = self.tree.parent / "fsmonitor"
        monitor.write_text(f"#!/bin/sh\ntouch {sentinel}\n")
        monitor.chmod(0o755)
        subprocess.run(["git", "init", "-q"], cwd=self.tree, check=True)
        subprocess.run(["git", "config", "core.fsmonitor", str(monitor)], cwd=self.tree, check=True)
        code, rows = self.lessons()
        self.assertEqual(code, 0)
        self.assertEqual({rows[lesson]["status"] for lesson in SCANS}, {"PASS"})
        self.assertFalse(sentinel.exists())

    def test_scan_ignores_python_path_into_the_tree(self):
        sentinel = Path(self.tree.parent) / "ran"
        self.write({"sitecustomize.py": f"open({str(sentinel)!r}, 'w').close()\n"})
        scan = Path(self.tree.parent) / "scan.py"
        scan.write_text("import json\nprint(json.dumps({'status': 'PASS', 'hits': []}))\n")
        with mock.patch.dict(os.environ, PYTHONPATH=os.pathsep.join([".", "", str(self.tree)])):
            self.assertEqual(rerun.run_check(str(scan), str(self.tree)), {"status": "PASS", "hits": []})
        self.assertFalse(sentinel.exists())

    def test_lessons_without_a_scan_are_listed_as_not_checked(self):
        code, rows = self.lessons()
        library = sorted(path.name for path in (ROOT / "library").iterdir() if (path / "rule.md").is_file())
        self.assertEqual((code, sorted(rows)), (0, library))
        for lesson, row in rows.items():
            with self.subTest(lesson):
                self.assertEqual(row["status"], "PASS" if lesson in SCANS else "NO_PORTABLE_CHECK")

    def test_plain_output_lists_each_hit_under_its_rule(self):
        self.write({"test_demo.py": "import unittest\n\n\nclass DemoTests(unittest.TestCase):\n    pass\n\n\n"
                                    "def test_never_collected():\n    pass\n"})
        result = self.check()
        self.assertEqual(result.returncode, 1)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[:2], ["FAIL: tests-must-test (A test must exercise the failure it names)", "  test_demo.py:8"])
        self.assertTrue(lines[2].startswith("  fix: ") and len(lines[2]) > len("  fix: "), lines[2])
        self.assertIn("NO_PORTABLE_CHECK: races (Guard read-then-write on shared state)", lines)
        self.assertRegex(lines[-1], rf"^1 with hits, 0 incomplete, {len(SCANS) - 1} passed, \d+ with no portable check$")

    def test_check_scans_the_named_path_and_prints_the_scan_fix(self):
        self.write({"other/tests/check_flags.py": "import unittest\n\n\nclass T(unittest.TestCase):\n"
                                                  "    def test_a(self):\n        pass\n"})
        result = self.check("other")
        self.assertEqual(result.returncode, 1)
        lines = result.stdout.splitlines()
        index = lines.index("FAIL: no-environment-assumptions (Don't assume the environment)")
        self.assertEqual(lines[index + 1], "  tests/check_flags.py:4")
        self.assertTrue(lines[index + 2].startswith("  fix: Register each listed test file"), lines[index + 2])
        missing = self.check("nowhere")
        self.assertNotEqual(missing.returncode, 0)

    def test_shared_fallback_for_a_missing_session_id_is_a_hit(self):
        self.assert_scan(
            "hook-suppression-by-session",
            {"hook.py": source("import os", "", "", "def state_key(event):", '    return event.get("session_id") or "default"',
                               "", "", "def env_key():", '    return os.environ.get("CLAUDE_SESSION_ID", "shared")',
                               "", "", "def marker(session_id):", '    key: str = session_id or "anonymous"', '    return open(key, "w")')},
            {"hook.py": source("import hashlib", "import os", "", "", "def state_key(event):",
                               '    session_id = event.get("session_id") or ""',
                               "    return hashlib.sha256(session_id.encode()).hexdigest() if session_id.strip() else None",
                               "", "", "def env_key():", '    return os.environ.get("CLAUDE_SESSION_ID", "")'),
             "banner.py": source("def banner(event):", '    title = event.get("session_title") or "Untitled"',
                                 "    return f\"session {event.get('session_id') or 'unknown'}: {title}\"", "", "",
                                 "def show(session_id):", '    label = session_id or "anonymous"', '    print("Session:", label)')},
            ["hook.py:5", "hook.py:9", "hook.py:13"])

    def test_session_id_rewritten_into_a_lossy_key_is_a_hit(self):
        self.assert_scan(
            "once-only-recall-per-session",
            {"recall.py": source("import re", "from pathlib import Path", "", "", "def marker(root, session_id):",
                                 '    return Path(root) / re.sub(r"[^A-Za-z0-9]", "_", session_id)', "", "",
                                 "def legacy(root, session_id):", '    return Path(root) / session_id.replace("/", "_")', "", "",
                                 "def annotated(root, session_id):", '    key: str = session_id.replace("/", "_")',
                                 "    return Path(root) / key")},
            {"recall.py": source("import hashlib", "from pathlib import Path", "", "", "def marker(root, session_id):",
                                 "    return Path(root) / hashlib.sha256(session_id.encode()).hexdigest()"),
             "names.py": source("import re", "", "", "def valid(session_id):",
                                '    return re.fullmatch(r"[A-Za-z0-9-]+", session_id) is not None', "", "",
                                "def slug(title):", '    return re.sub(r"\\W+", "-", title)', "", "",
                                "def show(session_id):", '    display_id = session_id.replace("-", " ")', "    print(display_id)")},
            ["recall.py:6", "recall.py:10", "recall.py:14"])

    def test_merge_cutoff_on_comment_creation_time_is_a_hit(self):
        self.assert_scan(
            "inline-review-edits-after-merge",
            {"evidence.py": source("def before_merge(comments, merged_at):",
                                   '    return [c for c in comments if c["created_at"] <= merged_at]', "", "",
                                   "def accepted(review, pull):", "    return review.created_at < pull.merged_at", "", "",
                                   "def logged(comment, pull):", '    print(pull["updated_at"])',
                                   '    return comment["created_at"] <= pull["merged_at"]')},
            {"evidence.py": source("def before_merge(comments, merged_at):",
                                   '    return [c for c in comments if max(c["created_at"], c["updated_at"]) <= merged_at]'),
             "timing.py": source("def lead_time(pull):", '    return pull["merged_at"] - pull["created_at"]', "", "",
                                 "def recent(comments, since):", '    return [c for c in comments if c["created_at"] >= since]',
                                 "", "", "def kept(comment, merged_at):", '    if comment["updated_at"] > merged_at:',
                                 "        return False", '    return comment["created_at"] <= merged_at')},
            ["evidence.py:2", "evidence.py:6", "evidence.py:11"])

    def test_bare_tool_name_under_a_restricted_path_is_a_hit(self):
        self.assert_scan(
            "resolve-tools-before-restricting-path",
            {"replay.py": source("import subprocess", "", "", "def typecheck(folder):",
                                 '    env = {"PATH": "/usr/bin:/bin", "HOME": folder}',
                                 '    return subprocess.run(["tsc", "--noEmit"], cwd=folder, env=env)'),
             "update.py": source("import os", "import subprocess", "", "", "def status():", "    env = os.environ.copy()",
                                 '    env.update(PATH="/custom/bin")', '    return subprocess.run(["git", "status"], env=env)')},
            {"replay.py": source("import shutil", "import subprocess", "", "", "def typecheck(folder):",
                                 '    tsc = shutil.which("tsc")', '    env = {"PATH": "/usr/bin:/bin", "HOME": folder}',
                                 '    return subprocess.run([tsc, "--noEmit"], cwd=folder, env=env)'),
             "venv.py": source("import os", "import subprocess", "", "", "def tests(venv):", "    env = dict(os.environ)",
                               '    env["PATH"] = venv + "/bin" + os.pathsep + os.environ["PATH"]',
                               '    return subprocess.run(["pytest", "-q"], env=env)'),
             "sandbox.py": source("import os", "import subprocess", "", "", "def status():", '    sandbox_env = {"PATH": "/sandbox/bin"}',
                                  '    subprocess.run(["git", "status"], env=os.environ.copy())', "    return sandbox_env"),
             "update.py": source("import os", "import subprocess", "", "", "def status():", "    env = os.environ.copy()",
                                 '    env.update(PATH="/custom/bin" + os.pathsep + os.environ["PATH"])', '    return subprocess.run(["git", "status"], env=env)'),
             "order.py": source("import os", "import subprocess", "", "", "def status():", "    env = os.environ.copy()",
                                '    subprocess.run(["git", "status"], env=env)', '    env.update(PATH="/custom/bin")', "    return env")},
            ["replay.py:6", "update.py:8"])

    def test_log_copied_after_a_timed_wait_is_a_hit(self):
        self.assert_scan(
            "stream-guest-output-to-log",
            {"guest.py": source("import subprocess", "", "", "def replay(command, log):",
                                "    result = subprocess.run(command, capture_output=True, text=True, timeout=600)",
                                "    log.write_text(result.stdout + result.stderr)", "    return result.returncode", "", "",
                                "def attach(process, log):", "    out, err = process.communicate(timeout=30)",
                                '    with open(log, "w") as stream:', "        stream.write(out)")},
            {"guest.py": source("import subprocess", "", "", "def replay(command, log):", '    with open(log, "w") as stream:',
                                "        return subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, timeout=600).returncode"),
             "report.py": source("import subprocess", "", "", "def version(log):",
                                 '    result = subprocess.run(["git", "--version"], capture_output=True, text=True)',
                                 "    log.write_text(result.stdout)", "", "", "def replay(command, log):", "    try:",
                                 "        result = subprocess.run(command, capture_output=True, text=True, timeout=600)",
                                 "    except subprocess.TimeoutExpired as error:", '        log.write_text(error.stdout or "")',
                                 "        raise", "    log.write_text(result.stdout)")},
            ["guest.py:6", "guest.py:13"])

    def test_cloud_init_recoverable_status_treated_as_failure_is_a_hit(self):
        self.assert_scan(
            "terminal-cloud-init-status",
            {"guest.py": source("import subprocess", "", "", "def ready():",
                                '    subprocess.run(["cloud-init", "status", "--wait"], check=True)', "", "",
                                "def booted(vm):", '    result = vm.execute(["cloud-init", "status", "--wait"])',
                                "    if result.returncode != 0:", '        raise RuntimeError("cloud-init failed")'),
             "wait.sh": source("#!/bin/sh", "cloud-init status --wait || exit 1")},
            {"guest.py": source("def booted(vm):", '    result = vm.execute(["cloud-init", "status", "--wait"])',
                                "    if result.returncode not in (0, 2):", '        raise RuntimeError("cloud-init failed")'),
             "wait.sh": source("#!/bin/sh", "cloud-init status --wait || [ $? -eq 2 ] || exit 1"),
             "clean.py": source("import subprocess", "", "", "def reset():",
                                '    subprocess.run(["cloud-init", "clean", "--logs"], check=True)'),
             "status.sh": source("#!/bin/sh", "cloud-init status --long || true"),
             "provision.py": source("import subprocess", "", "", "def provision():",
                                    '    subprocess.run(["cloud-init", "status", "--wait"])',
                                    '    result = subprocess.run(["git", "status"])', "    if result.returncode != 0:",
                                    '        raise RuntimeError("git failed")'),
             "NOTES.md": source("Do not write `cloud-init status --wait || exit 1`: status 2 is a warning.")},
            ["guest.py:5", "guest.py:10", "wait.sh:2"])

    def test_guard_worktree_from_a_base_that_moved_after_a_merge_is_a_hit(self):
        self.assert_scan(
            "fetch-the-current-base-before-creating-the-guard",
            {"tick.py": source("import subprocess", "", "", "def tick(prs, base):",
                               '    subprocess.run(["git", "fetch", "origin", base], check=True)', "    for pr in prs:",
                               '        subprocess.run(["git", "worktree", "add", f".guard/{pr}", f"origin/{base}"], check=True)',
                               '        subprocess.run(["gh", "pr", "merge", str(pr), "--squash"], check=True)')},
            {"tick.py": source("import subprocess", "", "", "def tick(prs, base):", "    for pr in prs:",
                               '        subprocess.run(["git", "fetch", "origin", base], check=True)',
                               '        subprocess.run(["git", "worktree", "add", f".guard/{pr}", f"origin/{base}"], check=True)',
                               '        subprocess.run(["gh", "pr", "merge", str(pr), "--squash"], check=True)'),
             "checkouts.py": source("import subprocess", "", "", "def checkouts(branches):",
                                    '    subprocess.run(["git", "fetch", "origin"], check=True)', "    for branch in branches:",
                                    '        subprocess.run(["git", "worktree", "add", f".worktrees/{branch}", f"origin/{branch}"],'
                                    " check=True)")},
            ["tick.py:7"])

    def test_pre_push_check_that_reads_the_working_tree_is_a_hit(self):
        listed = '    names = subprocess.run(["git", "diff", "--name-only", base, head], capture_output=True, text=True).stdout.split("\\n")'
        self.assert_scan(
            "pre-push-uses-pushed-tree",
            {"hooks/pre_push.py": source("import os", "import subprocess", "", "", "def changed(base, head):", listed,
                                         "    return [name for name in names if name and os.path.exists(name)]"),
             "hooks/pre-push": source("#!/bin/sh", 'git diff --name-only "$1" "$2" | while read -r file; do',
                                      '  [ -f "$file" ] && python3 -m py_compile "$file"', "done"),
             "hooks/pre-push-changed": source("#!/bin/sh", 'files=$(git diff --name-only "$1" "$2")', "for path in $files; do",
                                              '  [ -f "$path" ] && python3 -m py_compile "$path"', "done")},
            {"hooks/pre_push.py": source("import subprocess", "", "", "def changed(base, head):", listed,
                                         '    return [name for name in names if name and subprocess.run(["git", "cat-file", "-e",'
                                         ' f"{head}:{name}"]).returncode == 0]'),
             "hooks/pre-push": source("#!/bin/sh", 'git diff --name-only "$1" "$2" | while read -r file; do',
                                      '  git cat-file -e "$2:$file" || continue', "done"),
             "hooks/pre-push-changed": source("#!/bin/sh", 'files=$(git diff --name-only "$1" "$2")', "for path in $files; do",
                                              '  git cat-file -e "$2:$path" || continue', "done", "configs=$(ls config)",
                                              "for path in $configs; do", '  [ -f "$path" ] && echo "$path"', "done",
                                              "files=$(ls config)", "for path in $files; do", '  [ -f "$path" ] && echo "$path"',
                                              "done"),
             "hooks/pre_push_cache.py": source("from pathlib import Path", "", "", "def caches(cache_dirs):",
                                              "    for cache_dir in cache_dirs:", "        if Path(cache_dir).exists():",
                                              '            print("Cache available:", cache_dir)'),
             "hooks/pre_push_config.py": source("import os", "", "", "def configured():", '    return os.path.exists(".coldfix.toml")'),
             "tools/build.py": source("import os", "", "", "def sources(names):",
                                      "    return [name for name in names if os.path.exists(name)]")},
            ["hooks/pre-push:3", "hooks/pre-push-changed:4", "hooks/pre_push.py:7"])

    def test_call_that_still_passes_a_removed_keyword_is_a_hit(self):
        self.assert_scan(
            "update-callers-and-stubs",
            {"client.py": source("def send(message, retries):", "    return message, retries", "", "",
                                 "def notify(message):", "    return send(message, timeout=5)")},
            {"client.py": source("def send(message, retries):", "    return message, retries", "", "",
                                 "def notify(message):", "    return send(message, retries=3)"),
             "api.py": source("def send(message, **options):", "    return message, options", "", "",
                              "def notify(message, client):", "    client.send(message, timeout=5)",
                              "    return send(message, timeout=5)")},
            ["client.py:6"])

    def test_cli_json_without_a_depth_guard_is_a_hit(self):
        header = ("import argparse", "import json", "import sys", "", "", "def main():", "    parser = argparse.ArgumentParser()",
                  '    parser.add_argument("--facts")', "    args = parser.parse_args()", "    try:")
        self.assert_scan(
            "reject-malformed-facts",
            {"cli.py": source(*header, "        facts = json.loads(args.facts)", "    except json.JSONDecodeError:",
                              '        sys.exit("facts must be JSON")', "    return json.load(sys.stdin), facts"),
             "parse.py": source("import json", "", "", "def parse(args):", "    return json.loads(args.facts)", "", "",
                                "def safe(args):", "    try:", "        return parse(args)", "    except (ValueError, RecursionError):",
                                "        return None", "", "", "def unsafe(args):", "    return parse(args)")},
            {"cli.py": source(*header, "        return json.loads(args.facts), json.load(sys.stdin)",
                              "    except (ValueError, RecursionError):", '        sys.exit("facts must be JSON")'),
             "store.py": source("import json", "import sys", "", "", "def load(path):", "    with open(path) as stream:",
                                "        return json.load(stream)", "", "", "def parse(args):", "    return json.loads(args.facts)",
                                "", "", "def main(args):", "    try:", "        return parse(args)",
                                "    except (ValueError, RecursionError):", '        sys.exit("bad facts")'),
             "report.py": source("import json", "from pathlib import Path", "", "", "def report(args):",
                                 "    return json.loads(Path(args.input).read_text())")},
            ["cli.py:11", "cli.py:14", "parse.py:5"])


def source(*rows):
    return "\n".join(rows) + "\n"


class IncompleteTests(unittest.TestCase):
    def run_library(self, scan):
        with tempfile.TemporaryDirectory() as temporary:
            library = Path(temporary)
            shutil.copy(ROOT / "library" / "rerun.py", library / "rerun.py")
            (library / "probe").mkdir()
            (library / "probe" / "rule.md").write_text("---\nid: probe\ntitle: Probe\nproof: judged\n---\n")
            (library / "probe" / "scan.py").write_text(scan)
            with mock.patch.object(coldfix, "LIBRARY", library), contextlib.redirect_stdout(io.StringIO()) as out:
                code = coldfix.main(["check"])
        self.reason = out.getvalue().splitlines()[1]
        return code, out.getvalue().splitlines()[0]

    def test_incomplete_scan_is_reported_and_sets_the_exit_code(self):
        self.assertEqual(self.run_library("import sys\nprint('{\"status\": \"INCOMPLETE\", \"reason\": \"no git\"}')\nsys.exit(2)\n"),
                         (2, "INCOMPLETE: probe (Probe)"))
        self.assertEqual(self.reason, "  no git")

    def test_crashing_scan_is_incomplete_not_a_pass(self):
        self.assertEqual(self.run_library("raise RuntimeError('broken')\n"), (2, "INCOMPLETE: probe (Probe)"))

    def test_fail_without_hits_is_incomplete(self):
        self.assertEqual(self.run_library("import sys\nprint('{\"status\": \"FAIL\", \"hits\": []}')\nsys.exit(1)\n"),
                         (2, "INCOMPLETE: probe (Probe)"))

    def test_unknown_exit_without_status_is_incomplete(self):
        self.assertEqual(self.run_library("import sys\nprint('{\"hits\": []}')\nsys.exit(3)\n"),
                         (2, "INCOMPLETE: probe (Probe)"))

    def test_pass_without_hits_list_is_incomplete(self):
        self.assertEqual(self.run_library("print('{\"status\": \"PASS\"}')\n"), (2, "INCOMPLETE: probe (Probe)"))


if __name__ == "__main__":
    unittest.main()
