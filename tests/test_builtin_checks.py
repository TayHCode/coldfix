"""Tests for built-in checks: agents-md-drift, agent-config-secrets, and anti-test-weakening."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import builtin_checks


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "coldfix.py"
GIT = ["git", "-c", "core.fsmonitor=false", "-c", "user.name=t", "-c", "user.email=t", "-c", "commit.gpgsign=false"]


class BuiltinCheckCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tree = Path(temporary.name) / "repo"
        self.tree.mkdir()

    def write(self, files):
        for rel, content in files.items():
            target = self.tree / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

    def git_init_commit(self, message="init"):
        subprocess.run(GIT + ["init", "-q"], cwd=self.tree, check=True)
        subprocess.run(GIT + ["add", "-A"], cwd=self.tree, check=True)
        subprocess.run(GIT + ["commit", "-qm", message], cwd=self.tree, check=True)

    def run_check(self, *args):
        result = subprocess.run(
            [sys.executable, str(CLI), "check", *args],
            cwd=self.tree,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertNotIn("Traceback", result.stderr)
        return result

    def check_rows(self, *args):
        result = self.run_check("--json", *args)
        payload = json.loads(result.stdout)
        self.assertEqual((payload["schema_version"], payload["exit_code"]), (1, result.returncode))
        return result.returncode, {row["lesson"]: row for row in payload["lessons"]}


class AgentsMdDriftTests(BuiltinCheckCase):
    def test_stale_paths_and_commands_fail_and_fixes_pass(self):
        self.write({
            "src/real.py": "VALUE = 1\n",
            "scripts/ok.sh": "#!/bin/sh\necho ok\n",
            "Makefile": ".PHONY: test lint\ntest:\n\techo test\nlint:\n\techo lint\n",
            "package.json": json.dumps({"scripts": {"build": "tsc", "test": "vitest"}}),
            "pyproject.toml": "[project]\nname = 'demo'\n[project.scripts]\nreal-cli = 'src.real:main'\n",
            "AGENTS.md": (
                "# Repo guide\n\n"
                "Edit `src/foo.py` and `scripts/check.sh` or `docs/legacy/`.\n"
                "Run `make old-target` and `npm run missing-script`.\n"
                "Also run `poetry run old-cli` and `bash scripts/gone.sh`.\n"
                "```sh\n"
                "./scripts/also_gone.sh\n"
                "pnpm run nope\n"
                "```\n"
            ),
            "CLAUDE.md": "Check `src/missing_claude.py:12`.\n",
            ".cursorrules": "Use `tools/deleted_tool.py`.\n",
            ".cursor/rules/workflow.mdc": "Always run `python3 scripts/missing_rule.py`.\n",
        })
        code, rows = self.check_rows("--only", "agents-md-drift")
        row = rows["agents-md-drift"]
        self.assertEqual(code, 1)
        self.assertEqual(row["status"], "FAIL")
        joined = "\n".join(row["hits"])
        for expected in (
            "AGENTS.md:3: missing path `src/foo.py`",
            "AGENTS.md:3: missing path `scripts/check.sh`",
            "AGENTS.md:3: missing path `docs/legacy/`",
            "AGENTS.md:4: missing make target `old-target`",
            "AGENTS.md:4: missing npm script `missing-script`",
            "AGENTS.md:5: missing pyproject script `old-cli`",
            "AGENTS.md:5: missing script `scripts/gone.sh`",
            "AGENTS.md:7: missing script `./scripts/also_gone.sh`",
            "AGENTS.md:8: missing npm script `nope`",
            "CLAUDE.md:1: missing path `src/missing_claude.py:12`",
            ".cursorrules:1: missing path `tools/deleted_tool.py`",
            ".cursor/rules/workflow.mdc:1: missing script `scripts/missing_rule.py`",
        ):
            self.assertIn(expected, joined)

        # After fixing all references to real repo paths and commands -> PASS
        self.write({
            "AGENTS.md": (
                "# Repo guide\n\n"
                "Edit `src/real.py` and `scripts/ok.sh`.\n"
                "Run `make test` and `npm run build`.\n"
                "Also run `poetry run real-cli` and `bash scripts/ok.sh`.\n"
                "```sh\n"
                "./scripts/ok.sh\n"
                "pnpm run test\n"
                "```\n"
            ),
            "CLAUDE.md": "Check `src/real.py:1`.\n",
            ".cursorrules": "Use `src/real.py`.\n",
            ".cursor/rules/workflow.mdc": "Always run `bash scripts/ok.sh`.\n",
        })
        code, rows = self.check_rows("--only", "agents-md-drift")
        self.assertEqual((code, rows["agents-md-drift"]["status"], rows["agents-md-drift"]["hits"]), (0, "PASS", []))

    def test_negative_controls_avoid_false_positives(self):
        self.write({
            "packages/web/src/index.ts": "export const x = 1;\n",
            "packages/web/AGENTS.md": "Local entry is `src/index.ts`.\n",
            "Makefile": "test:\n\techo ok\n",
            "package.json": json.dumps({"scripts": {"test": "vitest"}}),
            "pyproject.toml": (
                "[project]\nname = 'demo'\ndependencies = ['httpx>=0.27']\n"
                "[project.scripts]\ndemo = 'demo:main'\n"
            ),
            "AGENTS.md": (
                "# Negative controls\n\n"
                "- URLs: `https://example.invalid/src/foo.py`, `http://localhost:8080/api/v1`, `mailto:?subject=setup`\n"
                "- Globs: `src/**/*.py`, `tests/test_*.py`, `docs/*.md`\n"
                "- Placeholders: `<path>`, `path/to/file.py`, `foo/bar`, `owner/repo`, `before/after`, `read/write`, `pass/fail`\n"
                "- Flags and env: `--diff`, `-p`, `$HOME/bin/tool`, `~/scripts/run.sh`, `/usr/local/bin/tool`\n"
                "- Scoped packages and MIME: `@scope/pkg`, `application/json`, `text/plain`\n"
                "- Runtime/build dirs: `dist/bundle.js`, `.venv/bin/pytest`, `node_modules/.bin/tsc`, `.coldfix/config.json`\n"
                "- Bare non-paths and placeholders: `os.path`, `node.js`, `res.json`, `foo.py`, `example.md`, `.env`, `pytest`\n"
                "- Standard tools & deps: `uv run pytest`, `poetry run ruff`, `poetry run httpx`, `make <target>`, `npm run <script>`\n"
                "- Non-shell code fence is ignored:\n"
                "```python\n"
                "path = 'src/does_not_exist.py'\n"
                "```\n"
            ),
        })
        code, rows = self.check_rows("--only", "agents-md-drift")
        self.assertEqual((code, rows["agents-md-drift"]["status"], rows["agents-md-drift"]["hits"]), (0, "PASS", []))

    def test_diff_mode_catches_newly_broken_ref_when_target_deleted_and_ignores_unrelated_debt(self):
        self.write({
            "src/keep.py": "x = 1\n",
            "src/delete_me.py": "y = 2\n",
            "AGENTS.md": "Old debt: `src/preexisting_missing.py`.\nReal file: `src/delete_me.py`.\n",
        })
        self.git_init_commit()

        # Unrelated change should NOT report preexisting debt in untouched AGENTS.md.
        self.write({"src/keep.py": "x = 2\n"})
        code, rows = self.check_rows("--diff", "--only", "agents-md-drift")
        self.assertEqual((code, rows["agents-md-drift"]["status"], rows["agents-md-drift"]["hits"]), (0, "PASS", []))

        # Deleting src/delete_me.py WITHOUT editing AGENTS.md MUST flag the newly broken reference!
        (self.tree / "src/delete_me.py").unlink()
        code, rows = self.check_rows("--diff", "--only", "agents-md-drift")
        self.assertEqual(code, 1)
        self.assertEqual(rows["agents-md-drift"]["hits"], ["AGENTS.md:2: missing path `src/delete_me.py`"])

    def test_symlinked_instruction_file_is_not_followed(self):
        outside = self.tree.parent / "outside_agents.md"
        outside.write_text("Missing `src/never_here.py`.\n", encoding="utf-8")
        (self.tree / "AGENTS.md").symlink_to(outside)
        code, rows = self.check_rows("--only", "agents-md-drift")
        self.assertEqual((code, rows["agents-md-drift"]["status"], rows["agents-md-drift"]["hits"]), (0, "PASS", []))

    def test_root_level_files_and_bare_runner_scripts_are_checked(self):
        self.write({
            "AGENTS.md": (
                "Read `CONTRIBUTING.md`, `verify.py`, and `run.sh`.\n"
                "Run `python3 verify.py`, `bash check.sh`, `node build.js`, and `uv run runner.py`.\n"
                "Ignore `os.path`, `node.js`, `foo.py`, `.env`, `res.json`, and `pytest`.\n"
            ),
        })
        code, rows = self.check_rows("--only", "agents-md-drift")
        self.assertEqual(code, 1)
        joined = "\n".join(rows["agents-md-drift"]["hits"])
        for expected in (
            "AGENTS.md:1: missing path `CONTRIBUTING.md`",
            "AGENTS.md:1: missing path `verify.py`",
            "AGENTS.md:1: missing path `run.sh`",
            "AGENTS.md:2: missing script `verify.py`",
            "AGENTS.md:2: missing script `check.sh`",
            "AGENTS.md:2: missing script `build.js`",
            "AGENTS.md:2: missing script `runner.py`",
        ):
            self.assertIn(expected, joined)
        for excluded in ("os.path", "node.js", "foo.py", ".env", "res.json", "pytest"):
            self.assertNotIn(excluded, joined)

        self.write({
            "CONTRIBUTING.md": "# Contributing\n",
            "verify.py": "print('ok')\n",
            "run.sh": "#!/bin/sh\necho ok\n",
            "check.sh": "#!/bin/sh\necho ok\n",
            "build.js": "console.log('ok');\n",
            "runner.py": "print('ok')\n",
        })
        code, rows = self.check_rows("--only", "agents-md-drift")
        self.assertEqual((code, rows["agents-md-drift"]["status"], rows["agents-md-drift"]["hits"]), (0, "PASS", []))

    def test_staged_drift_reads_git_index_not_sanitized_working_copy(self):
        self.write({"README.md": "ok\n"})
        self.git_init_commit()
        self.write({"AGENTS.md": "Check `missing_root.py`.\n"})
        subprocess.run(GIT + ["add", "AGENTS.md"], cwd=self.tree, check=True)
        # Fix working copy without restaging: --staged must still catch the staged drift.
        self.write({"AGENTS.md": "Check `README.md`.\n"})
        code, rows = self.check_rows("--staged", "--only", "agents-md-drift")
        self.assertEqual(code, 1)
        self.assertEqual(rows["agents-md-drift"]["hits"], ["AGENTS.md:1: missing path `missing_root.py`"])
        subprocess.run(GIT + ["add", "AGENTS.md"], cwd=self.tree, check=True)
        code, rows = self.check_rows("--staged", "--only", "agents-md-drift")
        self.assertEqual((code, rows["agents-md-drift"]["status"], rows["agents-md-drift"]["hits"]), (0, "PASS", []))


class AgentConfigSecretsTests(BuiltinCheckCase):
    def test_detects_credentials_in_claude_codex_and_cursor_configs_without_leaking_values(self):
        openai_val = "sk-" + "proj-" + "Ab12Cd34Ef56Gh78Ij90Kl12"
        gh_val = "ghp_" + "Q1w2E3r4" * 5
        env_val = "live_" + "9f8e7d6c5b4a3210"
        bearer_val = "tok_" + "XyZ9876543210AbCdEf"
        basic_val = "p4ss" + "W0rd99"
        header_val = "rk_" + "1122334455667788"

        claude_settings = {
            "permissions": {"allow": ["Bash(git status:*)", "Read(src/**)"]},
            "env": {
                "ANTHROPIC_API_KEY": openai_val,
                "STRIPE_SECRET_KEY": env_val,
                "SAFE_ENV": "${SAFE_ENV:-}",
            },
        }
        codex_toml = (
            'model = "gpt-5"\n'
            '[mcp_servers.crm]\n'
            'url = "https://svc:' + basic_val + '@' + 'api.example.invalid/mcp"\n'
            '[mcp_servers.crm.http_headers]\n'
            'Authorization = "Bearer ' + bearer_val + '"\n'
            'X-Api-Key = "' + header_val + '"\n'
            'Content-Type = "application/json"\n'
        )
        cursor_mcp = {
            "mcpServers": {
                "gh": {
                    "command": "npx",
                    "env": {"GITHUB_TOKEN": gh_val},
                }
            }
        }
        self.write({
            ".claude/settings.json": json.dumps(claude_settings, indent=2) + "\n",
            ".codex/config.toml": codex_toml,
            ".cursor/mcp.json": json.dumps(cursor_mcp, indent=2) + "\n",
        })

        code, rows = self.check_rows("--only", "agent-config-secrets")
        row = rows["agent-config-secrets"]
        self.assertEqual(code, 1)
        self.assertEqual(row["status"], "FAIL")
        rendered = "\n".join(row["hits"])
        self.assertIn(".claude/settings.json:", rendered)
        self.assertIn("env.ANTHROPIC_API_KEY", rendered)
        self.assertIn("env.STRIPE_SECRET_KEY", rendered)
        self.assertIn(".codex/config.toml:", rendered)
        self.assertIn("mcp_servers.crm.url", rendered)
        self.assertIn("mcp_servers.crm.http_headers.Authorization", rendered)
        self.assertIn("mcp_servers.crm.http_headers.X-Api-Key", rendered)
        self.assertIn(".cursor/mcp.json:", rendered)
        self.assertIn("mcpServers.gh.env.GITHUB_TOKEN", rendered)

        # Ensure raw secret values are never printed in hits.
        for val in (openai_val, gh_val, env_val, bearer_val, basic_val, header_val):
            self.assertNotIn(val, rendered)

        # Clean replacement -> PASS (before -> after proof).
        self.write({
            ".claude/settings.json": json.dumps({
                "permissions": {"allow": ["Bash(git status:*)"]},
                "env": {"ANTHROPIC_API_KEY": "${ANTHROPIC_API_KEY:-}", "STRIPE_SECRET_KEY": "$STRIPE_SECRET_KEY"},
            }, indent=2) + "\n",
            ".codex/config.toml": (
                'model = "gpt-5"\n'
                '[mcp_servers.crm]\n'
                'url = "https://svc:' + '${CRM_PASS}' + '@' + 'api.example.invalid/mcp"\n'
                'bearer_token_env_var = "CRM_TOKEN"\n'
                '[mcp_servers.crm.env_http_headers]\n'
                'Authorization = "CRM_AUTH_HEADER"\n'
                '[mcp_servers.crm.http_headers]\n'
                'Authorization = "Bearer ${CRM_TOKEN}"\n'
                'Content-Type = "application/json"\n'
            ),
            ".cursor/mcp.json": json.dumps({
                "mcpServers": {"gh": {"command": "npx", "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN:-}"}}}
            }, indent=2) + "\n",
        })
        code, rows = self.check_rows("--only", "agent-config-secrets")
        self.assertEqual((code, rows["agent-config-secrets"]["status"], rows["agent-config-secrets"]["hits"]), (0, "PASS", []))

    def test_negative_controls_for_permissions_placeholders_prose_and_gitignored_files(self):
        ignored_val = "ghp_" + "A9b8C7d6" * 5
        self.write({
            ".gitignore": ".claude/settings.local.json\n",
            ".claude/settings.json": json.dumps({
                "permissions": {
                    "allow": ["Bash(git status:*)", "Bash(pytest:*)", "Read(src/**)"],
                    "deny": ["Bash(rm -rf:*)"],
                },
                "env": {
                    "OPENAI_API_KEY": "${OPENAI_API_KEY:-}",
                    "API_TOKEN": "${API_TOKEN}",
                    "SECRET_NAME": "<your-secret>",
                    "DEBUG_MODE": "true",
                },
            }, indent=2) + "\n",
            ".claude/settings.local.json": json.dumps({"env": {"GITHUB_TOKEN": ignored_val}}) + "\n",
            ".codex/config.toml": (
                'bearer_token_env_var = "CODEX_API_TOKEN"\n'
                '[mcp_servers.docs.env_http_headers]\n'
                'Authorization = "DOCS_AUTH_TOKEN"\n'
            ),
            ".cursor/rules/security.mdc": (
                "# Security rules\n\n"
                "Never commit an API key, password, or Bearer token.\n"
                "Always read `${OPENAI_API_KEY:-}` from the environment instead of hardcoding secrets.\n"
                "Do not pass `Authorization: Bearer <token>` literally in config files.\n"
            ),
        })
        self.git_init_commit()
        code, rows = self.check_rows("--only", "agent-config-secrets")
        self.assertEqual((code, rows["agent-config-secrets"]["status"], rows["agent-config-secrets"]["hits"]), (0, "PASS", []))

    def test_python_310_toml_fallback_parses_tables_and_inline_headers(self):
        fallback_val = "tok_" + "Fallback9876543210"
        self.write({
            ".codex/config.toml": (
                '[mcp_servers.api]\n'
                'http_headers = { Authorization = "Bearer ' + fallback_val + '", "Content-Type" = "application/json" }\n'
            ),
        })
        with mock.patch.object(builtin_checks, "tomllib", None):
            result = builtin_checks.check_agent_config_secrets(self.tree)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["hits"], [".codex/config.toml:2: literal auth header in mcp_servers.api.http_headers.Authorization"])
        self.assertNotIn(fallback_val, "\n".join(result["hits"]))

    def test_filename_containing_secret_is_redacted(self):
        tok = "ghp_" + "R3d4C5t6" * 5
        self.write({f".codex/{tok}.json": json.dumps({"token": tok}) + "\n"})
        code, rows = self.check_rows("--only", "agent-config-secrets")
        self.assertEqual(code, 1)
        joined = "\n".join(rows["agent-config-secrets"]["hits"])
        self.assertIn(".codex/[redacted].json:", joined)
        self.assertNotIn(tok, joined)

    def test_secret_shaped_json_and_toml_key_names_are_redacted(self):
        tok_key = "sk-" + "proj-" + "KeyLeak1234567890AbCdEf"
        gh_val = "ghp_" + "M1n2O3p4" * 5
        self.write({
            ".claude/settings.json": json.dumps({"env": {tok_key: gh_val}}) + "\n",
            ".codex/config.toml": '[mcp_servers."' + tok_key + '"]\nurl = "https://example.invalid"\n',
        })
        result = self.run_check("--json", "--only", "agent-config-secrets")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(tok_key, result.stdout + result.stderr)
        self.assertNotIn(gh_val, result.stdout + result.stderr)
        rows = {row["lesson"]: row for row in json.loads(result.stdout)["lessons"]}
        joined = "\n".join(rows["agent-config-secrets"]["hits"])
        self.assertIn("env.[redacted]", joined)
        self.assertIn("mcp_servers.[redacted]", joined)

    def test_staged_secret_still_fails_after_working_copy_is_sanitized_without_restaging(self):
        staged_val = "ghp_" + "S7a8G9e0" * 5
        self.write({".claude/settings.json": json.dumps({"env": {"API_KEY": "${API_KEY:-}"}}) + "\n"})
        self.git_init_commit()
        self.write({".claude/settings.json": json.dumps({"env": {"GITHUB_TOKEN": staged_val}}) + "\n"})
        subprocess.run(GIT + ["add", ".claude/settings.json"], cwd=self.tree, check=True)
        # Sanitize working copy WITHOUT restaging: --staged must still inspect the Git index!
        self.write({".claude/settings.json": json.dumps({"env": {"GITHUB_TOKEN": "${GITHUB_TOKEN:-}"}}) + "\n"})
        code, rows = self.check_rows("--staged")
        self.assertEqual(code, 1)
        self.assertEqual(rows["agent-config-secrets"]["status"], "FAIL")
        self.assertEqual(rows["secrets-and-private-data"]["status"], "FAIL")
        self.assertNotIn(staged_val, "\n".join(rows["agent-config-secrets"]["hits"]))
        # After restaging the sanitized file, --staged passes cleanly.
        subprocess.run(GIT + ["add", ".claude/settings.json"], cwd=self.tree, check=True)
        code, rows = self.check_rows("--staged")
        self.assertEqual((code, rows["agent-config-secrets"]["status"], rows["secrets-and-private-data"]["status"]), (0, "PASS", "PASS"))


class AntiTestWeakeningTests(BuiltinCheckCase):
    def test_flags_added_skips_deleted_assertions_and_deleted_tests_then_passes_when_restored(self):
        original_py = (
            "import pytest\n"
            "import unittest\n\n"
            "def test_add():\n"
            "    assert 1 + 1 == 2\n\n"
            "def test_sub():\n"
            "    assert 5 - 3 == 2\n\n"
            "def test_removed_case():\n"
            "    assert 3 * 3 == 9\n\n"
            "class Suite(unittest.TestCase):\n"
            "    def test_eq(self):\n"
            "        self.assertEqual(10, 10)\n"
        )
        original_ts = (
            "import { describe, it, expect } from 'vitest';\n\n"
            "describe('math', () => {\n"
            "  it('adds', () => {\n"
            "    expect(1 + 1).toBe(2);\n"
            "  });\n"
            "  it('multiplies', () => {\n"
            "    expect(2 * 3).toBe(6);\n"
            "  });\n"
            "});\n"
        )
        self.write({"tests/test_math.py": original_py, "src/math.spec.ts": original_ts})
        self.git_init_commit()

        # Weaken tests: add @pytest.mark.skip, remove assertion in test_sub, delete test_removed_case,
        # add @unittest.skip, change it('adds') to it.skip('adds'), drop expect() in multiplies.
        weakened_py = (
            "import pytest\n"
            "import unittest\n\n"
            "@pytest.mark.skip(reason='flaky')\n"
            "def test_add():\n"
            "    assert 1 + 1 == 2\n\n"
            "def test_sub():\n"
            "    _ = 5 - 3\n\n"
            "class Suite(unittest.TestCase):\n"
            "    @unittest.skip('later')\n"
            "    def test_eq(self):\n"
            "        self.assertEqual(10, 10)\n"
        )
        weakened_ts = (
            "import { describe, it, expect } from 'vitest';\n\n"
            "describe('math', () => {\n"
            "  it.skip('adds', () => {\n"
            "    expect(1 + 1).toBe(2);\n"
            "  });\n"
            "  it('multiplies', () => {\n"
            "    const v = 2 * 3;\n"
            "  });\n"
            "});\n"
        )
        self.write({"tests/test_math.py": weakened_py, "src/math.spec.ts": weakened_ts})

        code, rows = self.check_rows("--diff", "--only", "anti-test-weakening")
        row = rows["anti-test-weakening"]
        self.assertEqual(code, 1)
        self.assertEqual(row["status"], "FAIL")
        joined = "\n".join(row["hits"])
        self.assertIn("tests/test_math.py:4: added skip/xfail (@pytest.mark.skip)", joined)
        self.assertIn("tests/test_math.py:12: added skip/xfail (@unittest.skip)", joined)
        self.assertIn("tests/test_math.py:9: removed 1 assertion(s) without replacement", joined)
        self.assertIn("deleted test `test_removed_case`", joined)
        self.assertIn("src/math.spec.ts:4: added skip/xfail (it.skip)", joined)
        self.assertNotIn("deleted test `adds`", joined)
        self.assertIn("src/math.spec.ts:8: removed 1 assertion(s) without replacement", joined)

        # Also verify --staged detects staged weakening cleanly!
        subprocess.run(GIT + ["add", "tests/test_math.py"], cwd=self.tree, check=True)
        staged_code, staged_rows = self.check_rows("--staged", "--only", "anti-test-weakening")
        self.assertEqual(staged_code, 1)
        self.assertTrue(all(h.startswith("tests/test_math.py:") for h in staged_rows["anti-test-weakening"]["hits"]))

        # Restore genuine tests -> PASS (before -> after proof).
        self.write({"tests/test_math.py": original_py, "src/math.spec.ts": original_ts})
        subprocess.run(GIT + ["reset", "-q"], cwd=self.tree, check=True)
        code, rows = self.check_rows("--diff", "--only", "anti-test-weakening")
        self.assertEqual((code, rows["anti-test-weakening"]["status"], rows["anti-test-weakening"]["hits"]), (0, "PASS", []))

    def test_tautological_assert_true_does_not_count_as_replacement(self):
        self.write({"tests/test_taut.py": "def test_val():\n    assert compute() == 42\n"})
        self.git_init_commit()
        self.write({"tests/test_taut.py": "def test_val():\n    assert True\n"})
        code, rows = self.check_rows("--diff", "--only", "anti-test-weakening")
        self.assertEqual(code, 1)
        self.assertIn("removed 1 assertion(s) without replacement", rows["anti-test-weakening"]["hits"][0])

    def test_module_wide_pytestmark_and_inline_skip_calls_are_flagged(self):
        self.write({
            "tests/test_mod.py": (
                "import pytest\n"
                "import unittest\n\n"
                "def test_one():\n"
                "    assert 1 == 1\n\n"
                "class C(unittest.TestCase):\n"
                "    def test_two(self):\n"
                "        self.assertEqual(2, 2)\n"
            ),
            "tests/test_list_mark.py": "import pytest\n\ndef test_a():\n    assert 1 == 1\n",
        })
        self.git_init_commit()
        self.write({
            "tests/test_mod.py": (
                "import pytest\n"
                "import unittest\n\n"
                "pytestmark = pytest.mark.skip(reason='broken')\n\n"
                "def test_one():\n"
                "    pytest.skip('skipping')\n"
                "    assert 1 == 1\n\n"
                "class C(unittest.TestCase):\n"
                "    def test_two(self):\n"
                "        self.skipTest('later')\n"
                "        self.assertEqual(2, 2)\n"
            ),
            "tests/test_list_mark.py": (
                "import pytest\n\n"
                "pytestmark = [\n"
                "    pytest.mark.xfail(reason='temporarily failing'),\n"
                "]\n\n"
                "def test_a():\n"
                "    assert 1 == 1\n"
            ),
        })
        code, rows = self.check_rows("--diff", "--only", "anti-test-weakening")
        self.assertEqual(code, 1)
        joined = "\n".join(rows["anti-test-weakening"]["hits"])
        self.assertIn("tests/test_mod.py:4: added skip/xfail", joined)
        self.assertIn("tests/test_mod.py:7: added skip/xfail (pytest.skip)", joined)
        self.assertIn("tests/test_mod.py:12: added skip/xfail (self.skipTest)", joined)
        self.assertIn("tests/test_list_mark.py:4: added skip/xfail (pytest.mark.xfail)", joined)

    def test_removed_per_test_skip_cannot_cancel_added_module_wide_pytestmark_skip(self):
        self.write({
            "tests/test_scope.py": (
                "import pytest\n\n"
                "@pytest.mark.skip(reason='known bug')\n"
                "def test_first():\n"
                "    assert 1 == 1\n\n"
                "def test_second():\n"
                "    assert 2 == 2\n"
            ),
            "tests/test_existing_modskip.py": (
                "import pytest\n\n"
                "pytestmark = pytest.mark.skip(reason='old ticket')\n\n"
                "def test_a():\n"
                "    assert 1 == 1\n"
            ),
        })
        self.git_init_commit()

        # Replacing 1 per-test skip with module-wide pytestmark skips BOTH tests -> MUST FAIL.
        # Updating reason on an already-present module-wide pytestmark -> PASS.
        self.write({
            "tests/test_scope.py": (
                "import pytest\n\n"
                "pytestmark = pytest.mark.skip(reason='skip whole module')\n\n"
                "def test_first():\n"
                "    assert 1 == 1\n\n"
                "def test_second():\n"
                "    assert 2 == 2\n"
            ),
            "tests/test_existing_modskip.py": (
                "import pytest\n\n"
                "pytestmark = pytest.mark.skip(reason='new ticket')\n\n"
                "def test_a():\n"
                "    assert 1 == 1\n"
            ),
        })
        code, rows = self.check_rows("--diff", "--only", "anti-test-weakening")
        self.assertEqual(code, 1)
        hits = rows["anti-test-weakening"]["hits"]
        self.assertEqual(len(hits), 1)
        self.assertIn("tests/test_scope.py:3: added skip/xfail", hits[0])

    def test_normal_test_refactors_do_not_false_positive(self):
        self.write({
            "app.py": "def calc(x):\n    assert x > 0\n    return x * 2\n",
            "tests/test_a.py": (
                "import pytest\n"
                "import unittest\n\n"
                "def test_old_name():\n"
                "    assert 1 + 1 == 2\n"
                "    assert 2 + 2 == 4\n\n"
                "def test_move_me():\n"
                "    assert 'ok' == 'ok'\n\n"
                "def test_extract_helper():\n"
                "    res = {'ok': True, 'code': 200}\n"
                "    assert res['ok'] is True\n"
                "    assert res['code'] == 200\n\n"
                "def test_case_one():\n"
                "    assert 1 == 1\n\n"
                "def test_case_two():\n"
                "    assert 2 == 2\n\n"
                "@pytest.mark.skip(reason='old reason')\n"
                "def test_already_skipped():\n"
                "    assert True\n"
            ),
            "tests/test_b.py": "def test_existing():\n    assert 1 == 1\n",
        })
        self.git_init_commit()

        # Perform legitimate refactors:
        # 1. Remove assert in production file app.py (not a test file)
        # 2. Rename test_old_name -> test_renamed and combine 2 assertions into 1
        # 3. Move test_move_me from test_a.py to test_b.py
        # 4. Extract assertions in test_extract_helper to check_response helper
        # 5. Consolidate test_case_one & test_case_two into parametrized test_cases
        # 6. Edit skip reason on already-skipped test
        # 7. Add brand-new test with runtime skipUnless guard
        self.write({
            "app.py": "def calc(x):\n    if x <= 0:\n        raise ValueError(x)\n    return x * 2\n",
            "tests/test_a.py": (
                "import shutil\n"
                "import pytest\n"
                "import unittest\n\n"
                "def check_response(res):\n"
                "    assert res['ok'] is True\n"
                "    assert res['code'] == 200\n\n"
                "def test_renamed():\n"
                "    assert (1 + 1, 2 + 2) == (2, 4)\n\n"
                "def test_extract_helper():\n"
                "    res = {'ok': True, 'code': 200}\n"
                "    check_response(res)\n\n"
                "@pytest.mark.parametrize('val', [1, 2])\n"
                "def test_cases(val):\n"
                "    assert val == val\n\n"
                "@pytest.mark.skip(reason='updated ticket link')\n"
                "def test_already_skipped():\n"
                "    assert True\n\n"
                "@unittest.skipUnless(shutil.which('shellcheck'), 'optional tool')\n"
                "def test_optional_tool():\n"
                "    assert 1 == 1\n"
            ),
            "tests/test_b.py": (
                "def test_existing():\n    assert 1 == 1\n\n"
                "def test_move_me():\n    assert 'ok' == 'ok'\n"
            ),
        })
        code, rows = self.check_rows("--diff", "--only", "anti-test-weakening")
        self.assertEqual((code, rows["anti-test-weakening"]["status"], rows["anti-test-weakening"]["hits"]), (0, "PASS", []))

    def test_non_git_directory_returns_incomplete_not_pass(self):
        code, rows = self.check_rows("--only", "anti-test-weakening")
        self.assertEqual(code, 2)
        self.assertEqual(rows["anti-test-weakening"]["status"], "INCOMPLETE")
        self.assertTrue(rows["anti-test-weakening"]["reason"])


class CheckCliOptionsTests(BuiltinCheckCase):
    def test_only_rejects_unknown_check_and_diff_staged_are_mutually_exclusive(self):
        unknown = self.run_check("--only", "no-such-check")
        self.assertEqual(unknown.returncode, 2)
        self.assertIn("unknown check: no-such-check", unknown.stderr)

        conflict = self.run_check("--diff", "--staged")
        self.assertEqual(conflict.returncode, 2)
        self.assertEqual(conflict.stderr, "coldfix: invalid arguments; see --help\n")

    def test_default_check_includes_tree_builtins_and_diff_includes_anti_test_weakening(self):
        self.write({"app.py": "x = 1\n"})
        self.git_init_commit()
        code, tree_rows = self.check_rows()
        self.assertEqual(code, 0)
        self.assertIn("agents-md-drift", tree_rows)
        self.assertIn("agent-config-secrets", tree_rows)
        self.assertNotIn("anti-test-weakening", tree_rows)

        code, diff_rows = self.check_rows("--diff")
        self.assertEqual(code, 0)
        self.assertIn("agents-md-drift", diff_rows)
        self.assertIn("agent-config-secrets", diff_rows)
        self.assertIn("anti-test-weakening", diff_rows)

    def test_diff_works_inside_a_git_worktree(self):
        self.write({"app.py": "x = 1\n", "tests/test_app.py": "def test_ok():\n    assert 1 == 1\n"})
        self.git_init_commit()
        wt = self.tree.parent / "wt"
        subprocess.run(GIT + ["worktree", "add", "-q", "-b", "wt-branch", str(wt)], cwd=self.tree, check=True)
        (wt / "tests/test_app.py").write_text("def test_ok():\n    pass\n", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(CLI), "check", "--diff", "--json"],
            cwd=wt,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        rows = {row["lesson"]: row for row in json.loads(result.stdout)["lessons"]}
        self.assertEqual(rows["anti-test-weakening"]["status"], "FAIL")
        self.assertIn("removed 1 assertion(s) without replacement", rows["anti-test-weakening"]["hits"][0])

    def test_git_invocations_disable_lazy_fetch_in_partial_clones(self):
        import coldfix as c

        calls = []

        def fake_run(cmd, *args, **kwargs):
            calls.append((cmd, kwargs.get("env")))
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        expected = {
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_ALLOW_PROTOCOL": "none",
            "GIT_TERMINAL_PROMPT": "0",
        }

        with mock.patch.object(builtin_checks.subprocess, "run", side_effect=fake_run):
            result = builtin_checks.check_anti_test_weakening(self.tree, base="HEAD")
        self.assertEqual(result["status"], "PASS")
        self.assertTrue(calls)
        for cmd, env in calls:
            self.assertEqual(cmd[0], "git")
            self.assertIsNotNone(env)
            for key, value in expected.items():
                self.assertEqual(env.get(key), value)

        calls.clear()
        with mock.patch.object(builtin_checks.subprocess, "run", side_effect=fake_run):
            builtin_checks._git_bytes(self.tree, "show", ":README.md")
        self.assertEqual(len(calls), 1)
        for key, value in expected.items():
            self.assertEqual(calls[0][1].get(key), value)

        calls.clear()
        with mock.patch.object(c.subprocess, "run", side_effect=fake_run):
            c.changed_files(self.tree, "HEAD")
            c.staged_files(self.tree)
        self.assertEqual(len(calls), 3)
        for _, env in calls:
            self.assertIsNotNone(env)
            for key, value in expected.items():
                self.assertEqual(env.get(key), value)

        origin = self.tree.parent / "origin.git"
        subprocess.run(GIT + ["init", "-q", "--bare", str(origin)], check=True)
        subprocess.run(GIT + ["config", "uploadpack.allowfilter", "true"], cwd=origin, check=True)
        seed = self.tree.parent / "seed"
        seed.mkdir()
        subprocess.run(GIT + ["init", "-q"], cwd=seed, check=True)
        (seed / "tests").mkdir()
        (seed / "tests/test_sample.py").write_text("def test_a():\n    assert 1 == 1\n", encoding="utf-8")
        subprocess.run(GIT + ["add", "-A"], cwd=seed, check=True)
        subprocess.run(GIT + ["commit", "-qm", "first"], cwd=seed, check=True)
        first = subprocess.check_output(GIT + ["rev-parse", "HEAD"], cwd=seed, text=True).strip()
        (seed / "tests/test_sample.py").write_text("def test_a():\n    assert 2 == 2\n", encoding="utf-8")
        subprocess.run(GIT + ["commit", "-qam", "second"], cwd=seed, check=True)
        subprocess.run(GIT + ["remote", "add", "origin", str(origin)], cwd=seed, check=True)
        subprocess.run(GIT + ["push", "-q", "origin", "HEAD"], cwd=seed, check=True)
        partial = self.tree.parent / "partial"
        subprocess.run(
            GIT + ["clone", "-q", "--filter=blob:none", origin.as_uri(), str(partial)],
            check=True,
        )
        blocked = builtin_checks._git(partial, "diff", first, "--")
        self.assertNotEqual(blocked.returncode, 0)
        self.assertTrue(
            "not allowed" in blocked.stderr or "lazy fetching disabled" in blocked.stderr,
            blocked.stderr,
        )
        fetch_blocked = builtin_checks._git(partial, "fetch", "origin")
        self.assertNotEqual(fetch_blocked.returncode, 0)
        self.assertIn("not allowed", fetch_blocked.stderr)
        check_result = builtin_checks.check_anti_test_weakening(partial, base=first)
        self.assertEqual(check_result["status"], "INCOMPLETE")

    def test_staged_secret_scan_fails_closed_when_partial_clone_blob_is_uncached(self):
        origin = self.tree.parent / "secrets-origin.git"
        subprocess.run(GIT + ["init", "-q", "--bare", str(origin)], check=True)
        subprocess.run(GIT + ["config", "uploadpack.allowfilter", "true"], cwd=origin, check=True)
        seed = self.tree.parent / "secrets-seed"
        seed.mkdir()
        subprocess.run(GIT + ["init", "-q"], cwd=seed, check=True)
        (seed / ".codex").mkdir()
        token = "sk-" + "proj-" + "A1b2" * 6
        (seed / ".codex/settings.json").write_text(json.dumps({"api_key": token}) + "\n", encoding="utf-8")
        subprocess.run(GIT + ["add", "-A"], cwd=seed, check=True)
        subprocess.run(GIT + ["commit", "-qm", "add codex config"], cwd=seed, check=True)
        old_blob = subprocess.check_output(
            GIT + ["rev-parse", "HEAD:.codex/settings.json"], cwd=seed, text=True
        ).strip()
        (seed / ".codex/settings.json").unlink()
        (seed / "README.md").write_text("clean\n", encoding="utf-8")
        subprocess.run(GIT + ["add", "-A"], cwd=seed, check=True)
        subprocess.run(GIT + ["commit", "-qm", "remove codex config"], cwd=seed, check=True)
        subprocess.run(GIT + ["remote", "add", "origin", str(origin)], cwd=seed, check=True)
        subprocess.run(GIT + ["push", "-q", "origin", "HEAD"], cwd=seed, check=True)

        partial = self.tree.parent / "secrets-partial"
        subprocess.run(
            GIT + ["clone", "-q", "--filter=blob:none", origin.as_uri(), str(partial)],
            check=True,
        )
        subprocess.run(
            GIT + ["update-index", "--add", "--cacheinfo", f"100644,{old_blob},.codex/settings.json"],
            cwd=partial,
            check=True,
        )
        done = subprocess.run(
            [sys.executable, str(CLI), "check", "--staged", "--only", "agent-config-secrets", "--json"],
            cwd=partial,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        payload = json.loads(done.stdout)
        rows = {row["lesson"]: row for row in payload["lessons"]}
        self.assertEqual(rows["agent-config-secrets"]["status"], "INCOMPLETE")
        self.assertIn(".codex/settings.json", rows["agent-config-secrets"]["reason"])


if __name__ == "__main__":
    unittest.main()
