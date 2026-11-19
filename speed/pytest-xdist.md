CI runs `pytest` on one worker while the runner has 4 vCPUs. Adding `pytest-xdist` and `-n auto` runs the same tests on every core.

## Applies when
The CI test step runs `pytest` (directly, `python -m`, `uv run`) with no `-n`/`--numprocesses`/xdist in the workflow, `addopts` or dependencies, and that step takes a minute or more.

## Reproduce
In a clean VM with the runner's core count, install the CI test dependencies, run the CI test command as-is 3 times after one warm-up run, and record the median wall time and the final pytest summary line.

## Fix
Add `pytest-xdist` to the test dependencies and `-n auto` to the CI pytest command (or to `addopts`). One line in the workflow plus one dependency.

## Verify
Run the fixed command 3 times after one warm-up. Accept only if every run has the same passed/failed/skipped counts as the baseline and the same set of FAILED/ERROR test IDs. If counts move, the suite shares state or timing across tests: record it as a negative and do not ship the flag.
