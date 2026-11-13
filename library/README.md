# Proven lesson library

Short rules learned from real failures in software projects and in agent work. Each lesson is one
directory: `library/<id>/rule.md` holds the rule, and a rerunnable lesson also holds its proof.

## Proof levels

- `rerunnable`: anyone can rerun the proof from files in this directory.
  - Fixture proofs ship `check.py` with `fixtures/bad` and `fixtures/good`. The bad fixture is a
    small synthetic reproduction of the bug class, not the original incident. The check must exit
    1 on the bad fixture and 0 on the good one.
  - When CI would reject the bad fixture as a committed file (a merge marker, a `splitlines()` JSONL reader),
    the lesson ships `fixtures.py` instead of `fixtures/`. Its `FIXTURES` maps `bad` and `good` to
    `{path: text}`, and `rerun.py` writes both trees to a temporary folder before it runs `check.py`.
  - Repository proofs ship `check.py` and `proof.json`, which names a public repository and the
    commits before and after the fix. The check must exit 1 before the fix and 0 after it. A lesson
    with fixtures can also ship `proof.json`; the rerun then needs both proofs to hold.
- `judged`: the lesson was proven against private history, or by judging agent transcripts, so no
  public rerun exists. Treat it as reviewed guidance, not as a reproducible proof.

## Check contract

Run `python3 check.py` with the tree under test as the working directory. It prints a JSON status
line and exits 0 for PASS, 1 for FAIL and 2 for INCOMPLETE. Checks use only the Python standard
library and git.

## Portable checks

A lesson can also ship `scan.py`, a static check that `coldfix check` runs on any repository. It follows
the check contract and adds a `hits` list of `file:line` entries. It only reads files in the tree under
test: it never imports or runs that tree's code, tests or README commands, and never uses the network.
These lessons have one:

- [tests-must-test](tests-must-test/scan.py): a module-level `def test*` in a unittest file that unittest never collects
- [conflict-markers](conflict-markers/scan.py): a line that opens or closes an unresolved merge conflict
- [jsonl-unicode-separators](jsonl-unicode-separators/scan.py): `json.loads` on each line of `.splitlines()`, which also splits inside JSON strings
- [authz-everywhere](authz-everywhere/scan.py): a `startswith`/`startsWith` path check whose prefix has no trailing `/`, so `/admin` also matches `/administrator`
- [clean-checkout](clean-checkout/scan.py): a `.worktrees` folder that the committed `.gitignore` does not ignore
- [cross-account-shared-queue](cross-account-shared-queue/scan.py): a group- or world-writable mode passed to `mkdir`, `makedirs`, `touch` or `os.open` with no `chmod` or `umask` in the same function
- [errors-are-states](errors-are-states/scan.py): `return x != "FAIL"` (or `failed`, `failure`), which treats INCOMPLETE as a pass
- [no-environment-assumptions](no-environment-assumptions/scan.py): a `TestCase` with `test` methods in a file that neither the default nor a configured test file pattern discovers
- [preserve-relative-links-when-moving-documents](preserve-relative-links-when-moving-documents/scan.py): a relative Markdown link to a missing file or outside the repository
- [secrets-and-private-data](secrets-and-private-data/scan.py): a committed token, private key or credential in a URL (it does not look for names, emails or phone numbers)
- [systemd-path-escaping](systemd-path-escaping/scan.py): a `Path*=` value written in quotes or through `json.dumps`, `shlex.quote` or a helper built on them
- [time-regexes-on-long-adversarial-inputs-before](time-regexes-on-long-adversarial-inputs-before/scan.py): a Python regex literal with a repeated group whose inner repeat can also match what follows it, such as `(?:\S+/)*`
- [fence-info-strings](fence-info-strings/scan.py): a fence-opener regex that allows only letters or word characters before `$`, so `python3` or `c++` openers are missed
- [closing-fence-length](closing-fence-length/scan.py): a line compared with `==` or `!=` to the opening fence, so a longer closer or trailing spaces never close it
- [normalize-crlf-before-markers](normalize-crlf-before-markers/scan.py): a `startswith`/`endswith` check on text ending in a newline, in a file that never handles a carriage return
- [filter-runs-by-failure-time](filter-runs-by-failure-time/scan.py): a time-window comparison on `created_at` in code that reads run conclusions, which drops a rerun that failed later
- [fully-qualified-test-ids](fully-qualified-test-ids/scan.py): test results matched by the last dotted part of their ID, so a same-named test in another class counts
- [reject-linter-error-output](reject-linter-error-output/scan.py): a lint wrapper that returns only whether the exit code is 1 (or 0 or 1), without reading the output for usage or error lines
- [validate-at-the-boundary](validate-at-the-boundary/scan.py): a password that is stripped or trimmed (it does not look for lenient date parsing or the other boundary checks)
- [verify-the-right-thing](verify-the-right-thing/scan.py): a GitHub workflow that diffs or logs against `HEAD~1` or `HEAD^`, which checks only the last commit of a push
- [parse-git-subcommand-first](parse-git-subcommand-first/scan.py): a push restriction that looks for `push` as a substring or list item of the command, instead of parsing the git subcommand
- [runnable-commands](runnable-commands/scan.py): a `git diff` with only the name-only flag, which misses staged and committed changes
- [hook-suppression-by-session](hook-suppression-by-session/scan.py): a session id that falls back to a fixed string such as `session_id or "default"` and is then used as a key or path, so every run without one shares state
- [once-only-recall-per-session](once-only-recall-per-session/scan.py): a session id passed through `re.sub` or `.replace` and then used as a key or path, so two ids can map to one key
- [inline-review-edits-after-merge](inline-review-edits-after-merge/scan.py): `created_at` ordered against `merged_at` in a Python function where no `merged_at` comparison uses `updated_at`
- [resolve-tools-before-restricting-path](resolve-tools-before-restricting-path/scan.py): a subprocess run of a bare tool name whose `env=` sets `PATH` without the old `PATH`
- [stream-guest-output-to-log](stream-guest-output-to-log/scan.py): output captured with a `timeout` and written to a file afterwards, in a Python function that does not handle `TimeoutExpired`
- [terminal-cloud-init-status](terminal-cloud-init-status/scan.py): `cloud-init status` run with `check=True`, a `returncode != 0` check on the cloud-init result with no exit-status 2 case, or `cloud-init status ... || exit` in a shell or YAML file
- [fetch-the-current-base-before-creating-the-guard](fetch-the-current-base-before-creating-the-guard/scan.py): a Python loop that merges and adds worktrees from `origin/...` without fetching inside the loop
- [pre-push-uses-pushed-tree](pre-push-uses-pushed-tree/scan.py): a pre-push file that tests a path looped over from `git diff` with `exists`/`isfile` or `[ -f`, which reads the working tree instead of the pushed commit
- [update-callers-and-stubs](update-callers-and-stubs/scan.py): a call that passes a keyword its same-module top-level Python function does not accept (other modules and stubs are not checked)
- [reject-malformed-facts](reject-malformed-facts/scan.py): `json.load(s)` of JSON text taken straight from `sys.stdin`, `sys.argv`, `input()` or parsed `args`, with no handler for both `ValueError` and `RecursionError` in that function or around every caller (a file named by an argument is not checked)

## Rerun the proofs

```sh
python3 library/rerun.py
python3 library/rerun.py closing-fence-length fence-info-strings
```

Repository proofs fetch commits from GitHub, so they need network access.

## Add newly proven lessons

`coldfix export --format library --output library --lessons <transcript lesson folder>` adds each proven
lesson whose rule is not already here, after the export privacy screen. Exported lessons are `judged`, because the
store cannot show that a replay repository is public. To make one `rerunnable`, write a synthetic fixture proof
(`<proofs>/<id>/check.py`, `fixtures/bad`, `fixtures/good`) and add `--proofs <proofs>`. The export reruns each proof
in a clean folder and runs the privacy screen on every file. It adds the proof only when the check exits 1 on the bad
fixture and 0 on the good one. Otherwise the lesson stays `judged`, and `proof_refused` gives the reason. Run
release-check on the result before any release.

## Cross-repository lessons

| Lesson | Proof |
| --- | --- |
| [Change docs in the same commit as the code they describe](docs-sync/rule.md) | rerunnable |
| [Persist state atomically and retry interrupted effects safely](state-safety/rule.md) | rerunnable |
| [Keep secrets and private data out of everything you write or publish](secrets-and-private-data/rule.md) | judged |
| [Bind every receipt to the tree that ships](verify-the-right-thing/rule.md) | judged |
| [Run every documented command exactly as written](runnable-commands/rule.md) | rerunnable |
| [Validate at the boundary: reject, don't coerce](validate-at-the-boundary/rule.md) | rerunnable |
| [Treat errors as states: PASS, FAIL or INCOMPLETE](errors-are-states/rule.md) | rerunnable |
| [When a producer changes, change every consumer in the same change](change-the-consumers/rule.md) | rerunnable |
| [Don't assume the environment](no-environment-assumptions/rule.md) | rerunnable |
| [Authorize every privileged action against identity and owner](authz-everywhere/rule.md) | rerunnable |
| [Guard read-then-write on shared state](races/rule.md) | judged |
| [A test must exercise the failure it names](tests-must-test/rule.md) | rerunnable |
| [Never commit unresolved merge markers](conflict-markers/rule.md) | rerunnable |
| [Keep worktrees and runtime state out of git status](clean-checkout/rule.md) | rerunnable |
| [Route models by role and the owner's latest instruction](model-routing/rule.md) | judged |
| [Keep going until done or truly blocked](keep-going/rule.md) | judged |

## Agent conduct lessons

| Lesson | Proof |
| --- | --- |
| [Use the documented worker lifecycle and receipt wake path](worker-lifecycle-and-receipts/rule.md) | judged |
| [Assign one task to each agent](one-task-per-agent/rule.md) | judged |
| [Answer the user's questions before launching unrelated work](answer-questions-before-launching/rule.md) | judged |
| [Stop repeating a failed credential path and offer the user’s requested alternative](switch-from-a-failed-credential-path/rule.md) | judged |
| [Stop when uncertain repair advice risks damage](stop-on-uncertain-repair-advice/rule.md) | judged |
| [Verify the bottleneck before prescribing an architecture](verify-the-bottleneck-first/rule.md) | judged |
| [Keep diagnostic requests proportionate](proportionate-diagnostics/rule.md) | judged |
| [Verify an owner is active before recommending that the user wait](verify-the-owner-is-active/rule.md) | judged |
| [Establish that a process is stale before terminating it](prove-a-process-is-stale/rule.md) | judged |
| [Keep tool outputs within the sample job’s allowed directory](keep-outputs-in-allowed-dirs/rule.md) | judged |
| [Do not describe an unfinished check as a blocker when a documented fallback works](fallback-is-not-a-blocker/rule.md) | judged |
| [Use the configured SSH alias](use-the-configured-ssh-alias/rule.md) | judged |

## Repository-specific lessons

| Lesson | Proof |
| --- | --- |
| [Clearing facts restores whole-rule authoring](clear-facts-restores-rule/rule.md) | judged |
| [Parse JSONL at newline record boundaries](jsonl-unicode-separators/rule.md) | rerunnable |
| [Isolate once-only recall by session](once-only-recall-per-session/rule.md) | judged |
| [Resolve the Git root before compiling from a subdirectory](compile-from-git-root/rule.md) | judged |
| [Isolate hook suppression by session](hook-suppression-by-session/rule.md) | judged |
| [Reject malformed facts JSON with a controlled error](reject-malformed-facts/rule.md) | judged |
| [Git hook install tests must not contact Codex](hook-tests-stay-offline/rule.md) | judged |
| [Fetch the current base before creating the guard worktree](fetch-the-current-base-before-creating-the-guard/rule.md) | judged |
| [Preserve relative links when moving documents](preserve-relative-links-when-moving-documents/rule.md) | rerunnable |
| [Export the full committed source tree to pre-push snapshots](export-the-full-committed-source-tree-to-pre/rule.md) | judged |
| [Include every tombstone alias in receipt identity extraction](include-every-tombstone-alias-in-receipt/rule.md) | judged |
| [Time regexes on long adversarial inputs before merging](time-regexes-on-long-adversarial-inputs-before/rule.md) | rerunnable |
| [Keep the project virtual environment available in snapshots](keep-the-project-virtual-environment-available/rule.md) | judged |
| [Do not count insertion-only hunks as changing a reviewed line](do-not-count-insertion-only-hunks-as-changing-a/rule.md) | rerunnable |
| [Register required signals for bounded consumers](register-required-signals-for-bounded-consumers/rule.md) | judged |
| [Remap a carried comment's line before comparing it at a newer head](remap-a-carried-comment-s-line-before-comparing/rule.md) | rerunnable |

## Code review lessons

| Lesson | Proof |
| --- | --- |
| [Put filesystem-behavior tests in the shared fake_filesystem_test class](shared-filesystem-test-class/rule.md) | rerunnable |
| [Use the same finditer loop pattern for parallel reference and footnote definition code](matching-sibling-parsers/rule.md) | judged |
| [Record reference definitions even when non-reference text follows them](record-reference-definitions/rule.md) | rerunnable |
| [Normalize line endings before checking the delivery marker](normalize-crlf-before-markers/rule.md) | rerunnable |
| [Accept closing fences of the same length or longer, with trailing whitespace](closing-fence-length/rule.md) | rerunnable |
| [Split cached JSONL on newlines, not splitlines()](split-jsonl-on-newlines/rule.md) | judged |
| [Resolve tools before restricting the child PATH](resolve-tools-before-restricting-path/rule.md) | judged |
| [Keep destination conflicts from masking privacy-screen tests](fresh-destination-in-guard-tests/rule.md) | judged |
| [Refuse hit timings when no fact is anchored to a path](refuse-hit-timings-without-anchors/rule.md) | judged |
| [Recognize Markdown fence info strings before tracking fence state](fence-info-strings/rule.md) | rerunnable |
| [Filter workflow runs by failure time, not creation time](filter-runs-by-failure-time/rule.md) | rerunnable |
| [Check item completeness before inferring a blocked ending](check-completeness-before-blocked/rule.md) | judged |
| [Stream guest command output to its log on timeout paths](stream-guest-output-to-log/rule.md) | judged |
| [Update every caller and test stub when a function signature changes](update-callers-and-stubs/rule.md) | judged |
| [Carry parsed values forward instead of re-reading the source](carry-parsed-values-forward/rule.md) | judged |
| [Exclude inline review bodies edited after merge from evidence](inline-review-edits-after-merge/rule.md) | judged |
| [Pre-push checks must select targets from the pushed tree, not the working tree](pre-push-uses-pushed-tree/rule.md) | judged |
| [Make the host-wide queue accessible across Unix accounts](cross-account-shared-queue/rule.md) | judged |
| [Create revision rows for SHAs that appear only in stored receipts](revision-rows-for-receipt-shas/rule.md) | judged |
| [Key test results by fully qualified test ID](fully-qualified-test-ids/rule.md) | rerunnable |
| [Keep node_modules at the root or tracked in git flagged for manual review](flag-node-modules-for-review/rule.md) | judged |
| [Exclude the PR author's own events when counting maintainer activity](exclude-author-activity/rule.md) | rerunnable |
| [Park a retry only after a distinct retry attempt fails](park-after-a-real-retry/rule.md) | judged |
| [Comparison runs must stay excluded from staging even when a fix links to them](keep-comparison-runs-excluded/rule.md) | judged |
| [Add every new rules key to rules.example.toml in the same change](example-config-covers-every-key/rule.md) | judged |
| [Upgrade docs must back up the configured store as well as .hub-store](back-up-every-store/rule.md) | judged |
| [Parse the git subcommand before applying publication restrictions](parse-git-subcommand-first/rule.md) | judged |
| [Flag upper caps written as rejection guards](flag-caps-in-rejection-guards/rule.md) | judged |
| [Persist setup-failure diagnostics before deleting the guest](persist-diagnostics-before-cleanup/rule.md) | judged |
| [Do not wrap PathChanged= values in JSON quotes](systemd-path-escaping/rule.md) | rerunnable |
| [Handle terminal cloud-init statuses instead of retrying forever](terminal-cloud-init-status/rule.md) | judged |
| [Reject usage and error diagnostics in linter output](reject-linter-error-output/rule.md) | rerunnable |
| [Apply the outbox age limit to enrichment failures too](outbox-age-limit-everywhere/rule.md) | judged |
