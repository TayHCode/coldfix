# Coldfix — Agent Guide

Coldfix checks repositories for repeated coding-agent mistakes and turns proven fixes into rerunnable rules.

## Install & verify
- Install: `pipx install git+https://github.com/coldstack-dev/coldfix.git` (or `python3 -m pip install .` from a checkout)
- Verify CLI and environment: `coldfix --version`, `coldfix doctor --json`, `coldfix status --json`

## First command
- Scan a repository offline (read-only, zero network): `coldfix check` (add `--json` for machine-readable output, `-v` for all PASS lines, `--diff` for changed files only)
- Try the bundled fixture from a checkout: `coldfix check library/reject-linter-error-output/fixtures/bad`

## Exit codes
| Code | Meaning |
| --- | --- |
| `0` | Pass / success |
| `1` | Finding or failed check / replay |
| `2` | Invalid arguments, incomplete check, or local error |

## Safety rules for agents
- Never pass `--apply` or `--yes` (e.g. `coldfix init --yes`) without explicit user approval.
- Never run `coldfix replay`, `coldfix install`, or `coldfix uninstall` unless the user asks for that side effect.
- With `--json`, stdout is one JSON object with `schema_version: 1` and `exit_code`.
