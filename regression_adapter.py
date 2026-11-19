"""Build a self-contained check that runs one repair's unittest regression test."""
import ast
import hashlib
import io
import json
import re
import tokenize
import zlib

LEGACY_TEMPLATE = '''import hashlib
import json
import os
import re
import shutil
import sys
from subprocess import DEVNULL, run
import tempfile
import zlib

def apply(root, patch_file, *options):
    return run(["git", "apply", "--whitespace=nowarn", *options, patch_file], cwd=root,
               stdout=DEVNULL, stderr=DEVNULL).returncode


def main():
    data = json.loads(zlib.decompress(bytes.fromhex("".join(PAYLOAD))))
    patch = data["patch"].encode("utf-8")
    if hashlib.sha256(patch).hexdigest() != TEST_SHA256:
        return 2
    names = data["names"]
    with tempfile.TemporaryDirectory() as temporary:
        root = os.path.join(temporary, "tree")
        shutil.copytree(".", root, symlinks=True, ignore=shutil.ignore_patterns(".git"))
        patch_file = os.path.join(temporary, "tests.patch")
        with open(patch_file, "wb") as stream:
            stream.write(patch)
        if apply(root, patch_file, "-R", "--check") != 0 and apply(root, patch_file) != 0:
            return 2
        directory, name = os.path.split(data["path"])
        command = [sys.executable, "-m", "unittest", "discover", "-s", directory or ".", "-p", name, "-v"]
        for test in names:
            command += ["-k", test]
        result = run(command, cwd=root, capture_output=True, text=True, errors="replace",
                     env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    output = result.stdout + result.stderr
    ran = re.findall(r"^Ran ([0-9]+) tests? in ", output, re.M)
    if not ran or int(ran[0]) < max(1, len(names)):
        return 2
    target = re.fullmatch(r"(?:FAIL|ERROR): (test\\w*)", data["diagnostic"])
    statuses = re.findall(r"^(test\\w*) \\([\\w.]+\\)(?:\\n.*)? \\.\\.\\. (ok|skipped|FAIL|ERROR)\\b", result.stderr, re.M)
    diagnosed = {status for test, status in statuses if target and test == target[1]}
    skipped = [test for test, status in statuses if status == "skipped"]
    summary = re.findall(r"^Ran [0-9]+ tests? in [0-9.]+s\\n\\nOK(?: \\(skipped=([0-9]+)\\))?$", result.stderr, re.M)
    if result.returncode == 0 and summary and (
            summary[-1] == "" or (diagnosed == {"ok"} and int(summary[-1]) == len(skipped))):
        print('{"status": "PASS"}')
        return 0
    if (result.returncode == 1 and re.search("^" + re.escape(data["diagnostic"]) + " \\\\(", output, re.M)
            and re.findall(r"^FAILED \\(", output, re.M)):
        print('{"status": "FAIL", "failure": "' + EXPECTED_FAILURE + '"}')
        return 1
    return 2


sys.exit(main())
'''

PREVIOUS_TEMPLATE = '''import hashlib
import json
import os
import re
import shutil
import sys
from subprocess import DEVNULL, run
import tempfile
import zlib

def apply(root, patch_file, *options):
    return run(["git", "apply", "--whitespace=nowarn", *options, patch_file], cwd=root,
               stdout=DEVNULL, stderr=DEVNULL).returncode


def main():
    data = json.loads(zlib.decompress(bytes.fromhex("".join(PAYLOAD))))
    patch = data["patch"].encode("utf-8")
    if hashlib.sha256(patch).hexdigest() != TEST_SHA256:
        return 2
    names = data["names"]
    with tempfile.TemporaryDirectory() as temporary:
        root = os.path.join(temporary, "tree")
        shutil.copytree(".", root, symlinks=True, ignore=shutil.ignore_patterns(".git"))
        patch_file = os.path.join(temporary, "tests.patch")
        with open(patch_file, "wb") as stream:
            stream.write(patch)
        if apply(root, patch_file, "-R", "--check") != 0 and apply(root, patch_file) != 0:
            return 2
        directory, name = os.path.split(data["path"])
        command = [sys.executable, "-m", "unittest", "discover", "-s", directory or ".", "-p", name]
        for test in names:
            command += ["-k", test]
        result = run(command, cwd=root, capture_output=True, text=True, errors="replace",
                     env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    output = result.stdout + result.stderr
    ran = re.findall(r"^Ran ([0-9]+) tests? in ", output, re.M)
    if not ran or int(ran[0]) < max(1, len(names)):
        return 2
    if result.returncode == 0 and re.findall(r"^OK$", output, re.M):
        print('{"status": "PASS"}')
        return 0
    if result.returncode == 1 and data["diagnostic"] in output and re.findall(r"^FAILED \\(", output, re.M):
        print('{"status": "FAIL", "failure": "' + EXPECTED_FAILURE + '"}')
        return 1
    return 2


sys.exit(main())
'''

REPORTING = r"""
def incomplete(why):
    sys.stderr.write("INCOMPLETE: " + why.strip() + "\n")
    return 2


def cause(output):
    lines = [line.strip() for line in output.split("\n") if line.strip()]
    errors = [line for line in lines if re.match(r"[A-Za-z_][\w.]*(Error|Exception|Exit|Interrupt)\b", line)]
    return (errors or lines or ["no test output"])[-1]


def main():"""
TEMPLATE = LEGACY_TEMPLATE
for _old, _new in (("\ndef main():", REPORTING),
                   ("!= TEST_SHA256:\n        return 2", '!= TEST_SHA256:\n        return incomplete("test patch digest changed")'),
                   ("apply(root, patch_file) != 0:\n            return 2",
                    'apply(root, patch_file) != 0:\n            return incomplete("test patch does not apply")'),
                   ("    output = result.stdout + result.stderr\n",
                    '    output = (result.stdout + result.stderr).replace(temporary, "<tmp>")\n'),
                   ("max(1, len(names)):\n        return 2", "max(1, len(names)):\n        return incomplete(cause(output))"),
                   ("        return 1\n    return 2\n", "        return 1\n    return incomplete(cause(output))\n")):
    TEMPLATE = TEMPLATE.replace(_old, _new, 1)

LEGACY_EXISTING_TEMPLATE = '''import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
from subprocess import run
import tempfile
import zlib

data = json.loads(zlib.decompress(bytes.fromhex("".join(PAYLOAD))))
with tempfile.TemporaryDirectory() as temporary:
    root = os.path.join(temporary, "tree")
    shutil.copytree(".", root, symlinks=True, ignore=shutil.ignore_patterns(".git"))
    path = Path(root) / data["path"]
    if (os.path.islink(path) or any(os.path.islink(parent) for parent in path.parents if parent != Path(root))
            or not os.path.isfile(path) or hashlib.sha256(getattr(path, "read_bytes")()).hexdigest() != TEST_SHA256):
        sys.exit(2)
    directory, name = os.path.split(data["path"])
    command = [sys.executable, "-m", "unittest", "discover", "-s", directory or ".", "-p", name,
               "-k", data["names"][0]]
    source = Path(root) / "src"
    pythonpath = str(source) if os.path.isdir(source) and not os.path.islink(source) else ""
    result = run(command, cwd=root, capture_output=True, text=True, errors="replace",
                 env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=pythonpath))
output = result.stdout + result.stderr
ran = re.findall(r"^Ran ([0-9]+) tests? in ", output, re.M)
if not ran or int(ran[0]) != 1:
    sys.exit(2)
if result.returncode == 0 and re.findall(r"^OK$", output, re.M):
    print('{"status": "PASS"}')
    sys.exit(0)
if result.returncode == 1 and data["diagnostic"] in output and re.findall(r"^FAILED \\(", output, re.M):
    print('{"status": "FAIL", "failure": "' + EXPECTED_FAILURE + '"}')
    sys.exit(1)
sys.exit(2)
'''
EXISTING_REPORTING = (
    ("import zlib\n\ndata = ", "import zlib\n" + REPORTING.removesuffix("\ndef main():") + "\ndata = "),
    ("        sys.exit(2)\n    directory", '        sys.exit(incomplete("test source digest changed"))\n    directory'),
    ("int(ran[0]) != 1:\n    sys.exit(2)", 'int(ran[0]) != 1:\n    sys.exit(incomplete(cause(output.replace(temporary, "<tmp>"))))'),
    ("    sys.exit(1)\nsys.exit(2)\n", '    sys.exit(1)\nsys.exit(incomplete(cause(output.replace(temporary, "<tmp>"))))\n'))
EXISTING_TEMPLATE = LEGACY_EXISTING_TEMPLATE
for _old, _new in EXISTING_REPORTING:
    EXISTING_TEMPLATE = EXISTING_TEMPLATE.replace(_old, _new, 1)


def without_existing_reporting(script):
    for old, new in EXISTING_REPORTING:
        script = script.replace(new, old, 1)
    return script


PAIR_TEMPLATE = EXISTING_TEMPLATE.replace('!= TEST_SHA256', 'not in data["source_sha256s"]')
EXACT_EXISTING_TEMPLATE = EXISTING_TEMPLATE.replace('"-k", data["names"][0]', '"-k", "*." + data["names"][0]')
EXACT_PAIR_TEMPLATE = PAIR_TEMPLATE.replace('"-k", data["names"][0]', '"-k", "*." + data["names"][0]')

LEGACY_PYTEST_PAIR_TEMPLATE = '''import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
from subprocess import run
import tempfile
import zlib

data = json.loads(zlib.decompress(bytes.fromhex("".join(PAYLOAD))))
with tempfile.TemporaryDirectory() as temporary:
    root = os.path.join(temporary, "tree")
    shutil.copytree(".", root, symlinks=True, ignore=shutil.ignore_patterns(".git"))
    path = Path(root) / data["path"]
    if (os.path.islink(path) or any(os.path.islink(parent) for parent in path.parents if parent != Path(root))
            or not os.path.isfile(path) or hashlib.sha256(getattr(path, "read_bytes")()).hexdigest() not in data["source_sha256s"]):
        sys.exit(2)
    command = [sys.executable, "-m", "pytest", "-q", "-rf", "-p", "no:cacheprovider", data["diagnostic"][len("FAILED "):]]
    result = run(command, cwd=root, capture_output=True, text=True, errors="replace",
                 env=dict(os.environ, COLUMNS="4096", PYTHONDONTWRITEBYTECODE="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1"))
output = result.stdout + result.stderr
summary = re.findall(r"^(?:=+ )?1 (passed|failed)(?:, [0-9]+ (?:subtests passed|warnings?))* in [0-9.]+s", output, re.M)
if len(summary) != 1:
    sys.exit(2)
if result.returncode == 0 and summary == ["passed"]:
    print('{"status": "PASS"}')
    sys.exit(0)
failed = re.findall(r"^(FAILED \\S+) - (.*)$", output, re.M)
if (result.returncode == 1 and summary == ["failed"] and len(failed) == 1 and failed[0][0] == data["diagnostic"]
        and failed[0][1].startswith(data["message"])):
    print('{"status": "FAIL", "failure": "' + EXPECTED_FAILURE + '"}')
    sys.exit(1)
sys.exit(2)
'''
PYTEST_PAIR_TEMPLATE = (
    LEGACY_PYTEST_PAIR_TEMPLATE
    .replace("import zlib\n\ndata = ", "import zlib\n" + REPORTING.removesuffix("\ndef main():") + "\ndata = ")
    .replace("        sys.exit(2)\n    command", '        sys.exit(incomplete("test source digest changed"))\n    command')
    .replace("sys.exit(2)\n", 'sys.exit(incomplete(cause(output.replace(temporary, "<tmp>"))))\n')
)


CANONICAL_VERIFY = b'#!/usr/bin/env bash\nset -euo pipefail\ncd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."\n\npython3 -B -m unittest discover -s tests\n'
PARALLEL_VERIFY = rb'''#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

packages=(tests/*/__init__.py)
if [[ -e ${packages[0]} ]]; then
    echo "verify.sh runs top-level test modules only; found ${packages[*]}" >&2
    exit 1
fi

logs=$(mktemp -d)
trap 'rm -rf -- "$logs"' EXIT

run_module() {
    python3 -B -m unittest discover -s tests -p "$1" >"$logs/$1.log" 2>&1 || echo "FAILED: $1" >>"$logs/failed"
}

workers=$(nproc)
running=0
for module in tests/test*.py; do
    if ((running >= workers)); then
        wait -n
    else
        running=$((running + 1))
    fi
    run_module "${module##*/}" &
done
wait

for log in "$logs"/*.log; do
    echo "== ${log##*/}"
    cat -- "$log"
done
if [[ -s $logs/failed ]]; then
    cat -- "$logs/failed"
    exit 1
fi
'''

PARALLEL_VERIFY_JUNIT = (PARALLEL_VERIFY
                         .replace(b'run_module() {\n', b'unittest=(-m unittest)\nif [[ -n ${COLD_VERIFY_UNITTEST:-} ]]; then\n'
                                  b'    unittest=("$COLD_VERIFY_UNITTEST")\nfi\n\nrun_module() {\n')
                         .replace(b'python3 -B -m unittest discover', b'python3 -B "${unittest[@]}" discover'))
VERIFY_RECIPES = (CANONICAL_VERIFY, PARALLEL_VERIFY, PARALLEL_VERIFY_JUNIT)

COMMAND_TEMPLATE = '''import json
from hashlib import sha256
from pathlib import Path
import shutil
import re
from subprocess import run
import sys
import tempfile
import zlib

data = json.loads(zlib.decompress(bytes.fromhex("".join(PAYLOAD))))
git_path = Path('.git')
if getattr(git_path, 'is_symlink')() or not (getattr(git_path, 'is_dir')() or getattr(git_path, 'is_file')()):
    sys.exit(2)
with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary) / 'tree'
    if getattr(git_path, 'is_file')():
        shutil.copytree('.', root, symlinks=True, ignore=shutil.ignore_patterns('.git'))
        env = dict(PATH='/usr/bin:/bin', HOME=temporary, GIT_CONFIG_NOSYSTEM='1',
                   GIT_CONFIG_GLOBAL='/dev/null', GIT_TERMINAL_PROMPT='0')
        commands = (["git", "-C", str(root), "init", "-q"],
                    ["git", "-C", str(root), "fetch", "-q", "--no-tags", "--", str(Path.cwd()), "HEAD"],
                    ["git", "-C", str(root), "reset", "-q", "--mixed", "FETCH_HEAD"])
        for command in commands:
            if run(command, env=env, capture_output=True).returncode:
                sys.exit(2)
    else:
        shutil.copytree('.', root, symlinks=True)
    sandbox = ["bwrap", "--die-with-parent", "--unshare-all", "--ro-bind", "/usr", "/usr",
               "--ro-bind", "/bin", "/bin", "--ro-bind", "/lib", "/lib", "--ro-bind", "/lib64", "/lib64",
               "--bind", str(root), "/work", "--tmpfs", "/tmp", "--proc", "/proc", "--dev", "/dev",
               "--clearenv", "--setenv", "PATH", "/usr/bin:/bin", "--setenv", "HOME", "/tmp",
               "--setenv", "PYTHONDONTWRITEBYTECODE", "1", "--chdir", "/work", "--"]
    git = ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null"]
    tree = run([*sandbox, *git, "rev-parse", "--verify", "HEAD:tests"], capture_output=True, text=True)
    status = run([*sandbox, *git, "status", "--porcelain", "--untracked-files=all", "--ignored", "--", "tests"],
                 capture_output=True, text=True)
    files = run([*sandbox, *git, "ls-tree", "-r", "HEAD", "--", "tests"], capture_output=True, text=True)
    if (tree.returncode or tree.stdout.strip() != data["test_tree"] or status.returncode or status.stdout
            or files.returncode or not files.stdout or any(line.startswith("120000 ") for line in files.stdout.splitlines())):
        sys.exit(2)
    if data["entrypoint_sha256"]:
        entry = root / '.fleet/verify.sh'
        if (getattr(entry, 'is_symlink')() or getattr(getattr(entry, 'parent'), 'is_symlink')()
                or not getattr(entry, 'is_file')()
                or sha256(getattr(entry, 'read_bytes')()).hexdigest() != data["entrypoint_sha256"]):
            sys.exit(2)
    result = run([*sandbox, *data["argv"]], capture_output=True, text=True, errors="replace")
output = result.stdout + result.stderr
ran = re.findall(r"^Ran ([0-9]+) tests? in ", output, re.M)
if len(ran) != 1 or int(ran[0]) == 0:
    sys.exit(2)
if result.returncode == 0 and re.findall(r"^OK(?: \\([^\\n]+\\))?$", output, re.M) == ["OK"]:
    print('{"status": "PASS"}')
    sys.exit(0)
if result.returncode == 1 and data["marker"] in output:
    print('{"status": "FAIL", "failure": "' + EXPECTED_FAILURE + '"}')
    sys.exit(1)
sys.exit(2)
'''
RUNS_COMMAND_TEMPLATE = (COMMAND_TEMPLATE
                         .replace('if len(ran) != 1 or int(ran[0]) == 0:',
                                  'if len(ran) != data["runs"] or any(int(count) == 0 for count in ran):')
                         .replace('re.M) == ["OK"]:', 're.M) == ["OK"] * data["runs"]:'))


def build_command(argv, marker, test_tree, diagnostic=None, entrypoint=CANONICAL_VERIFY, runs=1):
    data = dict(mode='recorded_command', argv=argv, marker=marker, test_tree=test_tree,
                path='tests', diagnostic=diagnostic if diagnostic is not None else ' '.join(argv), names=[],
                entrypoint_sha256=hashlib.sha256(entrypoint).hexdigest() if argv == ['./.fleet/verify.sh'] else '')
    if runs != 1:
        data['runs'] = runs
    digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    expected = 'regression:' + digest[:16]
    blob = zlib.compress(json.dumps(data, sort_keys=True).encode(), 9).hex()
    lines = ',\n'.join(f'    {blob[i:i + 64]!r}' for i in range(0, len(blob), 64))
    constants = (f'TEST_SHA256 = "{digest}"\nEXPECTED_FAILURE = {expected!r}\n'
                 f'PAYLOAD = (\n{lines},\n)\n')
    return constants + '\n' + (RUNS_COMMAND_TEMPLATE if runs != 1 else COMMAND_TEMPLATE), expected, digest


def test_digest(test_patch):
    return hashlib.sha256(test_patch.encode("utf-8")).hexdigest()


def build(test_path, test_patch, test_names, diagnostic, template=TEMPLATE):
    digest = test_digest(test_patch)
    expected = "regression:" + digest[:16]
    data = json.dumps(dict(path=test_path, names=list(test_names), diagnostic=diagnostic, patch=test_patch), sort_keys=True)
    blob = zlib.compress(data.encode("utf-8"), 9).hex()
    lines = ",\n".join(f"    {blob[i:i + 64]!r}" for i in range(0, len(blob), 64))
    constants = (f'TEST_SHA256 = "{digest}"\n'
                 f"EXPECTED_FAILURE = {expected!r}\n"
                 f"PAYLOAD = (\n{lines},\n)\n")
    return constants + "\n" + template, expected, digest


def build_existing(test_path, source_sha256, test_name, diagnostic, *, exact=False):
    if not re.fullmatch(r'[0-9a-f]{64}', source_sha256) or not re.fullmatch(r'test\w*', test_name):
        raise ValueError('invalid existing test identity')
    expected = 'regression:' + source_sha256[:16]
    data = json.dumps(dict(mode='existing_exact' if exact else 'existing', path=test_path, source_sha256=source_sha256,
                           names=[test_name], diagnostic=diagnostic), sort_keys=True)
    blob = zlib.compress(data.encode('utf-8'), 9).hex()
    lines = ',\n'.join(f'    {blob[i:i + 64]!r}' for i in range(0, len(blob), 64))
    constants = (f'TEST_SHA256 = "{source_sha256}"\n'
                 f'EXPECTED_FAILURE = {expected!r}\n'
                 f'PAYLOAD = (\n{lines},\n)\n')
    return constants + '\n' + (EXACT_EXISTING_TEMPLATE if exact else EXISTING_TEMPLATE), expected, source_sha256


def build_existing_pair(test_path, source_sha256s, test_name, diagnostic, *, exact=False):
    if (not isinstance(source_sha256s, list) or len(source_sha256s) != 2
            or not all(re.fullmatch(r'[0-9a-f]{64}', value) for value in source_sha256s)
            or not re.fullmatch(r'test\w*', test_name)):
        raise ValueError('invalid existing test identity')
    digest = hashlib.sha256(''.join(source_sha256s).encode('ascii')).hexdigest()
    expected = 'regression:' + digest[:16]
    data = json.dumps(dict(mode='existing_pair_exact' if exact else 'existing_pair', path=test_path, source_sha256s=source_sha256s,
                           names=[test_name], diagnostic=diagnostic), sort_keys=True)
    blob = zlib.compress(data.encode('utf-8'), 9).hex()
    lines = ',\n'.join(f'    {blob[i:i + 64]!r}' for i in range(0, len(blob), 64))
    constants = (f'TEST_SHA256 = "{digest}"\n'
                 f'EXPECTED_FAILURE = {expected!r}\n'
                 f'PAYLOAD = (\n{lines},\n)\n')
    return constants + '\n' + (EXACT_PAIR_TEMPLATE if exact else PAIR_TEMPLATE), expected, digest


def build_pytest_pair(test_path, source_sha256s, diagnostic, message):
    match = PYTEST_ID.fullmatch(diagnostic)
    if (match is None or match[1] != test_path or not isinstance(message, str)
            or not re.match(r'(?:AssertionError|assert)\b.', message) or not isinstance(source_sha256s, list) or len(source_sha256s) != 2
            or not all(re.fullmatch(r'[0-9a-f]{64}', value) for value in source_sha256s)):
        raise ValueError('invalid pytest test identity')
    digest = hashlib.sha256(''.join(source_sha256s).encode('ascii')).hexdigest()
    expected = 'regression:' + digest[:16]
    data = json.dumps(dict(mode='pytest_pair', path=test_path, source_sha256s=source_sha256s, names=[match[3]],
                           diagnostic=diagnostic, message=message), sort_keys=True)
    blob = zlib.compress(data.encode('utf-8'), 9).hex()
    lines = ',\n'.join(f'    {blob[i:i + 64]!r}' for i in range(0, len(blob), 64))
    constants = (f'TEST_SHA256 = "{digest}"\n'
                 f'EXPECTED_FAILURE = {expected!r}\n'
                 f'PAYLOAD = (\n{lines},\n)\n')
    return constants + '\n' + PYTEST_PAIR_TEMPLATE, expected, digest


def payload(script):
    try:
        match = re.search(r"^PAYLOAD = \((.*?)^\)$", script, re.M | re.S)
        blob = "".join(ast.literal_eval("(" + match[1] + ")"))
        return json.loads(zlib.decompress(bytes.fromhex(blob)))
    except (AttributeError, TypeError, SyntaxError, zlib.error) as error:
        raise ValueError("unreadable regression payload") from error


FILE_SUFFIX = re.compile(r"(?<=\w)\.(?:py|sh|md|js|ts|json|toml|ya?ml|txt)\b")
TEST_NAME = re.compile(r"^\+\s+def (test\w*)\(", re.M)


PYTEST_ID = re.compile(r"FAILED ((?:(?!\.\.?/)[\w.-]+/)*(?:test_[\w.-]*|[\w-]+_test)\.py)::((?:[A-Za-z_]\w*::)?(test\w*)(?:\[[\w.-]+\])?)(?![\w\[:])")
UNITTEST_ID = re.compile(r"(?:FAIL|ERROR): (test\w*) \(((?:[A-Za-z_]\w*\.)+\1)\)")


LITERAL_TOKENS = {getattr(tokenize, name) for name in ("STRING", "COMMENT", "FSTRING_MIDDLE", "TSTRING_MIDDLE")
                  if hasattr(tokenize, name)}
DIFF_HEADER = ("diff ", "index ", "@@", "--- ", "+++ ", "new file", "deleted file", "similarity ", "rename ", "old mode", "new mode")


def literal_text(source):
    return "\n".join(token.string for token in tokenize.generate_tokens(io.StringIO(source).readline)
                     if token.type in LITERAL_TOKENS)


def diff_headers(patch):
    return "\n".join(line for line in patch.splitlines() if line.startswith(DIFF_HEADER) or line[:1] not in "+- ")


def screenable(patch):
    body = "\n".join(line for line in patch.splitlines()
                     if not line.startswith(("index ", "new file mode ")))
    return FILE_SUFFIX.sub(lambda match: match[0] if re.search(
        r"(?i)(?:\b[a-z][a-z0-9+.-]*:)?//[^\s\"'<>]*$", body[:match.start()]) else "_ext", body)


COLDPASS_TEMPLATE = '''import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import zlib


def main():
    data = json.loads(zlib.decompress(bytes.fromhex("".join(PAYLOAD))))
    proof = data['proof']
    interpreters = [Path(sys.executable), Path(shutil.which(proof['runtime']['name']) or sys.executable)]
    python = next((str(path) for path in interpreters if path.is_file()
                   and hashlib.sha256(path.read_bytes()).hexdigest() == proof['runtime']['sha256']), None)
    if python is None:
        return 2
    version = subprocess.run([python, '--version'], capture_output=True, text=True)
    if version.returncode or proof['runtime']['version'] not in version.stdout.split():
        return 2
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / 'tree'
        shutil.copytree('.', root, symlinks=True, ignore=shutil.ignore_patterns('.git'))
        cwd = root / proof['cwd']
        if not cwd.is_dir() or cwd.is_symlink() or any(parent.is_symlink() for parent in cwd.parents):
            return 2
        patch = Path(temporary) / 'witness.patch'
        patch.write_text(data['patch'])
        if subprocess.run(['git', 'apply', '--whitespace=nowarn', str(patch)], cwd=cwd,
                          capture_output=True).returncode:
            return 2
        artifacts = Path(temporary) / 'artifacts'
        artifacts.mkdir()
        site = Path(temporary) / 'site'
        site.mkdir()
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=str(site),
                   PIP_NO_INDEX='1', PIP_DISABLE_PIP_VERSION_CHECK='1')
        for item in proof['setup'] + proof['dependencies']:
            blob = bytes.fromhex(data['artifacts'][item['path']])
            if hashlib.sha256(blob).hexdigest() != item['sha256']:
                return 2
            path = artifacts / item['path']
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(blob)
            if path.suffix == '.whl':
                command = [python, '-m', 'pip', 'install', '--no-index', '--no-deps',
                           '--no-compile', '--target', str(site), str(path)]
            elif path.suffix == '.py' and item in proof['setup']:
                command = [python, str(path)]
            else:
                return 2
            if subprocess.run(command, cwd=cwd, env=env, capture_output=True).returncode:
                return 2
        result = subprocess.run([python, *proof['argv'][1:]], cwd=cwd, env=env,
                                capture_output=True, text=True, errors='replace')
    output = result.stdout + result.stderr
    if re.findall(r'^Ran ([0-9]+) tests? in ', output, re.M) != ['1']:
        return 2
    if result.returncode == 0 and re.findall(r'^OK$', output, re.M) == ['OK']:
        print(json.dumps(dict(status='PASS')))
        return 0
    if (result.returncode == 1 and 'FAIL: ' + proof['expected_failure'] in output
            and 'AssertionError' in output and re.findall(r'^FAILED \\(failures=1\\)$', output, re.M)):
        print(json.dumps(dict(status='FAIL', failure=EXPECTED_FAILURE)))
        return 1
    return 2


sys.exit(main())
'''


def build_coldpass(path, patch, name, proof, artifacts):
    data = dict(mode='coldpass', path=path, patch=patch, names=[name],
                diagnostic='FAIL: ' + name, proof=proof,
                artifacts={key: value.hex() for key, value in artifacts.items()})
    content = json.dumps(data, sort_keys=True)
    digest = hashlib.sha256(content.encode()).hexdigest()
    expected = 'regression:' + digest[:16]
    blob = zlib.compress(content.encode(), 9).hex()
    lines = ',\n'.join(f'    {blob[index:index + 64]!r}' for index in range(0, len(blob), 64))
    constants = f'TEST_SHA256 = "{digest}"\nEXPECTED_FAILURE = {expected!r}\nPAYLOAD = (\n{lines},\n)\n'
    return constants + '\n' + COLDPASS_TEMPLATE, expected, digest
