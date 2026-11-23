# Proven lesson library

Short rules learned from real failures in software projects and in agent work. Each lesson is one
directory. Read the [ranked catalog](library.json) for titles, summaries, categories and proof levels.

## Proof levels

- `rerunnable`: anyone can rerun the proof from files in this directory.
  A chat lesson that gains a fixture proof becomes `rerunnable (opinion)`: the check reruns, but the rule
  stays a model's opinion.
  - Fixture proofs ship `check.py` with `fixtures/bad` and `fixtures/good`. The bad fixture is a
    small synthetic reproduction of the bug class, not the original incident. The check must exit
    1 on the bad fixture and 0 on the good one.
  - When CI would reject the bad fixture as a committed file (a merge marker, a `splitlines()` JSONL reader),
    the lesson ships `fixtures.py` instead of `fixtures/`. Its `FIXTURES` maps `bad` and `good` to
    `{path: text}`, and `rerun.py` writes both trees to a temporary folder before it runs `check.py`.
  - Repository proofs ship `check.py` and `proof.json`, which names a public repository and the
    commits before and after the fix. The check must exit 1 before the fix and 0 after it. A lesson
    with fixtures can also ship `proof.json`; the rerun then needs both proofs to hold.
- `replayed`: a replay on private history failed before the fix and passed after it, so no public
  rerun exists.
- `judged (opinion)`: a model judged an agent chat. It is an opinion, not a proof.
- `judged`: reviewed guidance with no public rerun.

## Check contract

Run `python3 check.py` (or `scan.py` for a scan-backed fixture proof) with the tree under test as the working directory. It prints a JSON status
line and exits 0 for PASS, 1 for FAIL and 2 for INCOMPLETE. Checks use only the Python standard
library and git.

## Portable checks

A lesson can also ship `scan.py`, a static check that `coldfix check [path]` runs on any repository (default:
the current directory). It follows the check contract and adds a `hits` list of `file:line` entries and, on FAIL,
a `fix` string that tells the agent the real fix; without one, `coldfix check` prints the first paragraph of the
rule's `## Fix` section, or of the rule itself. It only reads files in the tree under test: it never imports or runs
that tree's code, tests or README commands, and never uses the network.

These lessons have one:

- [tests-must-test](tests-must-test/scan.py): a module-level `def test*` in a unittest file that unittest never collects
- [conflict-markers](conflict-markers/scan.py): a line that opens or closes an unresolved merge conflict
- [split-jsonl-on-newlines](split-jsonl-on-newlines/scan.py): `json.loads` on each line of `.splitlines()`, which also splits inside JSON strings
- [authz-everywhere](authz-everywhere/scan.py): a `startswith`/`startsWith` path check whose prefix has no trailing `/`, so `/admin` also matches `/administrator`
- [clean-checkout](clean-checkout/scan.py): a `.worktrees` folder that the committed `.gitignore` does not ignore
- [errors-are-states](errors-are-states/scan.py): `return x != "FAIL"` (or `failed`, `failure`), which treats INCOMPLETE as a pass
- [no-environment-assumptions](no-environment-assumptions/scan.py): a `TestCase` with `test` methods in a file that neither the default nor a configured test file pattern discovers, or a vitest test file that no vitest config's literal `include` matches
- [preserve-relative-links-when-moving-documents](preserve-relative-links-when-moving-documents/scan.py): a relative Markdown link to a missing file or outside the repository
- [secrets-and-private-data](secrets-and-private-data/scan.py): a committed token, private key or credential in a URL (it does not look for names, emails or phone numbers)
- [systemd-path-escaping](systemd-path-escaping/scan.py): a `Path*=` value written in quotes or through `json.dumps`, `shlex.quote` or a helper built on them
- [time-regexes-on-long-adversarial-inputs-before](time-regexes-on-long-adversarial-inputs-before/scan.py): a Python regex literal with a repeated group whose inner repeat can also match what follows it, such as `(?:\S+/)*`
- [fence-info-strings](fence-info-strings/scan.py): a fence-opener regex that allows only letters or word characters before `$`, so `python3` or `c++` openers are missed
- [closing-fence-length](closing-fence-length/scan.py): a line compared with `==` or `!=` to the opening fence, so a longer closer or trailing spaces never close it
- [filter-runs-by-failure-time](filter-runs-by-failure-time/scan.py): a time-window comparison on `created_at` in code that reads run conclusions, which drops a rerun that failed later
- [fully-qualified-test-ids](fully-qualified-test-ids/scan.py): test results matched by the last dotted part of their ID, so a same-named test in another class counts
- [reject-linter-error-output](reject-linter-error-output/scan.py): a lint wrapper that returns only whether the exit code is 1 (or 0 or 1), without reading the output for usage or error lines
- [validate-at-the-boundary](validate-at-the-boundary/scan.py): a password that is stripped or trimmed (it does not look for lenient date parsing or the other boundary checks)
- [parse-git-subcommand-first](parse-git-subcommand-first/scan.py): a push restriction that looks for `push` as a substring or list item of the command, instead of parsing the git subcommand
- [runnable-commands](runnable-commands/scan.py): a `git diff` with only the name-only flag, which misses staged and committed changes
- [resolve-tools-before-restricting-path](resolve-tools-before-restricting-path/scan.py): a subprocess run of a bare tool name whose `env=` sets `PATH` without the old `PATH`
- [stream-guest-output-to-log](stream-guest-output-to-log/scan.py): output captured with a `timeout` and written to a file afterwards, in a Python function that does not handle `TimeoutExpired`
- [terminal-cloud-init-status](terminal-cloud-init-status/scan.py): `cloud-init status` run with `check=True`, a `returncode != 0` check on the cloud-init result with no exit-status 2 case, or `cloud-init status ... || exit` in a shell or YAML file
- [pre-push-uses-pushed-tree](pre-push-uses-pushed-tree/scan.py): a pre-push file that tests a path looped over from `git diff` with `exists`/`isfile` or `[ -f`, which reads the working tree instead of the pushed commit
- [update-callers-and-stubs](update-callers-and-stubs/scan.py): a call that passes a keyword its same-module top-level Python function does not accept (other modules and stubs are not checked)
- [use-the-triggering-invocation-id](use-the-triggering-invocation-id/scan.py): `InvocationID` read in a source file that runs `journalctl` but never reads `MONITOR_INVOCATION_ID`, so a failure handler can read a later run's journal

## Rerun the proofs

```sh
python3 library/rerun.py
python3 library/rerun.py closing-fence-length fence-info-strings
```

Repository proofs fetch commits from GitHub, so they need network access.

The runner counts each proof it starts, including incomplete attempts. With no lesson names,
it lists entries without runnable proofs as `JUDGED` and runs the available proofs.

- Exit `0` means at least one proof ran and all results matched expectations.
- Exit `1` means a completed proof disagreed with expectations.
- Exit `2` takes precedence for incomplete proofs, unknown lessons, explicitly requested lessons
  without runnable proofs, or runs that started zero proofs.

## Public lessons

| Lesson | Proof |
| --- | --- |
| [Authorize every privileged action against identity and owner](authz-everywhere/rule.md) | rerunnable |
| [A test must exercise the failure it names](tests-must-test/rule.md) | rerunnable |
| [Keep secrets and private data out of everything you write or publish](secrets-and-private-data/rule.md) | rerunnable |
| [Never commit unresolved merge markers](conflict-markers/rule.md) | rerunnable |
| [Validate at the boundary: reject, don't coerce](validate-at-the-boundary/rule.md) | rerunnable |
| [Treat errors as states: PASS, FAIL or INCOMPLETE](errors-are-states/rule.md) | rerunnable |
| [Parse JSONL at newline record boundaries](split-jsonl-on-newlines/rule.md) | rerunnable |
| [Reject usage and error diagnostics in linter output](reject-linter-error-output/rule.md) | rerunnable |
| [Key test results by fully qualified test ID](fully-qualified-test-ids/rule.md) | rerunnable |
| [Time regexes on long adversarial inputs before merging](time-regexes-on-long-adversarial-inputs-before/rule.md) | rerunnable |
| [Pre-push checks must select targets from the pushed tree, not the working tree](pre-push-uses-pushed-tree/rule.md) | rerunnable |
| [Don't assume the environment](no-environment-assumptions/rule.md) | rerunnable |
| [Preserve relative links when moving documents](preserve-relative-links-when-moving-documents/rule.md) | rerunnable |
| [Do not wrap PathChanged= values in JSON quotes](systemd-path-escaping/rule.md) | rerunnable |
| [Resolve tools before restricting the child PATH](resolve-tools-before-restricting-path/rule.md) | rerunnable |
| [Keep worktrees and runtime state out of git status](clean-checkout/rule.md) | rerunnable |
| [Update every caller and test stub when a function signature changes](update-callers-and-stubs/rule.md) | rerunnable |
| [Run every documented command exactly as written](runnable-commands/rule.md) | rerunnable |
| [Stream guest command output to its log on timeout paths](stream-guest-output-to-log/rule.md) | rerunnable |
| [Handle terminal cloud-init statuses instead of retrying forever](terminal-cloud-init-status/rule.md) | rerunnable |
| [Use the triggering invocation ID in async failure handlers](use-the-triggering-invocation-id/rule.md) | rerunnable |
| [Accept closing fences of the same length or longer, with trailing whitespace](closing-fence-length/rule.md) | rerunnable |
| [Recognize Markdown fence info strings before tracking fence state](fence-info-strings/rule.md) | rerunnable |
| [Filter workflow runs by failure time, not creation time](filter-runs-by-failure-time/rule.md) | rerunnable |
| [Parse the git subcommand before applying publication restrictions](parse-git-subcommand-first/rule.md) | rerunnable |
| [Persist state atomically and retry interrupted effects safely](state-safety/rule.md) | rerunnable |
| [When a producer changes, change every consumer in the same change](change-the-consumers/rule.md) | rerunnable |
| [Change docs in the same commit as the code they describe](docs-sync/rule.md) | rerunnable |
| [Guard read-then-write on shared state](races/rule.md) | judged |
| [Persist setup-failure diagnostics before deleting the guest](persist-diagnostics-before-cleanup/rule.md) | judged |
