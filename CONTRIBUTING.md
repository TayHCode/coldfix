# Contributing

Coldfix is a set of standard-library Python scripts. Keep changes small and focused.

## Run the checks

The tests need Python 3.10+, Git and a POSIX shell. Nothing else is required.

```sh
python3 -m unittest discover -s tests
```

A change that touches behavior needs a test in `tests/` that fails without it.

## Style

- Make the smallest change that fully solves the problem, and add no dependency: Coldfix uses only the Python
  standard library.
- No code comments. Use clear names and small functions, and say why in the commit message or the PR.
- Never weaken, skip or delete a test to make it pass.
- Keep tokens, private hostnames, IP addresses and personal paths out of code, tests, examples and reports. Use
  placeholders such as `example.org`, `192.0.2.1` and `/home/example` in fixtures.

## Pull requests

Describe what changed and how you verified it. Report security problems as described in [SECURITY.md](SECURITY.md), not in
a public issue.

Changes are checked for private names, addresses and paths before they are published. Fix each finding at its
source. Add an approval to `release-check.json` only for a false positive that is not a private name or
term, with a reason, in a reviewed pull request.
