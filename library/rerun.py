import hashlib
import json
import os
import runpy
import signal
import subprocess
import sys
import tempfile

LIBRARY = os.path.dirname(os.path.abspath(__file__))


EXPECTED = {1: "FAIL", 0: "PASS"}
TIMEOUT = 300


class Incomplete(Exception):
    pass


def run_check(check, cwd):
    process = subprocess.Popen([sys.executable, "-I", "-B", check], cwd=cwd, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, errors="replace", start_new_session=True)
    try:
        stdout = process.communicate(timeout=TIMEOUT)[0]
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise Incomplete(f"timed out after {TIMEOUT} s") from None
    lines = stdout.strip().splitlines()
    try:
        report = json.loads(lines[-1]) if lines else None
    except ValueError:
        report = None
    if not isinstance(report, dict):
        report = {}
    if process.returncode == 2:
        raise Incomplete(report.get("reason"))
    status = report.get("status")
    return report if status is not None and status == EXPECTED.get(process.returncode) else None


def check_holds(check, cwd, exit_code):
    report = run_check(check, cwd)
    return report is not None and report["status"] == EXPECTED[exit_code]


def fixture_proof(lesson, fixtures=None):
    fixtures = fixtures or os.path.join(lesson, "fixtures")
    check = os.path.join(lesson, "check.py")
    return (check_holds(check, os.path.join(fixtures, "bad"), 1)
            and check_holds(check, os.path.join(fixtures, "good"), 0))


def generated_proof(lesson):
    sides = runpy.run_path(os.path.join(lesson, "fixtures.py"))["FIXTURES"]
    with tempfile.TemporaryDirectory() as fixtures:
        for side in ("bad", "good"):
            for path, text in sides[side].items():
                target = os.path.join(fixtures, side, path)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "w", encoding="utf-8", newline="") as stream:
                    stream.write(text)
        return fixture_proof(lesson, fixtures)


def repository_proof(lesson):
    with open(os.path.join(lesson, "proof.json")) as stream:
        proof = json.load(stream)
    check = os.path.join(lesson, "check.py")
    with open(check, "rb") as stream:
        if hashlib.sha256(stream.read()).hexdigest() != proof["check_sha256"]:
            return False
    with tempfile.TemporaryDirectory() as checkout:
        def git(*args):
            subprocess.run(["git", *args], cwd=checkout, check=True, capture_output=True)

        git("init", "-q")
        for side in ("before", "after"):
            git("fetch", "-q", "--depth", "1", proof["repo"], proof[side]["commit"])
            git("checkout", "-q", "--force", "FETCH_HEAD")
            git("clean", "-qfdx")
            if not check_holds(check, checkout, proof[side]["exit"]):
                return False
    return True


def proof_kind(lesson):
    if os.path.isdir(os.path.join(lesson, "fixtures")):
        return fixture_proof
    if os.path.isfile(os.path.join(lesson, "fixtures.py")):
        return generated_proof
    if os.path.isfile(os.path.join(lesson, "proof.json")):
        return repository_proof
    return None


def main(slugs):
    slugs = slugs or sorted(name for name in os.listdir(LIBRARY)
                            if os.path.isfile(os.path.join(LIBRARY, name, "rule.md")))
    failed = 0
    for slug in slugs:
        lesson = os.path.join(LIBRARY, slug)
        if not os.path.isfile(os.path.join(lesson, "rule.md")):
            print("UNKNOWN", slug)
            failed += 1
            continue
        prove = proof_kind(lesson)
        if prove is None:
            print("JUDGED", slug)
            continue
        proofs = [prove]
        if prove is not repository_proof and os.path.isfile(os.path.join(lesson, "proof.json")):
            proofs.append(repository_proof)
        try:
            ok = all(proof(lesson) for proof in proofs)
        except (OSError, subprocess.CalledProcessError, Incomplete):
            print("INCOMPLETE", slug)
            failed += 1
            continue
        failed += not ok
        print("PASS" if ok else "FAIL", slug)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
