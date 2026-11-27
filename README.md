# Coldfix

Coldfix is for developers and teams using coding agents (Claude Code, Codex, Cursor) who want to
catch recurring agent mistakes automatically and turn proven fixes into checks you can rerun.

```text
$ coldfix check
FAIL: tests-must-test (A test must exercise the failure it names)
  test_demo.py:8
  fix: Make the test feed the exact failure it names, assert specific values, avoid racing deadlines, keep its imports, and put it where the runner collects it.
1 with hits, 0 incomplete, 26 passed, 5 with no portable check
```

No telemetry. Nothing leaves your machine by default. Optional `COLDFIX_JUDGE=jev` with `JEV_API_KEY` sends transcript text to `api.typesafe.ai`.

Coldfix records failures, groups exact repeats, and helps you author a rule plus a check command for
a recurring group. A lesson is only *proven* when its check fails on the commit with the mistake and
passes on the commit with the fix. Coldfix never runs a captured command; it runs an authored check
only when you explicitly ask it to replay that check against two Git revisions.

## Install

Install the `coldfix` command with [pipx](https://pipx.pypa.io/):

```sh
git clone https://github.com/coldstack-dev/coldfix.git
cd coldfix
pipx install .
pipx ensurepath
```

Open a new terminal so `coldfix` is on your `PATH`, then run `coldfix --version` or `coldfix --help`.

Or install it into a virtual environment:

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install .
coldfix --help
```

## Try an offline scan

From the Coldfix checkout, scan a fixture that ships with the repository. This works offline
and requires no sign-in, transcript collection, or scheduler.

```sh
coldfix check library/reject-linter-error-output/fixtures/bad
```

Coldfix reports `reject-linter-error-output` at `lint.py:2` and exits with status 1 because it
found a problem.

## Check your repository

`coldfix check` runs the library's static portable checks plus built-in checks (`agents-md-drift` for stale `AGENTS.md`/`CLAUDE.md`/`.cursorrules` paths and commands, `agent-config-secrets` for literal credentials in committed `.claude/settings*.json`, `.codex/*`, and `.cursor/*` files) on the current directory. They only read files: they never run your code, your tests or your README's commands, and never use the network. By default, `coldfix check` prints only hits (and any incomplete checks) plus a one-line summary; pass `-v`/`--verbose` to list all `PASS` and `NO_PORTABLE_CHECK` lines, or `--json` for machine-readable output. Each hit names the lesson, the `file:line` and the fix, and the command exits 1 when anything is hit. `coldfix check <path>` scans another tree. `coldfix check --diff [BASE]` reports only hits in files changed since `BASE` (default `HEAD`) and untracked files, plus `anti-test-weakening` on the diff; it scans a copy of those files and rechecks any hit against the whole tree, so a clean change returns in about a second. `coldfix check --staged` scans staged changes, and `--only CHECK` limits a run to named check(s). `coldfix hook` is the agent hook: it reads a hook event on stdin, adds the matching proven facts and always exits 0. The block below makes a repository with a test that unittest never collects; in your own repository, just run `coldfix check`.

```sh
mkdir demo && cd demo && git init -q
printf 'import unittest\n\n\nclass DemoTests(unittest.TestCase):\n    pass\n\n\ndef test_never_collected():\n    pass\n' > test_demo.py
coldfix check || echo "coldfix check exited $?"
```

To run `coldfix check` on every pull request in GitHub Actions:

```yaml
name: coldfix-check
on: pull_request
permissions:
  contents: read
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          persist-credentials: false
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: python3 -m pip install git+https://github.com/coldstack-dev/coldfix.git
      - run: coldfix check
```

## Requirements & privacy

No telemetry. Nothing leaves your machine by default. Optional COLDFIX_JUDGE=jev with JEV_API_KEY sends transcript text to api.typesafe.ai.

- Python 3.10 or later. Coldfix uses only the standard library and has no Python dependencies.
- Git and a POSIX shell (Linux or macOS) for replay and for the lesson commands, which use POSIX file locking.
- Linux with bubblewrap (`bwrap`) for sandboxed replay (`admit`, `learn` and the CI, review and witness replays)
  and for the test suite. See [Replay sandbox](#replay-sandbox).
- Python's `venv` module (Debian and Ubuntu package `python3-venv`) for CI replays whose workflow runs
  `pip install`, and for the test suite.
- Optional: PyYAML (`python3 -m pip install -r requirements-replay.txt`), only for replaying GitHub Actions
  workflows. Tests that need it are skipped without it.

For optional drafting models, fleet CLI routing, transcript judging, and delivery integrations, see [Advanced](#advanced).

## First run

Run these commands in any directory. They record the same failure twice, author a rule and a check
for it, and export the rule to `exports/count-check.md`. Nothing here runs the recorded command or the
check, and nothing leaves your machine. Records go to `.coldfix/` in the current directory.

```sh
printf '%s\n' 'AssertionError: expected count 2' | coldfix capture --command 'python3 -m unittest' --repo example --agent codex
printf '%s\n' 'AssertionError: expected count 2' | coldfix capture --command 'python3 -m unittest' --repo example --agent codex
coldfix propose
GROUP=$(coldfix propose | python3 -c 'import json,sys; print(json.load(sys.stdin)["groups"][0]["group_id"])')
PROPOSAL=$(coldfix propose --group "$GROUP" --kind test --title 'Verify expected counts' --rule 'Check the expected count with an explicit assertion.' --check-command 'python3 -m unittest' | python3 -c 'import json,sys; print(json.load(sys.stdin)["proposal_id"])')
coldfix export --proposal "$PROPOSAL" --output exports/count-check.md
cat exports/count-check.md
```

`propose` with no options lists the groups. A group needs two matching records before you can author a
proposal for it. `--kind` is `lint`, `test` or `hook` and only labels how you intend to enforce the rule.

The export above says `Verification: unproven`: nothing has checked the rule yet.

## Prove your first lesson

Run these commands in an empty directory. They make a toy Git repository with a bug commit and a fix
commit, record the bug's failure twice, author a lesson with a check, replay the check on both commits
and export the proven lesson to `exports/first-lesson.md`. Records go to `.coldfix/` in the current
directory, and nothing leaves your machine.

```sh
mkdir toy
printf '%s\n' 'def count(items):' '    return len(items) + 1' > toy/count.py
git -C toy init --quiet
git -C toy add count.py
git -C toy -c user.name=Example -c user.email=example@example.com commit --quiet -m 'Add count'
BUG=$(git -C toy rev-parse HEAD)
printf '%s\n' 'def count(items):' '    return len(items)' > toy/count.py
git -C toy -c user.name=Example -c user.email=example@example.com commit --quiet -am 'Fix count'
FIX=$(git -C toy rev-parse HEAD)
cat > check.py <<'EOF'
import json
import sys

sys.path.insert(0, ".")
from count import count

if count(["a", "b"]) == 2:
    print(json.dumps({"status": "PASS"}))
    sys.exit(0)
print(json.dumps({"status": "FAIL", "failure": "count-off-by-one"}))
sys.exit(1)
EOF
printf '%s\n' 'AssertionError: 3 != 2' | coldfix capture --command 'python3 -m unittest' --repo toy --agent codex
printf '%s\n' 'AssertionError: 3 != 2' | coldfix capture --command 'python3 -m unittest' --repo toy --agent codex
GROUP=$(coldfix propose | python3 -c 'import json,sys; print(json.load(sys.stdin)["groups"][0]["group_id"])')
LESSON=$(coldfix lesson --group "$GROUP" --kind test --level test --title 'Count every item once' --rule 'count() returns the number of items, with no offset.' --failure 'count() returned one more than the number of items.' --fix 'count() returns len(items).' --check-file check.py --check-command 'python3 {check}' --expected-failure count-off-by-one | python3 -c 'import json,sys; print(json.load(sys.stdin)["lesson_id"])')
coldfix replay --lesson "$LESSON" --repo toy --before "$BUG" --after "$FIX" --status active
coldfix export --lesson "$LESSON" --output exports/first-lesson.md
cat exports/first-lesson.md
```

`replay` prints `"verification": "proven"` because the check failed with `count-off-by-one` on the bug
commit and passed on the fix commit. The export says `Verification: proven` and records both commits,
their trees, the check's exit codes and the check's sha256.

`lesson --group` accepts the `group_id` that `propose` prints. `replay` and `export` accept the lesson's
`lesson_id` and use its latest revision. A proven replay without `--status` prints `next`, the exact
command that activates the lesson so `export` accepts it. Every subcommand documents its options with
`--help`.

`coldfix agents --repo <checkout>` delivers lessons without any other tool. It writes the active, proven
lessons that apply to that repository (proven on its history, or global) into a marked block in its
`AGENTS.md`, and in `CLAUDE.md` if one exists. Rerun it after lessons change: it rewrites only the
block, and removes it when no lesson applies.

Replay runs the check in a fresh checkout of each revision. The check must print exactly one JSON
object on stdout and exit with the matching code:

| Result | Exit | stdout |
| --- | --- | --- |
| Pass | 0 | `{"status":"PASS"}` |
| Expected failure | 1 | `{"status":"FAIL","failure":"<expected_failure>"}` |
| Cannot check | 2 | `{"status":"INCOMPLETE"}` |

Plain text, extra output, another failure identifier or a mismatched exit code makes that run
`INCOMPLETE`, and replay exits 2. Its `reason` names each incomplete side (`before`, `after`, or
`reversed` for incidents), the exit code and what was wrong with stdout. A lesson is proven only when
the check fails with the expected failure before and passes after.

## Replay sandbox

Sandboxed replay runs each check under bubblewrap with no network, no host environment and only `/usr`,
`/bin`, `/lib` and `/lib64` visible read-only. Check that it works on your machine:

```sh
coldfix doctor
```

It prints `{"sandbox": "available"}`, or exits 2 with `coldfix: sandbox unavailable: <reason>` and the fix.
Sandboxed replay stops with the same message instead of recording an `INCOMPLETE` result.

Pytest proofs run the system `python3 -m pytest` inside the sandbox. Without it (`sudo apt install python3-pytest`)
every pytest pair replays `INCOMPLETE` and is rejected, never admitted.

On Debian or Ubuntu, install bubblewrap:

```sh
sudo apt install bubblewrap
```

Ubuntu 23.10 and later (including 24.04) set `kernel.apparmor_restrict_unprivileged_userns=1`, so `bwrap`
cannot create its namespaces and `doctor` reports
`bwrap cannot create the sandbox (bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted)`.
Give `bwrap` alone an AppArmor profile that allows user namespaces, the same kind Ubuntu ships for
`flatpak`. The restriction stays on for every other program:

```sh
printf '%s\n' 'abi <abi/4.0>,' 'include <tunables/global>' 'profile bwrap /usr/bin/bwrap flags=(unconfined) {' '  userns,' '}' | sudo tee /etc/apparmor.d/bwrap
sudo apparmor_parser -r /etc/apparmor.d/bwrap
coldfix doctor
```

## Test

On Debian or Ubuntu, install the test prerequisites and set up the sandbox as in
[Replay sandbox](#replay-sandbox):

```sh
sudo apt install bubblewrap python3-venv
python3 coldfix.py doctor
python3 -m unittest discover -s tests
```

Run `doctor` first: without a working sandbox every sandboxed replay test fails with the same
`sandbox unavailable` message.

Some tests are skipped when an optional tool, such as `shellcheck`, is missing.

## Storage and safety

- Each capture is a separate immutable JSON file under `.coldfix/` (or `--store`). New directories use mode
  700 and files use mode 600. Records are plaintext: they are not encrypted or scrubbed.
- Exports contain only authored text and opaque evidence IDs, never the captured command, error or labels.
- Authored text is screened for common credential shapes. The screen is conservative and incomplete, so never
  paste raw logs into a rule.
- Commands exit 0 on success, 1 on a finding or a complete failed check or replay, and 2 on invalid input or
  a local I/O failure. Invalid arguments print `coldfix: invalid arguments; see --help` without echoing them.
- `check --json`, `doctor --json`, and `status --json` print one object with `schema_version: 1` and
  `exit_code`. Usage and runtime errors report the reason on stderr and set `exit_code` to `2`.

## Run the automatic loop

On a fresh Ubuntu machine, after [Install](#install):

1. Install the GitHub CLI and bubblewrap: `sudo apt install gh bubblewrap`. Run `coldfix doctor` and apply
   the fix it prints, if any (see [Replay sandbox](#replay-sandbox)). This is the `local` backend: replays run
   in a bubblewrap sandbox on this machine. Inside a container, the container must also allow nested
   namespaces (for Incus, `security.nesting=true`), or `doctor` reports `Can't mount proc`. To replay somewhere
   else, such as a VM or any host over SSH, set `COLDFIX_REPLAY_RUNNER` instead
   ([Configured runner](docs/replay-backends.md#configured-runner)).
2. Give Coldfix read access to your repositories' Actions runs: `export GH_TOKEN=<token>` (or `gh auth login`),
   and `export GITHUB_OWNER=<your GitHub user or organization>`.
3. Set `COLDFIX_MODEL_CMD` to your model command (see [Advanced](#advanced)).
4. Point the loop at your transcript folder (Claude Code keeps its transcripts in `~/.claude/projects`, Codex in
   `~/.codex/sessions`) and run its first pass:

```sh
coldfix setup --owner "$GITHUB_OWNER" --transcripts "$HOME/.claude/projects" --backend local
```

`--owner` defaults to the `gh` login, and `--transcripts` takes more than one folder.

`setup` checks each prerequisite and names the missing one. It then collects failed `verify` workflow runs from
the owner's private repositories, scans up to 100 new transcripts (`--limit`) for mistakes a person, test or
reviewer caught, and prints JSON with the number of `candidates` and the `first_candidate`. Records go to
`.coldfix/` in the current directory. Run the same command again, for example hourly from cron, to keep the loop
going: transcripts already scanned are skipped.

## Advanced

### Recurring learning (`init --yes`)

To opt into recurring learning, run this in the repository you want Coldfix to learn from:

```sh
coldfix init --yes
```

`init` runs `doctor` (stopping with its fix if the sandbox cannot run) and saves `.coldfix/config.json` with
this Git checkout as `repo`. With `--yes` it never prompts: it keeps an existing config and needs a Git checkout
or `--repo`. Without `--yes`, it asks before replacing an existing config and asks for the repository when you
run it outside one.

`--yes` then starts the automatic loop. It needs `gh` signed in (`gh auth login`), the
`claude` or `codex` CLI signed in and on `PATH`, and transcripts in `~/.claude/projects` or `~/.codex/sessions`.
If one is missing, it names it and installs no wrapper or schedule. Otherwise it writes `.coldfix/model-cmd` (the
`COLDFIX_MODEL_CMD` wrapper around that CLI), installs a `coldfix-loop` systemd user timer, or a cron entry when
user systemd is unavailable, that runs `setup` hourly, and runs `setup` once now. Only the owning store can update that schedule. `coldfix status` shows the transcripts analyzed, the CI failures collected and
the next scheduled run.

To stop recurring learning, run `coldfix --store PATH uninstall` before removing the package.
Run it in the repository where you ran `install`, or pass `--repo REPO` to remove its managed
hook and workflows. Existing store evidence, edited hooks and files installed before Coldfix
recorded hook ownership stay in place. After uninstall, another store can claim the schedule.

Cron keeps up to 64 KiB each in `loop.log`, its rotated copy `loop.log.1`, and
`loop.log.failure`, which holds the tail of the most recent failed invocation.

### Optional model, Coldlocker, Coldbox, and Jev integrations

- **Coldlocker delivery:** proven lessons can be exported as Markdown rule fragments and optionally delivered to a [Coldlocker](https://github.com/coldstack-dev/coldlocker) rules repository. Automatic delivery (`init` plus `learn`) uses a Coldlocker checkout and the installed `coldrelease` command.
- **Coldbox composite action:** the repository's `action.yml` runs `coldfix check` specifically on self-hosted [Coldbox](https://github.com/coldstack-dev/coldbox) runners (`runs-on: [self-hosted, coldbox]`, `uses: coldstack-dev/coldfix@main`) and exits 2 on non-Coldbox runners. For standard GitHub-hosted runners, use the `ubuntu-latest` workflow in [Check your repository](#check-your-repository).
- **Fleet CLI (`COLDFIX_FLEET_CLI`):** optional name of a fleet command (letters, digits, `-`, `_`) that resolves model roles and labels imported review and verify records. Unset, model-role resolution, review-receipt import and verify-pair import are off.
- **Drafting & judging model (`COLDFIX_MODEL_CMD`):** optional, only for `draft`, `learn` and `learn_daily.py`. They send a group's stored failure evidence and a Git diff to that model. Set `COLDFIX_MODEL_CMD` to any command (split like a shell line) that takes a role (`drafter` or `reviewer`) and a JSON Schema as its two arguments, reads the prompt on stdin, and prints one JSON object matching the schema. Use a different model for `reviewer` than for `drafter`. Calls may run in a temporary working directory, so give the command absolute paths. `learn_daily.py` makes model calls through it in at most one run per UTC day. Without the setting, drafting uses the `claude` CLI on `PATH` with the model role resolved through `COLDFIX_FLEET_CLI`. The transcript pipeline uses the same command when it is set: `transcript_mistakes.py`, `transcript_lessons.py` and the judged-lesson recheck call it with role `judge` (schema `{"score": number from 0 to 1}`, the probability that the question asked is true) in place of Jev, and lesson extraction calls it with role `extractor` in place of the `bulk-codex` route. `JEV_API_KEY` and `COLDFIX_FLEET_CLI` are then not needed.

### Replayed and judged lessons

Every delivered lesson says which kind of evidence backs it:

- **replayed**: Coldfix ran the lesson's check on the commit with the mistake, where it failed with the
  expected failure, and on the commit with the fix, where it passed (incidents also fail again with the
  fix reversed). Exported lesson files hold that replay record. Their `Verification: proven` line, which
  Coldlocker's import reads, means replayed.
- **judged**: nothing was executed. When opted in (`COLDFIX_JUDGE=jev` with `JEV_API_KEY`), a model judge (Jev) scores the mistaken agent message, a rewrite of it
  that follows the rule, the later corrected message and clean control messages. Transcript lessons in
  the shared lessons folder say `verification: judged` and store the `judge_model`, `judge_threshold`,
  `judge_prompt_sha256` (sha256 of `transcript_lessons.JUDGE_PROMPTS`), `judge_controls` and
  `judge_control_failures`, so they can be judged again under the same judge. The weekly recheck re-judges
  each one against its stored wrong and corrected messages and reports `passed` or `regression`.
- **replay record**: a hand-written lesson states `evidence: replay OWNER/REPO@SHA:PATH`, its pinned
  reproduction record. The weekly recheck checks that the record and every repository the lesson cites still
  exist, flags any that are gone, and reports a `proposed` lesson as `regression` until it is re-proven or retired.

## Limitations

- Coldfix does not invent tests from logs. You, or the optional drafting model, author every check.
- Grouping is exact: the repo, command and error strings must match, including whitespace.
  Coldpass failures are the exception: they group by a failure class derived from each run's evidence.
- The automatic pipeline (`learn`, `ingest-and-propose`) and delivery assume a Coldlocker rules repository and
  a drafting model (`COLDFIX_MODEL_CMD`, or the `claude` CLI with `COLDFIX_FLEET_CLI`). Without them, use the manual
  `capture`, `propose`, `lesson`, `replay` and `export` commands.
- Linux and macOS only. Windows is untested.
- This is a small personal tool, offered as is, with no support commitment.

## License

MIT. See [LICENSE](LICENSE).
