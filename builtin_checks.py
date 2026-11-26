"""Fast, deterministic, zero-LLM built-in repository and diff checks for Coldfix."""

from __future__ import annotations

import json
import os
from pathlib import Path
import posixpath
import re
import shlex
import subprocess

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 fallback
    tomllib = None

import coldfix


CHECKS = {
    "agents-md-drift": {
        "title": "Keep agent instruction file paths and commands in sync with the repository",
        "fix": "Update or remove stale file paths and command references in your agent instruction files.",
        "diff_only": False,
    },
    "agent-config-secrets": {
        "title": "Keep literal credentials out of committed agent configuration files",
        "fix": "Replace literal credentials in committed agent config files with environment variable references and rotate any exposed secrets.",
        "diff_only": False,
    },
    "anti-test-weakening": {
        "title": "Do not weaken tests by adding skips, dropping assertions, or deleting tests",
        "fix": "Fix the underlying failure instead of skipping tests, deleting test functions, or removing assertions without replacement.",
        "diff_only": True,
    },
}
CHECK_IDS = tuple(CHECKS)
TREE_CHECK_IDS = tuple(name for name, meta in CHECKS.items() if not meta["diff_only"])


# ---------------------------------------------------------------------------
# Shared safe filesystem and git helpers (never follow symlinks, never exec)
# ---------------------------------------------------------------------------

def _git(tree: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "core.fsmonitor=false", *args],
        cwd=tree,
        capture_output=True,
        text=True,
        errors="replace",
    )


def _is_safe_regular(tree: Path, rel_path: str) -> bool:
    if not rel_path or rel_path.startswith("/") or "\x00" in rel_path:
        return False
    parts = Path(rel_path).parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        return False
    current = tree
    for part in parts:
        current = current / part
        if current.is_symlink():
            return False
    return current.is_file()


def _safe_exists_in_tree(tree: Path, rel_path: str) -> bool:
    norm = posixpath.normpath(rel_path.replace("\\", "/"))
    if not norm or norm == "." or norm == ".." or norm.startswith("../") or norm.startswith("/"):
        return False
    parts = norm.split("/")
    current = tree
    for part in parts[:-1]:
        current = current / part
        if current.is_symlink() or not current.is_dir():
            return False
    target = current / parts[-1]
    return os.path.lexists(target) and not target.is_symlink()


def _list_repo_files(tree: Path) -> list[str]:
    listed = _git(tree, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    if listed.returncode == 0:
        paths = sorted({p for p in listed.stdout.split("\0") if p})
        return [p for p in paths if _is_safe_regular(tree, p)]
    found: list[str] = []
    for folder, folders, names in os.walk(tree, followlinks=False):
        rel_dir = os.path.relpath(folder, tree)
        kept = []
        for name in sorted(folders):
            if name == ".git":
                continue
            if not (Path(folder) / name).is_symlink():
                kept.append(name)
        folders[:] = kept
        for name in sorted(names):
            rel = name if rel_dir == "." else posixpath.join(rel_dir.replace(os.sep, "/"), name)
            if _is_safe_regular(tree, rel):
                found.append(rel)
    return found


def _git_bytes(tree: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-c", "core.fsmonitor=false", *args],
        cwd=tree,
        capture_output=True,
    )


def _git_prefix(tree: Path) -> str:
    res = _git(tree, "rev-parse", "--show-prefix")
    if res.returncode != 0:
        return ""
    return res.stdout.strip()


def _list_staged_regular_files(tree: Path) -> set[str]:
    res = _git(tree, "ls-files", "-s", "-z")
    if res.returncode != 0:
        return set()
    found: set[str] = set()
    for entry in res.stdout.split("\0"):
        if not entry or "\t" not in entry:
            continue
        meta, rel = entry.split("\t", 1)
        mode = meta.split(" ", 1)[0]
        # Regular files only: skip symlinks (120000) and submodules (160000).
        if mode in ("100644", "100755") and rel and not rel.startswith("/"):
            found.add(rel)
    return found


def _read_staged_text_safe(tree: Path, rel_path: str, *, prefix: str, staged_regular: set[str]) -> str | None:
    norm = posixpath.normpath(rel_path.replace("\\", "/"))
    if norm not in staged_regular:
        return None
    spec = f":{prefix}{norm}"
    res = _git_bytes(tree, "show", spec)
    if res.returncode != 0:
        return None
    raw = res.stdout
    if b"\x00" in raw:
        return None
    return raw.decode("utf-8", "replace")


def _read_text_for_mode(
    tree: Path,
    rel_path: str,
    *,
    staged: bool = False,
    prefix: str = "",
    staged_regular: set[str] | None = None,
) -> str | None:
    if staged:
        return _read_staged_text_safe(tree, rel_path, prefix=prefix, staged_regular=staged_regular or set())
    return _read_text_safe(tree, rel_path)


def _exists_for_mode(
    tree: Path,
    rel_path: str,
    *,
    staged: bool = False,
    staged_regular: set[str] | None = None,
    staged_dirs: set[str] | None = None,
) -> bool:
    norm = posixpath.normpath(rel_path.replace("\\", "/"))
    if not norm or norm == "." or norm == ".." or norm.startswith("../") or norm.startswith("/"):
        return False
    if staged:
        return norm in (staged_regular or set()) or norm in (staged_dirs or set())
    return _safe_exists_in_tree(tree, norm)


def _read_text_safe(tree: Path, rel_path: str) -> str | None:
    if not _is_safe_regular(tree, rel_path):
        return None
    raw = (tree / rel_path).read_bytes()
    if b"\x00" in raw:
        return None
    return raw.decode("utf-8", "replace")


def _sort_hits(hits: list[str]) -> list[str]:
    def key(hit: str) -> tuple[str, int, str]:
        match = re.match(r"^(.*?):(\d+)(?::\s*(.*))?$", hit)
        if not match:
            return (hit, 0, "")
        return (match.group(1), int(match.group(2)), match.group(3) or "")

    return sorted(set(hits), key=key)


def _format_result(check_id: str, hits: list[str], *, reason: str | None = None) -> dict:
    meta = CHECKS[check_id]
    if reason is not None:
        return {
            "lesson": check_id,
            "title": meta["title"],
            "status": "INCOMPLETE",
            "hits": [],
            "reason": reason,
        }
    ordered = _sort_hits(hits)
    result = {
        "lesson": check_id,
        "title": meta["title"],
        "status": "FAIL" if ordered else "PASS",
        "hits": ordered,
    }
    if ordered:
        result["fix"] = meta["fix"]
    return result


# ---------------------------------------------------------------------------
# Minimal TOML parser fallback for Python 3.10 (3.11+ uses stdlib tomllib)
# ---------------------------------------------------------------------------

def _strip_toml_comment(line: str) -> str:
    in_single = False
    in_double = False
    escaped = False
    for index, ch in enumerate(line):
        if escaped:
            escaped = False
            continue
        if in_double and ch == "\\":
            escaped = True
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "'" and not in_double:
            in_single = not in_single
        elif ch == "#" and not in_single and not in_double:
            return line[:index]
    return line


def _split_toml_top(text: str, sep: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    in_single = False
    in_double = False
    escaped = False
    depth = 0
    for ch in text:
        if escaped:
            buf.append(ch)
            escaped = False
            continue
        if in_double and ch == "\\":
            buf.append(ch)
            escaped = True
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "'" and not in_double:
            in_single = not in_single
        elif not in_single and not in_double:
            if ch in "{[":
                depth += 1
            elif ch in "}]":
                depth = max(0, depth - 1)
            elif ch == sep and depth == 0:
                parts.append("".join(buf))
                buf = []
                continue
        buf.append(ch)
    parts.append("".join(buf))
    return parts


def _parse_toml_key(raw: str) -> list[str]:
    keys: list[str] = []
    for part in _split_toml_top(raw.strip(), "."):
        token = part.strip()
        if (len(token) >= 2 and token[0] == token[-1] and token[0] in ('"', "'")):
            token = token[1:-1]
        if token:
            keys.append(token)
    return keys


def _parse_toml_value(raw: str):
    val = raw.strip()
    if not val:
        return ""
    if val[0] == '"' and val[-1] == '"' and len(val) >= 2:
        try:
            return json.loads(val)
        except ValueError:
            return val[1:-1]
    if val[0] == "'" and val[-1] == "'" and len(val) >= 2:
        return val[1:-1]
    if val in ("true", "false"):
        return val == "true"
    if val.startswith("{") and val.endswith("}"):
        table: dict = {}
        inner = val[1:-1].strip()
        if inner:
            for item in _split_toml_top(inner, ","):
                if "=" in item:
                    k, v = item.split("=", 1)
                    _assign_nested(table, _parse_toml_key(k), _parse_toml_value(v))
        return table
    if val.startswith("[") and val.endswith("]"):
        inner = val[1:-1].strip()
        if not inner:
            return []
        return [_parse_toml_value(item) for item in _split_toml_top(inner, ",") if item.strip()]
    return val


def _assign_nested(root: dict, keys: list[str], value) -> None:
    if not keys:
        return
    node = root
    for key in keys[:-1]:
        existing = node.get(key)
        if not isinstance(existing, dict):
            existing = {}
            node[key] = existing
        node = existing
    node[keys[-1]] = value


def _parse_toml(text: str) -> dict | None:
    if tomllib is not None:
        try:
            parsed = tomllib.loads(text)
            return parsed if isinstance(parsed, dict) else None
        except ValueError:
            return None
    root: dict = {}
    section: list[str] = []
    for raw_line in text.split("\n"):
        line = _strip_toml_comment(raw_line).strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            header = line[2:-2] if line.startswith("[[") and line.endswith("]]") else line[1:-1]
            section = _parse_toml_key(header)
            continue
        if "=" in line:
            key_raw, val_raw = line.split("=", 1)
            keys = section + _parse_toml_key(key_raw)
            _assign_nested(root, keys, _parse_toml_value(val_raw))
    return root


# ---------------------------------------------------------------------------
# Check 1: agents-md-drift
# ---------------------------------------------------------------------------

INSTRUCTION_FILENAMES = frozenset({"AGENTS.md", "CLAUDE.md", ".cursorrules"})
CURSOR_RULE_EXTENSIONS = frozenset({".md", ".mdc", ".txt", ""})
SHELL_FENCE_LANGS = frozenset({"sh", "bash", "shell", "zsh", "console"})
FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")

PATH_EXTENSIONS = frozenset({
    ".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".sh", ".bash", ".zsh", ".rb", ".go", ".rs", ".java", ".kt", ".scala",
    ".c", ".h", ".cpp", ".hpp", ".cc", ".cs", ".php", ".swift", ".sql",
    ".json", ".jsonc", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".md", ".mdx", ".mdc", ".txt", ".rst", ".html", ".css", ".scss", ".less",
    ".xml", ".lock", ".mk", ".tf", ".hcl", ".proto", ".graphql", ".gql",
    ".vue", ".svelte", ".gradle", ".nix", ".lua", ".ex", ".exs", ".zig",
    ".dockerfile", ".service", ".timer", ".path",
})

ROOT_FILE_EXTENSIONS = frozenset({
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".sh", ".bash", ".zsh", ".rb", ".go", ".rs",
    ".md", ".mdx", ".rst", ".txt",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".mk", ".sql", ".html", ".css",
})

BARE_FILE_NON_PATHS = frozenset({
    "node.js", "next.js", "vue.js", "react.js", "nuxt.js", "nest.js",
    "express.js", "three.js", "d3.js", "chart.js", "alpine.js", "solid.js",
    "svelte.js", "ember.js", "bun.sh", "deno.ts",
    "res.json", "req.json", "resp.json", "response.json", "body.json", "r.json",
    "os.path",
})

BARE_PLACEHOLDER_STEMS = frozenset({
    "foo", "bar", "baz", "qux", "example", "sample", "dummy", "placeholder",
    "file", "filename", "path", "script", "module", "your_file", "your-file",
    "my_file", "my-file", "your_script", "your-script", "output", "input",
})

STANDARD_REPO_DIRS = frozenset({
    "src", "lib", "app", "apps", "pkg", "pkgs", "packages", "scripts",
    "tests", "test", "spec", "docs", "doc", "config", "cmd", "internal",
    "bin", "tools", "hooks", "checks", "bench", "library", "migrations",
    ".github", ".claude", ".cursor", ".codex", ".fleet", ".githooks", ".coldlocker",
})

GENERATED_OR_RUNTIME_DIRS = frozenset({
    ".git", "node_modules", ".venv", "venv", "env", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".nox",
    "dist", "build", "target", "out", ".next", ".nuxt", ".cache",
    "coverage", ".coldfix", ".coldrecall", ".worktrees", "tmp", "temp",
    "logs", "exports",
})

MIME_PREFIXES = frozenset({
    "application", "text", "image", "audio", "video", "font", "multipart", "message",
})

DOMAIN_TLDS = frozenset({
    ".com", ".org", ".net", ".io", ".dev", ".ai", ".app", ".gov", ".edu",
    ".co", ".invalid", ".local", ".internal", ".localhost",
})

PLACEHOLDER_BARE_PATHS = frozenset({
    "foo/bar", "foo/bar/baz", "bar/baz", "a/b", "a/b/c", "x/y", "x/y/z",
    "dir/file", "dir/subdir", "old/new", "old/path", "new/path",
    "src/dst", "before/after", "read/write", "pass/fail", "true/false",
    "yes/no", "and/or", "in/out", "input/output", "get/set", "min/max",
    "key/value", "http/https", "stdout/stderr", "ci/cd", "i/o", "n/a",
    "w/o", "owner/repo", "org/repo", "user/repo", "your-org/your-repo",
    "name/version", "module/name",
})

PLACEHOLDER_PREFIXES = (
    "path/to/", "your-", "my-", "example/", "example-", "<",
)

STANDARD_CLI_TOOLS = frozenset({
    "python", "python3", "pytest", "mypy", "ruff", "black", "isort", "flake8",
    "pre-commit", "uvicorn", "gunicorn", "flask", "django-admin", "alembic",
    "celery", "sphinx-build", "mkdocs", "tox", "nox", "twine", "pip", "build",
    "coverage", "bandit", "pylint", "pyright", "ty", "basedpyright", "bash",
    "sh", "zsh", "env", "node", "npm", "npx", "pnpm", "yarn", "bun", "git",
    "make", "cargo", "go", "rustc", "tsc", "eslint", "prettier", "vitest",
    "jest", "playwright", "docker", "kubectl", "terraform", "shellcheck",
    "shfmt", "jq", "curl", "tar", "rsync", "ssh", "scp",
})

SCRIPT_RUNNERS = frozenset({
    "bash", "sh", "zsh", "python", "python3", "node", "tsx", "ts-node", "ruby", "perl",
})

VALID_PATH_RE = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
LINE_SUFFIX_RE = re.compile(r":\d+(?:-\d+|:\d+)?$")
MAKE_TARGET_RE = re.compile(r"^([A-Za-z0-9_./-]+)\s*:(?!=)")
MAKE_PHONY_RE = re.compile(r"^\.PHONY\s*:\s*(.+)$")
MAKE_INCLUDE_RE = re.compile(r"^-?include\s+(.+)$")
SHELL_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
PLACEHOLDER_TOKEN_RE = re.compile(r"[<>{}$*?\[\]]|\.\.\.")
DEP_NAME_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)")


def _is_instruction_file(rel_path: str) -> bool:
    parts = Path(rel_path).parts
    if not parts:
        return False
    name = parts[-1]
    if name in INSTRUCTION_FILENAMES:
        return True
    for index in range(len(parts) - 2):
        if parts[index] == ".cursor" and parts[index + 1] == "rules":
            return Path(name).suffix.lower() in CURSOR_RULE_EXTENSIONS
    return False


def _extract_inline_code_spans(line: str) -> list[str]:
    spans: list[str] = []
    index = 0
    length = len(line)
    while index < length:
        if line[index] != "`":
            index += 1
            continue
        end_ticks = index
        while end_ticks < length and line[end_ticks] == "`":
            end_ticks += 1
        tick_count = end_ticks - index
        needle = "`" * tick_count
        search = end_ticks
        found = -1
        while search < length:
            pos = line.find(needle, search)
            if pos == -1:
                break
            after = pos + tick_count
            if (pos == end_ticks or line[pos - 1] != "`") and (after == length or line[after] != "`"):
                found = pos
                break
            search = pos + 1
        if found == -1:
            index = end_ticks
            continue
        spans.append(line[end_ticks:found].strip())
        index = found + tick_count
    return spans


def _iter_instruction_items(text: str):
    """Yield (line_number, kind, text) where kind is 'inline' or 'shell'."""
    fence_char = ""
    fence_len = 0
    fence_lang = ""
    for line_no, line in enumerate(text.split("\n"), 1):
        stripped = line.strip()
        if fence_char:
            if stripped.startswith(fence_char * fence_len) and not stripped.lstrip(fence_char).strip():
                fence_char = ""
                fence_len = 0
                fence_lang = ""
            elif fence_lang in SHELL_FENCE_LANGS:
                if stripped and not stripped.startswith("#"):
                    yield line_no, "shell", stripped
            continue
        opened = FENCE_OPEN_RE.match(line)
        if opened:
            marker = opened.group(1)
            info = opened.group(2).strip()
            if marker[0] == "`" and "`" in info:
                # Inline code spans on a line starting with 3+ backticks, not a fence.
                for span in _extract_inline_code_spans(line):
                    if span:
                        yield line_no, "inline", span
                continue
            fence_char = marker[0]
            fence_len = len(marker)
            fence_lang = info.split()[0].lower() if info else ""
            continue
        for span in _extract_inline_code_spans(line):
            if span:
                yield line_no, "inline", span


def _is_placeholder_token(token: str) -> bool:
    if not token or PLACEHOLDER_TOKEN_RE.search(token):
        return True
    upper = token.upper()
    if upper.startswith(("YOUR_", "MY_", "EXAMPLE_", "REPLACE_")):
        return True
    if token.lower() in ("your-target", "your-script", "target", "script", "path", "file"):
        return True
    return False


def _normalize_concrete_path(
    raw: str,
    existing_top_dirs: frozenset[str],
    *,
    allow_bare_file: bool = False,
) -> str | None:
    token = raw.strip()
    if not token or any(ch.isspace() for ch in token):
        return None
    if token[0] in "-/~$%@#:'\"!?":
        return None
    if "://" in token or token.startswith(("mailto:", "git@")):
        return None
    if any(ch in token for ch in "*?[]{}<>|;&()'\",\\=") or "..." in token or (":" * 2) in token or "//" in token:
        return None

    explicit_dot_slash = token.startswith("./")
    if explicit_dot_slash:
        token = token[2:]
    if not token:
        return None

    trailing_slash = token.endswith("/")
    if trailing_slash:
        token = token.rstrip("/")
    else:
        token = LINE_SUFFIX_RE.sub("", token)
    if not token or not VALID_PATH_RE.fullmatch(token):
        return None

    segments = token.split("/")
    if any(seg in ("", ".", "..") for seg in segments):
        return None

    lowered = token.lower()
    if lowered in PLACEHOLDER_BARE_PATHS:
        return None
    if any(lowered.startswith(prefix) for prefix in PLACEHOLDER_PREFIXES):
        return None

    first = segments[0]
    first_lower = first.lower()
    if first_lower in MIME_PREFIXES:
        return None
    if any(first_lower.endswith(tld) for tld in DOMAIN_TLDS):
        return None
    if first in GENERATED_OR_RUNTIME_DIRS or first_lower in GENERATED_OR_RUNTIME_DIRS:
        return None

    if explicit_dot_slash:
        return token

    if "/" not in token:
        if token.startswith("."):
            return None
        if lowered in BARE_FILE_NON_PATHS:
            return None
        stem, dot, suffix = token.rpartition(".")
        if not dot or not stem or stem.lower() in BARE_PLACEHOLDER_STEMS:
            return None
        ext = "." + suffix.lower()
        if allow_bare_file:
            return token if ext in PATH_EXTENSIONS else None
        if ext in ROOT_FILE_EXTENSIONS:
            return token
        return None

    ext = Path(segments[-1]).suffix.lower()
    has_ext = ext in PATH_EXTENSIONS
    if has_ext or trailing_slash or first in existing_top_dirs or first in STANDARD_REPO_DIRS:
        return token
    return None


def _mode_is_regular(tree: Path, rel_path: str, mode: dict | None = None) -> bool:
    norm = posixpath.normpath(rel_path.replace("\\", "/"))
    if mode and mode.get("staged"):
        return norm in mode["staged_regular"]
    return _is_safe_regular(tree, norm)


def _mode_read_text(tree: Path, rel_path: str, mode: dict | None = None) -> str | None:
    if mode and mode.get("staged"):
        return _read_staged_text_safe(
            tree,
            rel_path,
            prefix=mode["prefix"],
            staged_regular=mode["staged_regular"],
        )
    return _read_text_safe(tree, rel_path)


def _parse_makefile(tree: Path, makefile_rel: str, mode: dict | None = None) -> tuple[set[str], bool, bool]:
    """Return (targets, has_pattern_rule, has_dynamic_include)."""
    targets: set[str] = set()
    has_pattern = False
    has_dynamic = False
    seen: set[str] = set()

    def load(rel: str) -> None:
        nonlocal has_pattern, has_dynamic
        norm = posixpath.normpath(rel)
        if norm in seen:
            return
        seen.add(norm)
        text = _mode_read_text(tree, norm, mode)
        if text is None:
            return
        parent = posixpath.dirname(norm)
        for line in text.split("\n"):
            if not line or line.startswith(("\t", " ", "#")):
                continue
            phony = MAKE_PHONY_RE.match(line)
            if phony:
                for tok in phony.group(1).split():
                    if not tok.startswith("$"):
                        targets.add(tok)
                continue
            inc = MAKE_INCLUDE_RE.match(line)
            if inc:
                for tok in inc.group(1).split():
                    if any(ch in tok for ch in "$*?[]"):
                        has_dynamic = True
                    else:
                        inc_path = posixpath.join(parent, tok) if parent else tok
                        if _mode_is_regular(tree, inc_path, mode):
                            load(inc_path)
                        else:
                            has_dynamic = True
                continue
            match = MAKE_TARGET_RE.match(line)
            if match:
                lhs = match.group(1)
                if "%" in lhs:
                    has_pattern = True
                elif not lhs.startswith("."):
                    targets.add(lhs)

    load(makefile_rel)
    return targets, has_pattern, has_dynamic


def _find_makefile(tree: Path, base_dir: str, mode: dict | None = None) -> str | None:
    for name in ("Makefile", "makefile", "GNUmakefile"):
        candidate = posixpath.join(base_dir, name) if base_dir not in ("", ".") else name
        if _mode_is_regular(tree, candidate, mode):
            return candidate
    return None


def _load_package_scripts(tree: Path, base_dir: str, mode: dict | None = None) -> tuple[bool, set[str]]:
    rel = posixpath.join(base_dir, "package.json") if base_dir not in ("", ".") else "package.json"
    text = _mode_read_text(tree, rel, mode)
    if text is None:
        return False, set()
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return True, set()
    scripts = data.get("scripts") if isinstance(data, dict) else None
    if isinstance(scripts, dict):
        return True, {str(k) for k in scripts}
    return True, set()


def _normalize_pkg_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _load_pyproject_info(tree: Path, base_dir: str, mode: dict | None = None) -> tuple[bool, set[str], set[str]]:
    """Return (exists, scripts, declared_deps)."""
    rel = posixpath.join(base_dir, "pyproject.toml") if base_dir not in ("", ".") else "pyproject.toml"
    text = _mode_read_text(tree, rel, mode)
    if text is None:
        return False, set(), set()
    data = _parse_toml(text)
    if not isinstance(data, dict):
        return True, set(), set()

    scripts: set[str] = set()
    deps: set[str] = set()
    project = data.get("project")
    if isinstance(project, dict):
        for key in ("scripts", "gui-scripts"):
            table = project.get(key)
            if isinstance(table, dict):
                scripts.update(str(k) for k in table)
        raw_deps = project.get("dependencies")
        if isinstance(raw_deps, list):
            for entry in raw_deps:
                if isinstance(entry, str):
                    m = DEP_NAME_RE.match(entry.strip())
                    if m:
                        deps.add(_normalize_pkg_name(m.group(1)))
        opt = project.get("optional-dependencies")
        if isinstance(opt, dict):
            for items in opt.values():
                if isinstance(items, list):
                    for entry in items:
                        if isinstance(entry, str):
                            m = DEP_NAME_RE.match(entry.strip())
                            if m:
                                deps.add(_normalize_pkg_name(m.group(1)))

    dep_groups = data.get("dependency-groups")
    if isinstance(dep_groups, dict):
        for items in dep_groups.values():
            if isinstance(items, list):
                for entry in items:
                    if isinstance(entry, str):
                        m = DEP_NAME_RE.match(entry.strip())
                        if m:
                            deps.add(_normalize_pkg_name(m.group(1)))

    tool = data.get("tool")
    if isinstance(tool, dict):
        poetry = tool.get("poetry")
        if isinstance(poetry, dict):
            if isinstance(poetry.get("scripts"), dict):
                scripts.update(str(k) for k in poetry["scripts"])
            if isinstance(poetry.get("dependencies"), dict):
                deps.update(_normalize_pkg_name(str(k)) for k in poetry["dependencies"])
            groups = poetry.get("group")
            if isinstance(groups, dict):
                for grp in groups.values():
                    if isinstance(grp, dict) and isinstance(grp.get("dependencies"), dict):
                        deps.update(_normalize_pkg_name(str(k)) for k in grp["dependencies"])
        pdm = tool.get("pdm")
        if isinstance(pdm, dict) and isinstance(pdm.get("scripts"), dict):
            scripts.update(str(k) for k in pdm["scripts"])
        hatch = tool.get("hatch")
        if isinstance(hatch, dict):
            envs = hatch.get("envs")
            if isinstance(envs, dict):
                for env_cfg in envs.values():
                    if isinstance(env_cfg, dict) and isinstance(env_cfg.get("scripts"), dict):
                        scripts.update(str(k) for k in env_cfg["scripts"])
    return True, scripts, deps


def _split_shell_subcommands(command: str) -> list[str]:
    cleaned = command.strip()
    if cleaned.startswith("$ "):
        cleaned = cleaned[2:].strip()
    parts: list[str] = []
    buf: list[str] = []
    in_single = False
    in_double = False
    escaped = False
    index = 0
    length = len(cleaned)
    while index < length:
        ch = cleaned[index]
        if escaped:
            buf.append(ch)
            escaped = False
            index += 1
            continue
        if ch == "\\" and not in_single:
            buf.append(ch)
            escaped = True
            index += 1
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            buf.append(ch)
            index += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            buf.append(ch)
            index += 1
            continue
        if not in_single and not in_double:
            if cleaned[index:index + 2] in ("&&", "||"):
                parts.append("".join(buf).strip())
                buf = []
                index += 2
                continue
            if ch in ";|":
                parts.append("".join(buf).strip())
                buf = []
                index += 1
                continue
        buf.append(ch)
        index += 1
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return [p for p in parts if p]


def _path_exists_relative(tree: Path, instr_parent: str, rel: str, *, mode: dict | None = None) -> bool:
    staged = bool(mode and mode.get("staged"))
    staged_regular = mode["staged_regular"] if staged else None
    staged_dirs = mode["staged_dirs"] if staged else None
    if _exists_for_mode(tree, rel, staged=staged, staged_regular=staged_regular, staged_dirs=staged_dirs):
        return True
    if instr_parent not in ("", "."):
        nested = posixpath.join(instr_parent, rel)
        if _exists_for_mode(tree, nested, staged=staged, staged_regular=staged_regular, staged_dirs=staged_dirs):
            return True
    return False


def _resolve_dir_arg(base_dir: str, arg: str) -> str | None:
    if not arg or arg.startswith("/") or PLACEHOLDER_TOKEN_RE.search(arg):
        return None
    joined = posixpath.normpath(posixpath.join(base_dir, arg) if base_dir not in ("", ".") else arg)
    if joined == ".." or joined.startswith("../"):
        return None
    return "" if joined == "." else joined


def _check_command(
    tree: Path,
    instr_parent: str,
    command: str,
    existing_top_dirs: frozenset[str],
    cache: dict,
    *,
    mode: dict | None = None,
) -> list[tuple[str, str]]:
    """Return list of (target_path_or_source, message) for stale command refs."""
    issues: list[tuple[str, str]] = []
    for sub in _split_shell_subcommands(command):
        try:
            tokens = shlex.split(sub, comments=True)
        except ValueError:
            continue
        while tokens and (SHELL_ASSIGN_RE.match(tokens[0]) or tokens[0] in ("env", "time", "sudo")):
            if tokens[0] in ("env", "sudo"):
                tokens.pop(0)
                while tokens and tokens[0].startswith("-"):
                    tokens.pop(0)
            else:
                tokens.pop(0)
        if not tokens:
            continue

        cmd = tokens[0]
        args = tokens[1:]

        # 1. make <target>
        if cmd == "make":
            make_dir = ""
            makefile_override = None
            targets_to_check: list[str] = []
            idx = 0
            while idx < len(args):
                tok = args[idx]
                if tok == "--":
                    break
                if tok in ("-C", "--directory") and idx + 1 < len(args):
                    resolved = _resolve_dir_arg(make_dir, args[idx + 1])
                    if resolved is None:
                        make_dir = None  # external/placeholder dir, skip
                        break
                    make_dir = resolved
                    idx += 2
                    continue
                if tok in ("-f", "--file", "--makefile") and idx + 1 < len(args):
                    makefile_override = args[idx + 1]
                    idx += 2
                    continue
                if tok.startswith("-"):
                    idx += 1
                    continue
                if "=" in tok:
                    idx += 1
                    continue
                targets_to_check.append(tok)
                idx += 1
            if make_dir is None or not targets_to_check:
                continue
            search_dirs = [make_dir] if make_dir else ([instr_parent, ""] if instr_parent not in ("", ".") else [""])
            mf = None
            if makefile_override:
                cand = posixpath.join(make_dir, makefile_override) if make_dir else makefile_override
                if _mode_is_regular(tree, cand, mode):
                    mf = cand
            else:
                for sd in search_dirs:
                    mf = _find_makefile(tree, sd, mode)
                    if mf:
                        break
            if mf is None:
                for target in targets_to_check:
                    if not _is_placeholder_token(target):
                        issues.append(("Makefile", f"missing Makefile for `make {target}`"))
            else:
                if ("make", mf) not in cache:
                    cache[("make", mf)] = _parse_makefile(tree, mf, mode)
                defined, has_pattern, has_dynamic = cache[("make", mf)]
                if not has_pattern and not has_dynamic:
                    for target in targets_to_check:
                        if not _is_placeholder_token(target) and target not in defined:
                            issues.append((mf, f"missing make target `{target}`"))
            continue

        # 2. npm / pnpm / yarn / bun scripts
        if cmd in ("npm", "pnpm", "yarn", "bun"):
            pkg_dir = ""
            idx = 0
            script_name = None
            explicit_run = False
            while idx < len(args):
                tok = args[idx]
                if tok in ("--prefix", "-C", "--cwd") and idx + 1 < len(args):
                    resolved = _resolve_dir_arg(pkg_dir, args[idx + 1])
                    if resolved is None:
                        pkg_dir = None
                        break
                    pkg_dir = resolved
                    idx += 2
                    continue
                if tok.startswith("-"):
                    idx += 1
                    continue
                if tok == "run":
                    explicit_run = True
                    idx += 1
                    while idx < len(args) and args[idx].startswith("-"):
                        idx += 1
                    if idx < len(args) and args[idx] != "--":
                        script_name = args[idx]
                    break
                if cmd == "npm" and tok in ("test", "start"):
                    script_name = tok
                    break
                break
            if pkg_dir is None or not script_name or _is_placeholder_token(script_name):
                continue
            search_dirs = [pkg_dir] if pkg_dir else ([instr_parent, ""] if instr_parent not in ("", ".") else [""])
            pkg_exists = False
            scripts: set[str] = set()
            pkg_rel = "package.json"
            for sd in search_dirs:
                key = ("pkg", sd)
                if key not in cache:
                    cache[key] = _load_package_scripts(tree, sd, mode)
                exists, found_scripts = cache[key]
                if exists:
                    pkg_exists = True
                    scripts = found_scripts
                    pkg_rel = posixpath.join(sd, "package.json") if sd else "package.json"
                    break
            if not pkg_exists:
                if explicit_run:
                    issues.append(("package.json", f"missing package.json for `{cmd} run {script_name}`"))
            elif script_name not in scripts:
                issues.append((pkg_rel, f"missing npm script `{script_name}`"))
            continue

        # 3. poetry / uv / pdm / hatch run <script or path>
        if cmd in ("poetry", "uv", "pdm", "hatch") and args and args[0] == "run":
            idx = 1
            while idx < len(args):
                tok = args[idx]
                if tok in ("--directory", "--project", "--with", "--extra", "--group", "-C", "-p", "--python", "-e") and idx + 1 < len(args):
                    idx += 2
                    continue
                if tok.startswith("-"):
                    idx += 1
                    continue
                break
            if idx >= len(args):
                continue
            target = args[idx]
            rest = args[idx + 1:]
            if target in SCRIPT_RUNNERS:
                # Recurse on e.g. `uv run python scripts/foo.py`
                issues.extend(_check_command(tree, instr_parent, shlex.join([target, *rest]), existing_top_dirs, cache, mode=mode))
                continue
            if _is_placeholder_token(target):
                continue
            # Is it a file path like `uv run scripts/check.py` or `uv run verify.py`?
            concrete = _normalize_concrete_path(target, existing_top_dirs, allow_bare_file=True)
            if concrete is not None:
                if not _path_exists_relative(tree, instr_parent, concrete, mode=mode):
                    issues.append((concrete, f"missing script `{target}`"))
                continue
            if "/" in target or target.startswith("."):
                continue
            if target.lower() in STANDARD_CLI_TOOLS:
                continue
            key = ("pyproject", "")
            if key not in cache:
                cache[key] = _load_pyproject_info(tree, "", mode=mode)
            py_exists, py_scripts, py_deps = cache[key]
            if py_exists and py_scripts:
                if target not in py_scripts and _normalize_pkg_name(target) not in py_deps:
                    issues.append(("pyproject.toml", f"missing pyproject script `{target}`"))
            continue

        # 4. Direct interpreter script invocation: bash/sh/python3/node <script_path>
        if cmd in SCRIPT_RUNNERS:
            idx = 0
            stop = False
            while idx < len(args):
                tok = args[idx]
                if tok in ("-c", "-m", "-e", "--eval", "-"):
                    stop = True
                    break
                if tok == "-o" and idx + 1 < len(args):
                    idx += 2
                    continue
                if tok.startswith("-"):
                    idx += 1
                    continue
                break
            if stop or idx >= len(args):
                continue
            script_arg = args[idx]
            if _is_placeholder_token(script_arg):
                continue
            concrete = _normalize_concrete_path(script_arg, existing_top_dirs, allow_bare_file=True)
            if concrete is not None and not _path_exists_relative(tree, instr_parent, concrete, mode=mode):
                issues.append((concrete, f"missing script `{script_arg}`"))
            continue

        # 5. Direct repo script execution: `./scripts/check.sh` or `scripts/check.sh`
        if cmd.startswith("./") or ("/" in cmd and not cmd.startswith("/")):
            if not _is_placeholder_token(cmd):
                concrete = _normalize_concrete_path(cmd, existing_top_dirs, allow_bare_file=True)
                if concrete is not None and not _path_exists_relative(tree, instr_parent, concrete, mode=mode):
                    issues.append((concrete, f"missing script `{cmd}`"))
    return issues


def _deleted_or_renamed_paths(tree: Path, base: str | None, staged: bool) -> set[str]:
    if not staged and base is None:
        return set()
    if staged:
        args = ["diff", "--cached", "--name-status", "--relative", "-z", "--no-ext-diff", "-M", "--"]
    else:
        args = ["diff", "--name-status", "--relative", "-z", "--no-ext-diff", "-M", base, "--"]
    res = _git(tree, *args)
    if res.returncode != 0:
        return set()
    tokens = res.stdout.split("\0")
    removed: set[str] = set()
    idx = 0
    while idx < len(tokens):
        status = tokens[idx]
        if not status:
            idx += 1
            continue
        if status.startswith("D") and idx + 1 < len(tokens):
            removed.add(tokens[idx + 1])
            idx += 2
        elif status.startswith(("R", "C")) and idx + 2 < len(tokens):
            if status.startswith("R"):
                removed.add(tokens[idx + 1])
            idx += 3
        else:
            idx += 2
    return removed


def check_agents_md_drift(
    tree: Path,
    *,
    base: str | None = None,
    staged: bool = False,
    changed: set[str] | None = None,
) -> dict:
    try:
        mode = None
        if staged:
            staged_regular = _list_staged_regular_files(tree)
            staged_dirs: set[str] = set()
            for sp in staged_regular:
                parent = posixpath.dirname(sp)
                while parent and parent != ".":
                    staged_dirs.add(parent)
                    parent = posixpath.dirname(parent)
            mode = {
                "staged": True,
                "prefix": _git_prefix(tree),
                "staged_regular": staged_regular,
                "staged_dirs": staged_dirs,
            }
            files = sorted(staged_regular)
            existing_top_dirs = frozenset(p.split("/", 1)[0] for p in staged_regular if "/" in p)
        else:
            files = _list_repo_files(tree)
            existing_top_dirs = frozenset(
                item.name for item in tree.iterdir() if item.is_dir() and not item.is_symlink() and item.name != ".git"
            )

        instr_files = [p for p in files if _is_instruction_file(p)]
        if not instr_files:
            return _format_result("agents-md-drift", [])

        diff_mode = base is not None or staged
        changed_set = changed or set()
        deleted_paths = _deleted_or_renamed_paths(tree, base, staged) if diff_mode else set()
        deleted_dirs = set()
        for dp in deleted_paths:
            parent = posixpath.dirname(dp)
            while parent and parent != ".":
                deleted_dirs.add(parent)
                parent = posixpath.dirname(parent)

        cache: dict = {}
        hits: list[str] = []
        for instr_path in instr_files:
            text = _mode_read_text(tree, instr_path, mode)
            if text is None:
                continue
            instr_parent = posixpath.dirname(instr_path)
            instr_changed = not diff_mode or instr_path in changed_set
            seen_on_line: set[tuple[int, str]] = set()
            for line_no, kind, snippet in _iter_instruction_items(text):
                if kind == "shell":
                    for ref, msg in _check_command(tree, instr_parent, snippet, existing_top_dirs, cache, mode=mode):
                        if not instr_changed and ref not in changed_set and ref not in deleted_paths and ref not in deleted_dirs:
                            continue
                        if (line_no, msg) not in seen_on_line:
                            seen_on_line.add((line_no, msg))
                            hits.append(f"{instr_path}:{line_no}: {msg}")
                    continue

                # Inline backtick span
                if any(ch.isspace() for ch in snippet):
                    for ref, msg in _check_command(tree, instr_parent, snippet, existing_top_dirs, cache, mode=mode):
                        if not instr_changed and ref not in changed_set and ref not in deleted_paths and ref not in deleted_dirs:
                            continue
                        if (line_no, msg) not in seen_on_line:
                            seen_on_line.add((line_no, msg))
                            hits.append(f"{instr_path}:{line_no}: {msg}")
                    continue

                concrete = _normalize_concrete_path(snippet, existing_top_dirs)
                if concrete is not None and not _path_exists_relative(tree, instr_parent, concrete, mode=mode):
                    norm_ref = posixpath.normpath(concrete)
                    nested_ref = posixpath.normpath(posixpath.join(instr_parent, norm_ref)) if instr_parent else norm_ref
                    if not instr_changed and not ({norm_ref, nested_ref} & (deleted_paths | deleted_dirs)):
                        continue
                    msg = f"missing path `{snippet}`"
                    if (line_no, msg) not in seen_on_line:
                        seen_on_line.add((line_no, msg))
                        hits.append(f"{instr_path}:{line_no}: {msg}")
        return _format_result("agents-md-drift", hits)
    except Exception as error:
        return _format_result("agents-md-drift", [], reason=repr(error))


# ---------------------------------------------------------------------------
# Check 2: agent-config-secrets
# ---------------------------------------------------------------------------

EXTRA_SECRET_TOKEN_RE = re.compile(
    r"\b(?:ASIA[A-Z0-9]{16}|AIza[A-Za-z0-9_-]{30,}|"
    r"sk-ant-[A-Za-z0-9_-]{20,}|npm_[A-Za-z0-9]{20,}|pypi-[A-Za-z0-9_-]{20,}|"
    r"hf_[A-Za-z0-9]{20,})\b"
)
BASIC_AUTH_URL_RE = re.compile(r"\b[a-z][a-z0-9+.-]*://([^\s/@${}<>]+):([^\s/@${}<>]+)@")
BEARER_VALUE_RE = re.compile(r"^\s*(?:Bearer|Basic|Token)\s+(\S+)\s*$", re.I)
BEARER_INLINE_RE = re.compile(r"\bBearer\s+([A-Za-z0-9._~+/=-]{16,})\b")
ENV_LINE_ASSIGN_RE = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]{2,})\s*=\s*(.+?)\s*$")

SECRET_KEY_HINT_RE = re.compile(
    r"(?:^|[_-])(?:api[_-]?key|apikey|secret|token|password|passwd|credential|auth[_-]?token|access[_-]?key|private[_-]?key|pat|bearer)(?:$|[_-])",
    re.I,
)
URL_KEY_HINT_RE = re.compile(r"(?:^|[_-])(?:url|uri|dsn|endpoint|connection[_-]?string)(?:$|[_-])", re.I)
ENV_INDIRECTION_KEY_RE = re.compile(r"(?:_env(?:_var|_name)?|env_key|env_var|env_name|token_env|key_env)$", re.I)

AUTH_HEADER_NAMES = frozenset({
    "authorization", "proxy-authorization", "x-api-key", "api-key",
    "x-auth-token", "x-access-token", "x-secret", "private-token",
    "x-github-token", "x-gitlab-token", "cookie", "set-cookie",
})

PLACEHOLDER_VALUES = frozenset({
    "", "none", "null", "true", "false", "disabled", "enabled", "todo", "tbd",
    "changeme", "change_me", "change-me", "replace_me", "replace-me",
    "placeholder", "example", "dummy", "redacted", "unset", "empty",
    "your-api-key", "your_api_key", "your-token", "your_token",
    "your-secret", "your_secret", "insert-key-here",
})
PLACEHOLDER_VAR_RE = re.compile(
    r"^(?:\$\{[^}]+\}|\$[A-Za-z_][A-Za-z0-9_]*|\{\{[^}]+\}\}|%[A-Za-z_][A-Za-z0-9_]*%|<[^>]+>|env:[A-Za-z_][A-Za-z0-9_]*|\*{3,}|x{4,}|X{4,})$"
)
ENV_DEFAULT_SECRET_RE = re.compile(r"^\$\{[^}:]+:-(.+)\}$")
CLAUDE_PERM_RULE_RE = re.compile(r"^[A-Z][A-Za-z0-9_-]*\([^)]*\)$")


def _is_agent_config_file(rel_path: str) -> bool:
    parts = Path(rel_path).parts
    if len(parts) < 2:
        return False
    for index, part in enumerate(parts[:-1]):
        if part == ".claude":
            if index + 1 == len(parts) - 1:
                name = parts[-1]
                return name.startswith("settings") and name.endswith(".json")
        if part in (".codex", ".cursor"):
            return True
    return False


def _is_placeholder_secret(value: str) -> bool:
    stripped = value.strip()
    if not stripped:
        return True
    if stripped.lower() in PLACEHOLDER_VALUES:
        return True
    # Check ${VAR:-default}: safe unless the default itself is a literal secret.
    default_match = ENV_DEFAULT_SECRET_RE.match(stripped)
    if default_match:
        default_val = default_match.group(1).strip()
        if not default_val:
            return True
        return _is_placeholder_secret(default_val)
    if PLACEHOLDER_VAR_RE.match(stripped):
        return True
    if stripped.upper().startswith(("YOUR_", "REPLACE_", "EXAMPLE_", "DUMMY_", "FAKE_")):
        return True
    if stripped.startswith(("<", "${", "{{")) and stripped.endswith((">", "}", "}}")):
        return True
    return False


def _extract_secret_shape_matches(text: str) -> list[tuple[str, str]]:
    """Reuse coldfix.SECRET_SHAPE + extra prefixes + placeholder-safe basic-auth URLs."""
    found: list[tuple[str, str]] = []
    for match in coldfix.SECRET_SHAPE.finditer(text):
        raw = match.group(0)
        if "://" in raw and "@" in raw:
            # Handled with placeholder filtering below.
            continue
        found.append(("literal credential token", raw))
    for match in EXTRA_SECRET_TOKEN_RE.finditer(text):
        found.append(("literal credential token", match.group(0)))
    for match in BASIC_AUTH_URL_RE.finditer(text):
        pw_part = match.group(2)
        if not _is_placeholder_secret(pw_part):
            found.append(("basic-auth URL credential", pw_part))
    return found


def _redact_text(text: str, secrets: set[str]) -> str:
    redacted = coldfix.SECRET_SHAPE.sub("[redacted]", text)
    redacted = EXTRA_SECRET_TOKEN_RE.sub("[redacted]", redacted)
    redacted = BASIC_AUTH_URL_RE.sub("[redacted]", redacted)
    redacted = BEARER_INLINE_RE.sub("Bearer [redacted]", redacted)
    for secret in sorted(secrets, key=len, reverse=True):
        if secret and len(secret) >= 4:
            redacted = redacted.replace(secret, "[redacted]")
    return redacted


def _is_sensitive_key_segment(seg: str, secrets: set[str]) -> bool:
    if not seg or (seg.startswith("[") and seg.endswith("]")):
        return False
    if _extract_secret_shape_matches(seg) or BEARER_INLINE_RE.search(seg):
        return True
    if any(secret in seg for secret in secrets if len(secret) >= 4):
        return True
    if any(ch.isspace() or ch in ":@" for ch in seg):
        return True
    if len(seg) >= 16 and any(c.isdigit() for c in seg) and any(c.islower() for c in seg) and any(c.isupper() for c in seg):
        return True
    return False


def _sanitize_key_path(path: tuple[str, ...], secrets: set[str]) -> str:
    if not path:
        return "value"
    clean = ["[redacted]" if _is_sensitive_key_segment(seg, secrets) else seg for seg in path]
    return ".".join(clean)


def _find_line_number(lines: list[str], key: str, value: str) -> int:
    # Prefer a line containing both key and value (or key and value prefix).
    val_probe = value[:40]
    for idx, line in enumerate(lines, 1):
        if key and key in line and val_probe and val_probe in line:
            return idx
    for idx, line in enumerate(lines, 1):
        if val_probe and val_probe in line:
            return idx
    for idx, line in enumerate(lines, 1):
        if key and key in line:
            return idx
    return 1


def _scan_structured_config(
    data,
    lines: list[str],
    findings: list[tuple[int, str]],
    secrets: set[str],
    path: tuple[str, ...] = (),
) -> None:
    if isinstance(data, dict):
        for raw_key, val in data.items():
            key = str(raw_key)
            key_shape_hits = _extract_secret_shape_matches(key)
            if key_shape_hits:
                for _, secret in key_shape_hits:
                    secrets.add(secret)
                secrets.add(key)
                line_no = _find_line_number(lines, key, "")
                key_path = _sanitize_key_path(path + (key,), secrets)
                findings.append((line_no, f"{key_shape_hits[0][0]} in {key_path}"))
            elif BEARER_INLINE_RE.search(key):
                tok = BEARER_INLINE_RE.search(key).group(1)
                if _looks_like_real_token(tok):
                    secrets.add(tok)
                    secrets.add(key)
                    line_no = _find_line_number(lines, key, "")
                    key_path = _sanitize_key_path(path + (key,), secrets)
                    findings.append((line_no, f"literal bearer token in {key_path}"))
            _scan_structured_config(val, lines, findings, secrets, path + (key,))
        return
    if isinstance(data, list):
        for index, item in enumerate(data):
            _scan_structured_config(item, lines, findings, secrets, path + (f"[{index}]",))
        return
    if not isinstance(data, str):
        return

    value = data.strip()
    if not value:
        return
    key = path[-1] if path else ""
    parent = path[-2].lower() if len(path) >= 2 else ""

    # 1. Always check known token/key/private-key/basic-auth URL shapes everywhere.
    shape_hits = _extract_secret_shape_matches(value)
    if shape_hits:
        line_no = _find_line_number(lines, key, value)
        kind = shape_hits[0][0]
        for _, secret in shape_hits:
            secrets.add(secret)
        key_path = _sanitize_key_path(path, secrets)
        findings.append((line_no, f"{kind} in {key_path}"))
        return

    # Skip Claude permission allow/deny rules like "Bash(git status:*)" unless shape_hits matched above.
    if CLAUDE_PERM_RULE_RE.match(value):
        return

    # Check if value is a safe placeholder / env expansion.
    if _is_placeholder_secret(value):
        return

    # 2. Structured http_headers / headers (NOT env_http_headers which holds env var names).
    if parent in ("http_headers", "headers") and key.lower() in AUTH_HEADER_NAMES:
        bearer = BEARER_VALUE_RE.match(value)
        if bearer:
            token = bearer.group(1)
            if not _is_placeholder_secret(token):
                secrets.add(token)
                key_path = _sanitize_key_path(path, secrets)
                findings.append((_find_line_number(lines, key, value), f"literal auth header in {key_path}"))
            return
        if len(value) >= 6:
            secrets.add(value)
            key_path = _sanitize_key_path(path, secrets)
            findings.append((_find_line_number(lines, key, value), f"literal auth header in {key_path}"))
        return

    # Skip explicit env-var indirection fields (e.g. bearer_token_env_var, env_http_headers).
    if parent == "env_http_headers" or ENV_INDIRECTION_KEY_RE.search(key):
        return

    # 3. Structured env / environment tables.
    if parent in ("env", "environment"):
        if SECRET_KEY_HINT_RE.search(key):
            if len(value) >= 6 and not any(ch.isspace() for ch in value):
                secrets.add(value)
                key_path = _sanitize_key_path(path, secrets)
                findings.append((_find_line_number(lines, key, value), f"literal secret in {key_path}"))
                return
            bearer = BEARER_VALUE_RE.match(value)
            if bearer and not _is_placeholder_secret(bearer.group(1)):
                secrets.add(bearer.group(1))
                key_path = _sanitize_key_path(path, secrets)
                findings.append((_find_line_number(lines, key, value), f"literal bearer token in {key_path}"))
                return
        if URL_KEY_HINT_RE.search(key):
            for kind, secret in _extract_secret_shape_matches(value):
                secrets.add(secret)
                key_path = _sanitize_key_path(path, secrets)
                findings.append((_find_line_number(lines, key, value), f"{kind} in {key_path}"))
                return

    # 4. Direct sensitive key (e.g. .codex/auth.json {"OPENAI_API_KEY": "..."} or {"bearer_token": "..."}).
    if SECRET_KEY_HINT_RE.search(key):
        bearer = BEARER_VALUE_RE.match(value)
        if bearer and not _is_placeholder_secret(bearer.group(1)):
            secrets.add(bearer.group(1))
            key_path = _sanitize_key_path(path, secrets)
            findings.append((_find_line_number(lines, key, value), f"literal bearer token in {key_path}"))
            return
        if len(value) >= 8 and not any(ch.isspace() for ch in value):
            secrets.add(value)
            key_path = _sanitize_key_path(path, secrets)
            findings.append((_find_line_number(lines, key, value), f"literal credential in {key_path}"))
            return

    # 5. Any Bearer <long-token> inside a structured string value.
    for match in BEARER_INLINE_RE.finditer(value):
        token = match.group(1)
        if not _is_placeholder_secret(token) and _looks_like_real_token(token):
            secrets.add(token)
            key_path = _sanitize_key_path(path, secrets)
            findings.append((_find_line_number(lines, key, value), f"literal bearer token in {key_path}"))
            return


def _looks_like_real_token(token: str) -> bool:
    if len(token) < 12 or _is_placeholder_secret(token):
        return False
    has_digit = any(ch.isdigit() for ch in token)
    has_upper = any(ch.isupper() for ch in token)
    has_lower = any(ch.islower() for ch in token)
    has_sym = any(ch in "_-+./=" for ch in token)
    # Require mixed character classes so plain English hyphenated words do not trigger.
    classes = sum((has_digit, has_upper, has_lower, has_sym))
    return classes >= 2 and (has_digit or len(token) >= 24)


def _scan_raw_agent_file(
    text: str,
    rel_path: str,
    findings: list[tuple[int, str]],
    secrets: set[str],
) -> None:
    is_env_file = Path(rel_path).name.startswith(".env") or rel_path.endswith(".env")
    for line_no, line in enumerate(text.split("\n"), 1):
        shape_hits = _extract_secret_shape_matches(line)
        if shape_hits:
            for _, secret in shape_hits:
                secrets.add(secret)
            findings.append((line_no, shape_hits[0][0]))
            continue
        bearer = BEARER_INLINE_RE.search(line)
        if bearer and _looks_like_real_token(bearer.group(1)):
            secrets.add(bearer.group(1))
            findings.append((line_no, "literal bearer token"))
            continue
        assign = ENV_LINE_ASSIGN_RE.match(line)
        if assign:
            key, raw_val = assign.group(1), assign.group(2).strip()
            if len(raw_val) >= 2 and raw_val[0] == raw_val[-1] and raw_val[0] in ('"', "'"):
                raw_val = raw_val[1:-1]
            if SECRET_KEY_HINT_RE.search(key) and not _is_placeholder_secret(raw_val):
                if is_env_file or (_looks_like_real_token(raw_val) and not any(ch.isspace() for ch in raw_val)):
                    if len(raw_val) >= 6 and not any(ch.isspace() for ch in raw_val):
                        secrets.add(raw_val)
                        findings.append((line_no, f"literal secret in {key}"))


def check_agent_config_secrets(
    tree: Path,
    *,
    base: str | None = None,
    staged: bool = False,
    changed: set[str] | None = None,
) -> dict:
    try:
        prefix = ""
        staged_regular: set[str] = set()
        if staged:
            prefix = _git_prefix(tree)
            staged_regular = _list_staged_regular_files(tree)
            changed_set = changed or set()
            targets = sorted(p for p in changed_set if _is_agent_config_file(p) and p in staged_regular)
        else:
            files = _list_repo_files(tree)
            targets = [p for p in files if _is_agent_config_file(p)]
            if base is not None:
                changed_set = changed or set()
                targets = [p for p in targets if p in changed_set]

        raw_hits: list[tuple[str, int, str]] = []
        secrets: set[str] = set()
        for rel_path in targets:
            if staged:
                text = _read_staged_text_safe(tree, rel_path, prefix=prefix, staged_regular=staged_regular)
            else:
                text = _read_text_safe(tree, rel_path)
            if text is None:
                continue
            lines = text.split("\n")
            findings: list[tuple[int, str]] = []
            suffix = Path(rel_path).suffix.lower()
            parsed = None
            if suffix == ".json":
                try:
                    parsed = json.loads(text)
                except (ValueError, RecursionError):
                    parsed = None
            elif suffix == ".toml":
                parsed = _parse_toml(text)

            if isinstance(parsed, (dict, list)):
                _scan_structured_config(parsed, lines, findings, secrets)
            else:
                _scan_raw_agent_file(text, rel_path, findings, secrets)

            seen: set[tuple[int, str]] = set()
            for line_no, reason in findings:
                if (line_no, reason) not in seen:
                    seen.add((line_no, reason))
                    raw_hits.append((rel_path, line_no, reason))

        hits = [
            f"{_redact_text(path, secrets)}:{line_no}: {_redact_text(reason, secrets)}"
            for path, line_no, reason in raw_hits
        ]
        return _format_result("agent-config-secrets", hits)
    except Exception as error:
        return _format_result("agent-config-secrets", [], reason=repr(error))


# ---------------------------------------------------------------------------
# Check 3: anti-test-weakening (diff check)
# ---------------------------------------------------------------------------

PY_TEST_FILE_RE = re.compile(r"^(?:test_.+\.py|.+_test\.py)$")
JS_TEST_FILE_RE = re.compile(r"^.+\.(?:test|spec)\.[A-Za-z0-9]+$")

HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

PY_TEST_DEF_RE = re.compile(r"^\s*(?:async\s+)?def\s+(test_[A-Za-z0-9_]+)\s*\(")
JS_TEST_DEF_RE = re.compile(r"""^\s*(?:it|test)(?:\.(?:only|each|concurrent|serial))?\s*\(\s*(['"`])(.+?)\1""")
PY_HELPER_DEF_RE = re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")
JS_HELPER_DEF_RE = re.compile(r"^\s*(?:export\s+)?(?:async\s+)?(?:function\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*\(|(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*(?:async\s+)?\()")

PY_UNCOND_SKIP_RE = re.compile(
    r"^\s*(?:@(?:pytest\.mark\.(?:skip|xfail)|unittest\.skip|skip)\b(?!if\b|If\b|Unless\b)|"
    r"pytestmark\s*(?:\s*:[^=]+)?=[^#\n]*\bpytest\.mark\.(?:skip|xfail)\b(?!if\b)|"
    r"pytest\.mark\.(?:skip|xfail)\b(?!if\b)|"
    r"(?:if\s+[^:#]+:\s*)?(?:pytest\.(?:skip|xfail)|self\.skipTest|raise\s+unittest\.SkipTest)\s*\()"
)
PY_COND_SKIP_RE = re.compile(
    r"^\s*(?:@(?:pytest\.mark\.skipif|unittest\.skipIf|unittest\.skipUnless|skipIf|skipUnless)|"
    r"pytestmark\s*(?:\s*:[^=]+)?=[^#\n]*\bpytest\.mark\.skipif|"
    r"pytest\.mark\.skipif)\s*\((.*)$"
)
TRIVIAL_COND_RE = re.compile(r"^\s*(?:True|1|not\s+False|['\"][^'\"]+['\"])\s*(?:,|\))")

JS_SKIP_RE = re.compile(
    r"^\s*(?:(?:test|it|describe|suite|context)\.(?:skip|fixme)|(?:xit|xtest|xdescribe))\s*\("
)
JS_SKIP_NAME_RE = re.compile(
    r"""^\s*(?:(?:test|it)\.(?:skip|fixme)|(?:xit|xtest))\s*\(\s*(['"`])(.+?)\1"""
)

PY_ASSERT_RE = re.compile(
    r"^\s*(?:assert\b|self\.assert[A-Z]\w*\s*\(|(?:with\s+)?pytest\.(?:raises|warns)\s*\()"
)
PY_TAUTOLOGY_RE = re.compile(
    r"^\s*(?:assert\s+(?:True|1)\s*(?:#.*)?$|self\.assertTrue\s*\(\s*True\s*\)\s*(?:#.*)?$)"
)
JS_ASSERT_RE = re.compile(
    r"^\s*(?:await\s+)?(?:expect\s*\(|assert(?:\.[A-Za-z]\w*)?\s*\()"
)
JS_TAUTOLOGY_RE = re.compile(
    r"^\s*expect\s*\(\s*true\s*\)\s*\.\s*toBe\s*\(\s*true\s*\)\s*;?\s*(?://.*)?$"
)

PARAMETRIZE_RE = re.compile(
    r"@pytest\.mark\.parametrize\b|\bself\.subTest\s*\(|\b(?:test|it|describe)\.each\s*\("
)
CALL_NAME_RE = re.compile(r"\b(?:self\.)?([A-Za-z_][A-Za-z0-9_]*)\s*\(")
ASSERT_HELPER_NAME_RE = re.compile(r"^(?:assert_|check_|verify_|expect_)", re.I)


def _is_test_file(rel_path: str) -> bool:
    name = Path(rel_path).name
    return bool(PY_TEST_FILE_RE.match(name) or JS_TEST_FILE_RE.match(name))


def _is_python_test(rel_path: str) -> bool:
    return bool(PY_TEST_FILE_RE.match(Path(rel_path).name))


def _unquote_diff_path(raw: str) -> str:
    text = raw.strip()
    if text.startswith('"') and text.endswith('"') and len(text) >= 2:
        try:
            text = json.loads(text)
        except ValueError:
            text = text[1:-1]
    if text.startswith(("a/", "b/")):
        return text[2:]
    return text


def _parse_unified_diff(diff_text: str) -> list[dict]:
    files: list[dict] = []
    current: dict | None = None
    hunk: dict | None = None
    block: dict | None = None
    old_line = 0
    new_line = 0

    for line in diff_text.split("\n"):
        if line.startswith("diff --git "):
            current = {
                "old_path": "",
                "new_path": "",
                "deleted": False,
                "new_file": False,
                "hunks": [],
            }
            files.append(current)
            hunk = None
            block = None
            continue
        if current is None:
            continue
        if line.startswith("deleted file mode "):
            current["deleted"] = True
            continue
        if line.startswith("new file mode "):
            current["new_file"] = True
            continue
        if line.startswith("rename from "):
            current["old_path"] = _unquote_diff_path(line[len("rename from "):])
            continue
        if line.startswith("rename to "):
            current["new_path"] = _unquote_diff_path(line[len("rename to "):])
            continue
        if line.startswith("--- "):
            raw = line[4:].split("\t", 1)[0]
            if raw != "/dev/null":
                current["old_path"] = _unquote_diff_path(raw)
            continue
        if line.startswith("+++ "):
            raw = line[4:].split("\t", 1)[0]
            if raw != "/dev/null":
                current["new_path"] = _unquote_diff_path(raw)
            continue
        header = HUNK_HEADER_RE.match(line)
        if header:
            old_line = int(header.group(1))
            new_line = int(header.group(3))
            hunk = {"old_start": old_line, "new_start": new_line, "removed": [], "added": [], "blocks": []}
            current["hunks"].append(hunk)
            block = None
            continue
        if hunk is None:
            continue
        if line.startswith("-"):
            if block is None:
                block = {"old_start": old_line, "new_start": new_line, "removed": [], "added": []}
                hunk["blocks"].append(block)
            hunk["removed"].append((old_line, line[1:]))
            block["removed"].append((old_line, line[1:]))
            old_line += 1
        elif line.startswith("+"):
            if block is None:
                block = {"old_start": old_line, "new_start": new_line, "removed": [], "added": []}
                hunk["blocks"].append(block)
            hunk["added"].append((new_line, line[1:]))
            block["added"].append((new_line, line[1:]))
            new_line += 1
        elif line.startswith(" "):
            block = None
            old_line += 1
            new_line += 1
    return files


def _is_comment_line(line: str, is_py: bool) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    if is_py:
        return stripped.startswith("#")
    return stripped.startswith(("//", "/*", "*"))


def _match_test_def(line: str, is_py: bool) -> str | None:
    if is_py:
        m = PY_TEST_DEF_RE.match(line)
        return m.group(1) if m else None
    m = JS_TEST_DEF_RE.match(line)
    return m.group(2) if m else None


def _is_real_assert(line: str, is_py: bool) -> bool:
    if _is_comment_line(line, is_py):
        return False
    if is_py:
        return bool(PY_ASSERT_RE.match(line)) and not bool(PY_TAUTOLOGY_RE.match(line))
    return bool(JS_ASSERT_RE.match(line)) and not bool(JS_TAUTOLOGY_RE.match(line))


JS_SUITE_SKIP_RE = re.compile(r"^\s*(?:(?:describe|suite|context)\.(?:skip|fixme)|xdescribe)\s*\(")
PY_MODULE_SKIP_RE = re.compile(
    r"^\s*(?:pytestmark\s*(?:\s*:[^=]+)?=[^#\n]*\bpytest\.mark\.(?:skip|xfail|skipif)\b|"
    r"pytest\.mark\.(?:skip|xfail|skipif)\b)"
)


def _match_skip_marker(line: str, is_py: bool) -> tuple[str, bool, str] | None:
    """Return (marker_label, is_conditional_nontrivial, scope) or None.

    scope is 'module' for module/suite-wide skips (e.g. pytestmark, describe.skip)
    and 'test' for per-test skips (e.g. @pytest.mark.skip, it.skip).
    """
    if _is_comment_line(line, is_py):
        return None
    if is_py:
        scope = "module" if PY_MODULE_SKIP_RE.match(line) else "test"
        m = PY_UNCOND_SKIP_RE.match(line)
        if m:
            return m.group(0).strip().rstrip("("), False, scope
        cm = PY_COND_SKIP_RE.match(line)
        if cm:
            label = line.strip().split("(", 1)[0]
            is_trivial = bool(TRIVIAL_COND_RE.match(cm.group(1)))
            return label, not is_trivial, scope
        return None
    m = JS_SKIP_RE.match(line)
    if m:
        scope = "module" if JS_SUITE_SKIP_RE.match(line) else "test"
        return m.group(0).strip().rstrip("("), False, scope
    return None


def _collect_added_assert_helpers(test_files: list[dict]) -> set[str]:
    helpers: set[str] = set()
    for entry in test_files:
        path = entry["new_path"] or entry["old_path"]
        is_py = _is_python_test(path)
        for hunk in entry["hunks"]:
            current_helper: str | None = None
            for _, text in hunk["added"]:
                if is_py:
                    m = PY_HELPER_DEF_RE.match(text)
                    if m and not m.group(1).startswith("test_"):
                        current_helper = m.group(1)
                        continue
                    if current_helper and text and not text[0].isspace():
                        current_helper = None
                else:
                    m = JS_HELPER_DEF_RE.match(text)
                    if m:
                        current_helper = m.group(1) or m.group(2)
                        continue
                if current_helper and _is_real_assert(text, is_py):
                    helpers.add(current_helper)
    return helpers


def analyze_test_weakening_diff(diff_text: str) -> list[str]:
    parsed = _parse_unified_diff(diff_text)
    test_files = []
    for entry in parsed:
        path = entry["new_path"] or entry["old_path"]
        if path and _is_test_file(path):
            test_files.append(entry)
    if not test_files:
        return []

    # Global index of added test names across all test files (handles cross-file moves).
    all_added_test_names: set[str] = set()
    for entry in test_files:
        path = entry["new_path"] or entry["old_path"]
        is_py = _is_python_test(path)
        for hunk in entry["hunks"]:
            for _, text in hunk["added"]:
                name = _match_test_def(text, is_py)
                if name:
                    all_added_test_names.add(name)

    added_assert_helpers = _collect_added_assert_helpers(test_files)
    hits: list[str] = []

    for entry in test_files:
        path = entry["new_path"] or entry["old_path"]
        is_py = _is_python_test(path)
        file_added_param = False
        file_added_defs = 0
        file_removed_skips = {"module": 0, "test": 0}
        file_added_skips: list[tuple[int, str, bool, bool, str]] = []
        file_added_assert_texts: list[str] = []

        for hunk in entry["hunks"]:
            removed_def_names = {n for _, t in hunk["removed"] if (n := _match_test_def(t, is_py))}
            added_lines = hunk["added"]
            for _, text in hunk["removed"]:
                removed_skip = _match_skip_marker(text, is_py)
                if removed_skip is not None:
                    file_removed_skips[removed_skip[2]] += 1
            for idx, (line_no, text) in enumerate(added_lines):
                if PARAMETRIZE_RE.search(text):
                    file_added_param = True
                if _match_test_def(text, is_py):
                    file_added_defs += 1
                if _is_real_assert(text, is_py):
                    file_added_assert_texts.append(text.strip())
                skip = _match_skip_marker(text, is_py)
                if skip is not None:
                    marker, is_cond_nontrivial, scope = skip
                    # Decorators on a brand-new test or module skipif on a brand-new file may be legitimate guards.
                    if text.lstrip().startswith("@"):
                        decorates_new_test = False
                        for _, next_text in added_lines[idx + 1:idx + 4]:
                            next_def = _match_test_def(next_text, is_py)
                            if next_def:
                                decorates_new_test = next_def not in removed_def_names
                                break
                            if next_text.strip() and not next_text.strip().startswith("@"):
                                break
                    else:
                        decorates_new_test = bool(entry["new_file"])
                    file_added_skips.append((line_no, marker, is_cond_nontrivial, decorates_new_test, scope))

        # 1. Newly added skip/xfail markers (scope-aware: a removed per-test skip never cancels a module/suite skip).
        module_budget = file_removed_skips["module"]
        test_budget = file_removed_skips["test"]
        # Match module/suite skips first so removed per-test skips cannot cancel them.
        for line_no, marker, is_cond_nontrivial, decorates_new_test, scope in file_added_skips:
            if scope != "module":
                continue
            if module_budget > 0:
                module_budget -= 1
                continue
            if is_cond_nontrivial and decorates_new_test:
                continue
            hits.append(f"{path}:{line_no}: added skip/xfail ({marker})")
        for line_no, marker, is_cond_nontrivial, decorates_new_test, scope in file_added_skips:
            if scope != "test":
                continue
            if test_budget > 0:
                test_budget -= 1
                continue
            if module_budget > 0:
                module_budget -= 1
                continue
            if is_cond_nontrivial and decorates_new_test:
                continue
            hits.append(f"{path}:{line_no}: added skip/xfail ({marker})")

        # Remaining pool of added assertion texts for exact-move matching within the file.
        unmatched_added_assert_texts = list(file_added_assert_texts)

        # 2 & 3. Per-change-block deleted tests & deleted assertions without replacement.
        for hunk in entry["hunks"]:
            for block in hunk["blocks"]:
                removed_defs = [(ln, name) for ln, text in block["removed"] if (name := _match_test_def(text, is_py))]
                added_defs = [(ln, name) for ln, text in block["added"] if (name := _match_test_def(text, is_py))]
                skipped_in_block = set()
                if not is_py:
                    for _, text in block["added"]:
                        sm = JS_SKIP_NAME_RE.match(text)
                        if sm:
                            skipped_in_block.add(sm.group(2))
                unmatched_removed_defs = [
                    (ln, name) for ln, name in removed_defs
                    if name not in all_added_test_names and name not in skipped_in_block
                ]

                first_deleted_def_line: int | None = None
                if unmatched_removed_defs:
                    is_inplace_rename = len(added_defs) >= len(unmatched_removed_defs)
                    is_parametrized_consolidation = file_added_param and file_added_defs > 0
                    if not is_inplace_rename and not is_parametrized_consolidation:
                        excess = len(unmatched_removed_defs) - len(added_defs)
                        for ln, name in unmatched_removed_defs[:excess]:
                            report_line = block["new_start"] if not entry["deleted"] else ln
                            hits.append(f"{path}:{report_line}: deleted test `{name}`")
                            if first_deleted_def_line is None or ln < first_deleted_def_line:
                                first_deleted_def_line = ln

                if entry["deleted"]:
                    continue

                # Only count removed assertions that do not belong to a deleted test body in this block.
                removed_asserts = [
                    (ln, text.strip()) for ln, text in block["removed"]
                    if _is_real_assert(text, is_py) and (first_deleted_def_line is None or ln < first_deleted_def_line)
                ]
                added_asserts = [(ln, text.strip()) for ln, text in block["added"] if _is_real_assert(text, is_py)]
                if not removed_asserts or added_asserts:
                    continue

                # Check whether the block calls an assertion helper (newly extracted or assert_*/check_*/verify_*).
                calls_helper = False
                for _, text in block["added"]:
                    if _is_comment_line(text, is_py):
                        continue
                    for call_name in CALL_NAME_RE.findall(text):
                        if call_name in added_assert_helpers or ASSERT_HELPER_NAME_RE.match(call_name):
                            calls_helper = True
                            break
                    if calls_helper:
                        break
                if calls_helper:
                    continue

                # Check whether every removed assertion was moved verbatim elsewhere in the same file.
                unmoved = 0
                for _, stripped in removed_asserts:
                    if stripped in unmatched_added_assert_texts:
                        unmatched_added_assert_texts.remove(stripped)
                    else:
                        unmoved += 1
                if unmoved > 0:
                    report_line = block["added"][0][0] if block["added"] else block["new_start"]
                    hits.append(f"{path}:{report_line}: removed {unmoved} assertion(s) without replacement")

    return _sort_hits(hits)


def check_anti_test_weakening(
    tree: Path,
    *,
    base: str | None = None,
    staged: bool = False,
) -> dict:
    try:
        if staged:
            diff_args = ["diff", "--cached", "-M", "--relative", "--no-ext-diff", "-U3", "--"]
        else:
            ref = "HEAD" if base is None else base
            if ref.startswith("-"):
                return _format_result("anti-test-weakening", [], reason="invalid diff base revision")
            diff_args = ["diff", "-M", "--relative", "--no-ext-diff", "-U3", ref, "--"]
        result = _git(tree, *diff_args)
        if result.returncode != 0:
            reason = result.stderr.strip() or f"git diff exited {result.returncode}"
            return _format_result("anti-test-weakening", [], reason=reason)
        hits = analyze_test_weakening_diff(result.stdout)
        return _format_result("anti-test-weakening", hits)
    except Exception as error:
        return _format_result("anti-test-weakening", [], reason=repr(error))


# ---------------------------------------------------------------------------
# Entry point called by coldfix.check_library
# ---------------------------------------------------------------------------

def run_checks(
    tree: Path,
    *,
    base: str | None = None,
    staged: bool = False,
    changed: set[str] | None = None,
    only: set[str] | None = None,
) -> list[dict]:
    diff_mode = base is not None or staged
    selected: list[str] = []
    for check_id in CHECK_IDS:
        if only is not None and check_id not in only:
            continue
        if CHECKS[check_id]["diff_only"] and not diff_mode and (only is None or check_id not in only):
            continue
        selected.append(check_id)

    results: list[dict] = []
    for check_id in selected:
        if check_id == "agents-md-drift":
            results.append(check_agents_md_drift(tree, base=base, staged=staged, changed=changed))
        elif check_id == "agent-config-secrets":
            results.append(check_agent_config_secrets(tree, base=base, staged=staged, changed=changed))
        elif check_id == "anti-test-weakening":
            results.append(check_anti_test_weakening(tree, base=base, staged=staged))
    return results
