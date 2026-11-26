"""Tier 1 proof: a portable regex check that flags the mistaken code and passes the corrected code."""
import subprocess
import sys

PATTERN_CHARS = 300
SNIPPET_CHARS = 4000
TIMEOUT = 2
MATCH = ("import re, sys\n"
         "pattern, before, after = sys.stdin.read().split('\\0')\n"
         "check = re.compile(pattern, re.M)\n"
         "print(int(bool(check.search(before))), int(bool(check.search(after))))")


def prove(pattern, before, after):
    if (not pattern or len(pattern) > PATTERN_CHARS or not before.strip() or not after.strip()
            or max(len(before), len(after)) > SNIPPET_CHARS or "\0" in pattern + before + after):
        return {}, "diff proof needs a short check pattern and both code snippets"
    try:
        run = subprocess.run([sys.executable, "-I", "-c", MATCH], input="\0".join((pattern, before, after)),
                             capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return {}, "check pattern timed out"
    if run.returncode:
        return {}, "check pattern does not compile"
    hits = run.stdout.split()
    proof = dict(proof_tier="diff", pattern=pattern, before="FAIL" if hits[0] == "1" else "PASS",
                 after="FAIL" if hits[1] == "1" else "PASS")
    if proof["before"] == "PASS":
        return proof, "check misses the mistake"
    if proof["after"] == "FAIL":
        return proof, "check fails the corrected code"
    return proof, ""
