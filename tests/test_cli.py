import concurrent.futures
import contextlib
import fnmatch
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid


CLI = Path(__file__).resolve().parents[1] / "coldfix.py"
LOOPBACK = socket.inet_ntoa(socket.INADDR_LOOPBACK.to_bytes(4, "big"))
INSTALLED_ENTRY = ("import os, sys; sys.argv[0] = sys.argv.pop(1); sys.path[0] = os.path.dirname(sys.argv[0]); "
                   "import coldfix; sys.exit(coldfix.main())")
LESSON_CANARY_SHARDS = {"phone": {"phone"}, "hostname": {"hostname"}, "ip": {"ip"},
                        "accounts": {"email", "credential"}}


def run_main(args, cwd, stdin=None, env=None):
    if CLI.name != "coldfix.py" or threading.current_thread() is not threading.main_thread():
        entry = ["-c", INSTALLED_ENTRY] if CLI.name == "coldfix.py" else []
        return subprocess.run([sys.executable, *entry, str(CLI), *args], cwd=cwd, input=stdin, text=True,
                              capture_output=True, env=env)
    import coldfix
    stdout, stderr = io.StringIO(), io.StringIO()
    previous = os.getcwd()
    with patch.dict(os.environ, env or {}, clear=env is not None), patch.object(sys, "argv", [str(CLI), *args]), \
            patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO((stdin or "").encode()), encoding="utf-8")), \
            contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        os.chdir(cwd)
        try:
            code = coldfix.main(list(args))
        except SystemExit as error:
            code = error.code
            if not isinstance(code, int) and code is not None:
                print(code, file=sys.stderr)
                code = 1
        finally:
            os.chdir(previous)
    return subprocess.CompletedProcess(args, code or 0, stdout.getvalue(), stderr.getvalue())


class CliCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.cwd = Path(self.temporary.name)
        self.store = self.cwd / ".coldfix"
        isolated = patch.dict(os.environ, COLDFIX_PRIVATE_HOSTS=str(self.cwd / "no-private-hosts"))
        isolated.start()
        self.addCleanup(isolated.stop)

    def run_cli(self, *args, stdin=None, code=0):
        result = run_main(args, self.cwd, stdin=stdin)
        self.assertEqual(result.returncode, code, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return result

    def capture(self, *, command="python3 -m unittest", error="AssertionError: expected 2", agent="codex", thread=""):
        result = self.run_cli("capture", "--command", command, "--repo", "example", "--agent", agent,
                              "--thread", thread, stdin=error)
        return json.loads(result.stdout)

    def recurring(self):
        first = self.capture()
        second = self.capture(agent="other", thread="thread:example")
        self.assertNotEqual(first["failure_id"], second["failure_id"])
        self.assertEqual(first["group_id"], second["group_id"])
        return first["group_id"], sorted([first["failure_id"], second["failure_id"]])

    def proposal(self, group, kind="test", rule="Check the expected count before accepting a change.", check="python3 -m unittest"):
        result = self.run_cli("propose", "--group", group, "--kind", kind, "--title", "Verify the count",
                              "--rule", rule, "--check-command", check)
        return json.loads(result.stdout)["proposal_id"]

    def export(self, proposal, output="rules/count-check.md", **kwargs):
        return self.run_cli("export", "--proposal", proposal, "--reviewed", "--output", output, **kwargs)

    def event(self, **changes):
        event = dict(command="python3 -m unittest", error="AssertionError: expected 2", repo="example",
                     agent="worker", kind="test", source_ref="thread:private-example",
                     occurred="2026-10-20T12:34:56Z")
        event.update(changes)
        return event

    def collector_evidence(self):
        return dict(status="PASS", window=dict(since="2026-10-20T00:00:00+00:00",
                                               until="2026-10-27T00:00:00+00:00", days=7),
                    repos_scanned=1, errors=[],
                    scope=[dict(kind="scope", repo="example", date="2026-10-20", path="app.py",
                                detail="Unrequested file changed", url="https://example.invalid/run/1", head="abc", branch="feature")],
                    codex=[dict(repo="example", pr=1, date="2026-10-20", severity="P1", path="app.py",
                                title="Check the count", detail="The count is wrong", url="https://example.invalid/review/1")],
                    ci=[dict(repo="example", workflow="verify", title="Check changes", branch="feature", event="push",
                             date="2026-10-21", failed=["tests: count check"], url="https://example.invalid/run/2",
                             count=9, dates=["2026-10-21", "2026-10-25"])],
                    verify=[dict(repo="example", date="2026-10-22", session="session1", source="codex-session",
                                 cmd="python3 -m unittest", exit="1", error="AssertionError: expected 2",
                                 count=12, dates=["2026-10-22", "2026-10-26"])],
                    corrections=[dict(repo="example", date="2026-10-23", session="session2", text="Do not skip verification")],
                    coldpass=[dict(repo="example", target="example", fault_owner="repo", status="fixed",
                                   date="2026-10-24", reason="Missing count check", fix_commits=["abc"],
                                   verification_passed=True, source="coldpass-fix", fix_id="fix1", untriaged=False),
                              dict(repo="coldpass", target="example", source="coldpass-run", run_id="run1",
                                   fault_owner="test", status="fail", date="2026-10-25", reason="Fixture failed")],
                    coldpass_untriaged=[dict(repo=None, target=None, fault_owner="unknown", status="pending",
                                            date="2026-10-26", reason="Needs diagnosis", fix_commits=[],
                                            verification_passed=None, source="coldpass-fix", fix_id="fix2", untriaged=True)])

    def privacy_canaries(self):
        return {
            "email": ("email", "example.person@company.engineering"),
            "email_sentence": ("email", "Contact example@company.engineering."),
            "markdown_email": ("email", "Contact _example@example.com_ for help"),
            "markdown_hostname": ("hostname", "Connect to _db.example.com_ for help"),
            "markdown_phone": ("phone", "Call _020 7946 0958_"),
            "markdown_ip": ("ip", "Connect to _100.99.88.77_"),
            "identifier_ipv4": ("ip", "server_192.168.1.1"),
            "identifier_ipv6": ("ip", "db_2001:db8::1"),
            "identifier_phone": ("phone", "office_020 7946 0958"),
            "identifier_hostname": ("hostname", "server_node-01"),
            "hostname_suffix": ("hostname", "db.example.com_backup"),
            "email_suffix": ("email", "example@example.com_suffix"),
            "identifier_token": ("credential", "prefix_ghp_" + "A" * 24),
            "markdown_token": ("credential", "_ghp_" + "ExampleOnlyCanary1234567890_"),
            "phone": ("phone", "+1 (202) 555-0147"),
            "compact_phone": ("phone", "(202)5550147"),
            "national_phone": ("phone", "Call 020 7946 0958"),
            "international_phone": ("phone", "Call 0044 20 7946 0958"),
            "local_phone": ("phone", "Call 555-0147"),
            "slash_phone": ("phone", "Call 030/12345678"),
            "typographic_phone": ("phone", "Call 202–555–0147"),
            "tab_phone": ("phone", "Call 020\t7946\t0958"),
            "tailscale": ("ip", "100.99.88.77"),
            "ipv4": ("ip", "192.0.2.123"),
            "ipv4_sentence": ("ip", "Connect to 192.0.2.123."),
            "ipv6": ("ip", "2001:db8::1234"),
            "ipv6_colon": ("ip", "Address 2001:db8::1234:"),
            "ipv6_mapped": ("ip", "::ffff:192.0.2.123"),
            "hostname": ("hostname", "example-node-47"),
            "hostname_io": ("hostname", "database.example.io"),
            "hostname_sentence": ("hostname", "Connect to database.example.com."),
            "hostname_other_suffix": ("hostname", "database.example.engineering"),
            "hostname_two_labels": ("hostname", "example.engineering"),
            "hostname_url": ("hostname", "https://example.engineering/path"),
            "hostname_punycode": ("hostname", "example.xn--p1ai"),
            "hostname_unicode": ("hostname", "пример.рф"),
            "tailnet_host": ("hostname", "example-node.example.ts.net"),
            "home": ("home_path", "/home/example/private"),
            "root_home": ("home_path", "/root/example-private"),
            "named_home": ("home_path", "~example/private"),
            "windows_home": ("home_path", "C:\\Users\\user\\private"),
            "token": ("credential", "ghp_" + "ExampleOnlyCanary1234567890"),
            "key": ("credential", "-----BEGIN " + "PRIVATE KEY-----"),
            "entropy": ("credential", "aB" + "3dE6gH9jK2mN5pQ8sT1vW4yZ7cF0iL"),
            "git_author": ("git_author", "Example Canary Author"),
            "private_repo": ("private_repo", "example-private-project"),
            "church": ("denylist", "Example Congregation Canary"),
            "person": ("denylist", "Example Person Canary"),
            "quoted_person": ("denylist", "Example O'Canary"),
        }

    def privacy_inputs(self):
        denylist = self.cwd / "private-terms.json"
        denylist.write_text(json.dumps({"categories": {
            "church": {"terms": ["Example Congregation Canary"]},
            "people": ["Example Person Canary", "Example O'Canary"],
        }}))
        repo = self.cwd / "author-history"
        repo.mkdir()
        for args in (("init", "--quiet"), ("-c", "user.name=Example Canary Author", "-c",
                    "user.email=example-author", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
                    "commit", "--quiet", "--allow-empty", "-m", "Example author fixture")):
            subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)
        self.run_cli("ingest", stdin=json.dumps(self.event(repo="https://example.invalid/example-owner/example-private-project.git?ref=main")))
        return ["--denylist", str(denylist), "--git-repo", str(repo)]

    def assert_privacy_block(self, args, output, canary, category=None):
        for existing in (False, True):
            if existing:
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text("preserve existing output")
            result = self.run_cli(*args, code=2)
            self.assertIn("privacy screen blocked", result.stderr)
            if category is not None:
                counts = json.loads(result.stderr.partition("privacy screen blocked: ")[2])
                self.assertGreater(counts.get(category, 0), 0)
            self.assertNotIn(canary, result.stdout + result.stderr)
            self.assertEqual(result.stdout, "")
            if existing:
                self.assertEqual(output.read_text(), "preserve existing output")
                output.unlink()
            else:
                self.assertFalse(output.exists())

    def export_with_privacy_source(self, proposal, *source):
        result = self.run_cli("export", "--proposal", proposal, "--reviewed", "--output", "source.md", *source, code=2)
        self.assertFalse((self.cwd / "source.md").exists())
        self.assertNotIn(str(self.cwd), result.stdout + result.stderr)

    def public_inputs(self, mode="pass"):
        source = self.cwd / "gate-source"
        if not source.exists():
            source.mkdir()
            for args in (("init", "--quiet"), ("-c", "user.name=Stage Author", "-c", "user.email=stage-author",
                         "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "commit", "--quiet",
                         "--allow-empty", "-m", "Gate source")):
                subprocess.run(["git", "-C", str(source), *args], capture_output=True, check=True)
        denylist = self.cwd / "gate-terms.json"
        denylist.write_text(json.dumps(["example-private-gate-term"]))
        gate = self.cwd / "gate.py"
        gate.write_text('''import argparse, hashlib, json, subprocess, sys
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument('repo', type=Path)
p.add_argument('--denylist', type=Path, required=True)
p.add_argument('--reports', type=Path, required=True)
p.add_argument('--verify', action='store_true', required=True)
p.add_argument('--private-repo', action='append', default=[])
p.add_argument('--manifest', type=Path)
a = p.parse_args()
candidate, = (a.repo / 'exports').glob('*.md')
raw = candidate.read_bytes()
committed = subprocess.run(['git', '-C', str(a.repo), 'show', 'HEAD:exports/' + candidate.name], check=True, capture_output=True).stdout
assert raw == committed
assert 'example' in a.private_repo
Path(__file__).with_suffix('.seen').write_text(hashlib.sha256(raw).hexdigest())
mode = MODE
print('example-private-subprocess-canary', file=sys.stderr)
report = dict(version='fixture', outcome='PASS', ready=True, reasons=[], counts={}, findings=[],
              verification=dict(status='pass', commands=[dict(role='quickstart', exit_code=0), dict(role='tests', exit_code=0)],
                                dependencies=dict(blocked=0)), license=dict(status='pass', dependencies='stdlib-only'),
              stats=dict(tree_files=1, history_commits=1))
if mode == 'incomplete': report['verification']['status'] = 'not-run'
if mode == 'false-pass': report['findings'] = [dict(reviewed=False)]
if mode == 'missing-field': del report['stats']
if mode == 'no-commands': report['verification']['commands'] = []
if mode == 'blocked-dependencies': report['verification']['dependencies']['blocked'] = 1
if mode == 'bad-count': report['counts'] = dict(personal=True)
if mode == 'changed': candidate.write_text('example changed candidate')
if mode == 'symlink':
    candidate.unlink()
    candidate.symlink_to(Path(__file__))
destination = a.reports / 'candidate' / 'run' / 'report.json'
destination.parent.mkdir(parents=True)
if mode != 'summary-only': destination.write_text(json.dumps(report) if mode != 'bad-report' else '{}')
summary = dict(outcome='PASS', ready=True, counts=report['counts'], report='candidate/run/report.json')
if mode == 'failed': summary['outcome'] = 'FAIL'
if mode == 'traversal': summary['report'] = '../report.json'
print(json.dumps(summary) if mode != 'malformed' else 'example-private-subprocess-canary')
sys.exit(1 if mode == 'nonzero' else 0)
'''.replace('MODE', repr(mode)))
        return ["--public", "--release-check", str(gate), "--release-check-repo", str(source), "--denylist", str(denylist)]

    def lesson_record(self, revision):
        return json.loads((self.store / "lessons" / (revision + ".json")).read_text())

    def author_lesson(self, script=None, *, repos=("example-one", "example-two"), level="test"):
        for repo in repos:
            imported = json.loads(self.run_cli("ingest", stdin=json.dumps(self.event(repo=repo, error="count mismatch private-evidence-canary", source_ref="thread:" + os.urandom(4).hex()))).stdout)
        self.run_cli("group")
        group = next(row for row in json.loads((self.store / "groups.json").read_text())["groups"]
                     if imported["failure_ids"][0] in row["members"])
        check = self.cwd / "authored-check.py"
        check.write_text(script if script is not None else
                         "import json, sys\nfrom pathlib import Path\n"
                         "ok = Path('count.txt').read_text() == '2'\n"
                         "print(json.dumps({'status': 'PASS'} if ok else {'status': 'FAIL', 'failure': 'count-mismatch'}))\n"
                         "sys.exit(0 if ok else 1)\n")
        result = self.run_cli("lesson", "--group", group["signature"], "--kind", "test", "--level", level,
                              "--title", "Verify the count", "--rule", "Require the expected count.",
                              "--failure", "The count did not match the expected value.", "--fix", "Set the count to its expected value.",
                              "--check-command", "python3 {check}", "--check-file", str(check),
                              "--expected-failure", "count-mismatch")
        return json.loads(result.stdout)["id"]

    def history(self, name="history"):
        repo = self.cwd / name
        repo.mkdir()
        def git(*args):
            return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()
        git("init", "--quiet")
        git("config", "user.name", "Example")
        git("config", "user.email", "example@example.invalid")
        (repo / "count.txt").write_text("1")
        (repo / "changing-check.py").write_text("raise RuntimeError('before check must not run')")
        git("add", ".")
        git("commit", "--quiet", "-m", "Example failure in " + name)
        before = git("rev-parse", "HEAD")
        (repo / "count.txt").write_text("2")
        (repo / "changing-check.py").write_text("raise RuntimeError('after check must not run')")
        git("add", ".")
        git("commit", "--quiet", "-m", "Example fix")
        after = git("rev-parse", "HEAD")
        (repo / "unfamiliar.txt").write_text("preserve this")
        (repo / "count.txt").write_text("uncommitted")
        return repo, before, after

    def run_replay(self, revision, repo, before, after, *extra, code=0):
        return self.run_cli("replay", "--lesson", revision, "--repo", str(repo),
                            "--before", before, "--after", after, *extra, code=code)

    def prove_elsewhere(self, revision, *extra):
        if not (self.cwd / "second-history").exists():
            self.second_history = self.history("second-history")
        return json.loads(self.run_replay(revision, *self.second_history, *extra).stdout)["id"]

    def agent_rule(self, fixture, name):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "coldfix-test", "version": "1.0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "hub_list_rules", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
                "name": "hub_get_item", "arguments": {"kind": "rule", "name": name}}},
        ]
        result = subprocess.run([sys.executable, str(fixture / "mcp_endpoint.py")], cwd=fixture,
                                input="".join(json.dumps(request) + "\n" for request in requests),
                                text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([response["id"] for response in responses], [1, 2, 3, 4])
        for response in responses:
            self.assertEqual(response["jsonrpc"], "2.0")
            self.assertNotIn("error", response)
        initialized, tools, listed, served = [response["result"] for response in responses]
        self.assertEqual(initialized["protocolVersion"], "2024-11-05")
        self.assertIn("tools", initialized["capabilities"])
        advertised = {tool["name"] for tool in tools["tools"]}
        self.assertTrue({"hub_list_rules", "hub_get_item"} <= advertised)
        values = []
        for response in (listed, served):
            self.assertIs(response["isError"], False)
            content, = response["content"]
            self.assertEqual(content["type"], "text")
            values.append(json.loads(content["text"]))
        return values

    def assert_lesson_canaries_blocked(self, shard):
        sharded = set().union(*LESSON_CANARY_SHARDS.values())
        cases = {case: value for case, value in self.privacy_canaries().items()
                 if value[0] in LESSON_CANARY_SHARDS.get(shard, ()) or shard == "other" and value[0] not in sharded}
        self.assertTrue(cases)
        repo, before, after = self.history()
        inputs = self.privacy_inputs()
        for case, (category, canary) in cases.items():
            with self.subTest(case=case):
                revision = self.author_lesson()
                script = self.lesson_record(revision)["check_script"] + "# " + canary + "\n"
                check = self.cwd / "private-check.py"
                check.write_text(script)
                revision = json.loads(self.run_cli("lesson", "--revise", revision, "--check-file", str(check)).stdout)["id"]
                proven = json.loads(self.run_replay(revision, repo, before, after, "--status", "active").stdout)["id"]
                output = self.cwd / "privacy/lesson.md"
                self.assert_privacy_block(["export", "--lesson", proven, "--reviewed", "--output", str(output), *inputs], output, canary, category)
                if case not in ("token", "key"):
                    revision = json.loads(self.run_cli("lesson", "--revise", proven, "--rule", "Check " + canary).stdout)["id"]
                    proven = json.loads(self.run_replay(revision, repo, before, after, "--status", "active").stdout)["id"]
                    proven = self.prove_elsewhere(proven)
                    output = self.cwd / "privacy/briefing.md"
                    self.assert_privacy_block(["export", "--format", "briefing", "--reviewed", "--output", str(output), *inputs], output, canary, category)
                self.run_cli("lesson", "--revise", proven, "--status", "retired", "--rule", "Check expected counts.")


class CliTests(CliCase):
    def test_doctor_confirms_the_replay_sandbox_runs_here(self):
        import coldfix as c
        with patch.dict(os.environ):
            os.environ.pop("COLDFIX_FLEET_CLI", None)
            self.assertEqual(json.loads(self.run_cli("doctor").stdout),
                             {"sandbox": "available", "fleet_cli": "off: set COLDFIX_FLEET_CLI to the command that " + c.FLEET_CLI_USE})
        tools = self.cwd / "tools"
        tools.mkdir()
        (tools / "acme").write_text("#!/bin/sh\n")
        (tools / "acme").chmod(0o755)
        with patch.dict(os.environ, {"COLDFIX_FLEET_CLI": "acme", "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}"}):
            self.assertEqual(json.loads(self.run_cli("doctor").stdout)["fleet_cli"], "acme: " + c.FLEET_CLI_USE)

    def test_doctor_without_bwrap_names_the_missing_prerequisite_and_the_fix(self):
        result = subprocess.run([sys.executable, str(CLI), "doctor"], cwd=self.cwd, capture_output=True, text=True,
                                env=dict(os.environ, PATH=str(self.cwd)))
        self.assertEqual((result.returncode, result.stdout), (2, ""))
        self.assertTrue(result.stderr.startswith("coldfix: sandbox unavailable: bwrap is not installed; "
                                                 "install bubblewrap (sudo apt install bubblewrap)"), result.stderr)

    def test_ingest_v2_mixed_v1_store_and_existing_proposal_export(self):
        first = self.capture()
        original_path = self.store / "failures" / (first["failure_id"] + ".json")
        original = original_path.read_bytes()
        self.assertEqual(json.loads(original)["version"], 1)
        result = self.run_cli("ingest", stdin=json.dumps(self.event()) + "\n")
        imported = json.loads(result.stdout)
        self.assertEqual(imported["imported_records"], 1)
        record = json.loads((self.store / "failures" / (imported["failure_ids"][0] + ".json")).read_text())
        self.assertEqual(record["version"], 2)
        self.assertEqual(record["group_id"], "92c69faf0a1dae7910661e0ebe79a4bb635db866bf3bdfdedb3d8ff40b1362e1")
        self.assertEqual(record["signature"], first["group_id"])
        for key, value in self.event().items():
            self.assertEqual(record[key], value)
        self.assertEqual(record["source"], "stdin")
        self.assertEqual(record["thread"], "")
        self.assertEqual(original_path.read_bytes(), original)
        groups = json.loads(self.run_cli("propose").stdout)["groups"]
        self.assertEqual([(row["group_id"], row["count"]) for row in groups], [(first["group_id"], 2)])
        proposal = self.proposal(first["group_id"])
        self.export(proposal)
        self.assertIn("coldfix:failure:" + imported["failure_ids"][0], (self.cwd / "rules/count-check.md").read_text())

    def test_group_real_derived_repeats_preserve_exact_proposals_and_legacy_records(self):
        fixture = json.loads((CLI.parent / "tests/fixtures/repeated-failures.json").read_text())
        first, second = fixture["events"]
        captured = json.loads(self.run_cli("capture", "--command", first["command"], "--repo", first["repo"],
                                           "--agent", first["agent"], stdin=first["error"]).stdout)
        legacy_path = self.store / "failures" / (captured["failure_id"] + ".json")
        legacy = json.loads(legacy_path.read_text())
        legacy["created"] = "2026-10-22T12:00:00+00:00"
        legacy_path.write_text(json.dumps(legacy))
        imported = json.loads(self.run_cli("ingest", stdin="\n".join(json.dumps(row) for row in (first, second))).stdout)
        originals = {p: p.read_bytes() for p in (self.store / "failures").glob("*.json")}
        exact = json.loads(self.run_cli("propose").stdout)["groups"]
        self.assertEqual(sorted(row["count"] for row in exact), [1, 2])
        proposal = self.proposal(captured["group_id"])
        result = self.run_cli("group")
        self.assertEqual(json.loads(result.stdout), {"groups": 1, "members": 3})
        self.assertNotIn("example-one", result.stdout)
        snapshot = json.loads((self.store / "groups.json").read_text())
        self.assertEqual(snapshot["version"], 1)
        row, = snapshot["groups"]
        self.assertRegex(row.pop("signature"), r"^[0-9a-f]{64}$")
        legacy = json.loads(originals[self.store / "failures" / (captured["failure_id"] + ".json")])
        self.assertEqual(row, dict(members=sorted([captured["failure_id"], *imported["failure_ids"]]),
                                   repos=["example-one", "example-two"], first_seen="2026-10-20", last_seen=legacy["created"]))
        self.assertEqual({p: p.read_bytes() for p in originals}, originals)
        self.assertEqual(json.loads(self.run_cli("propose").stdout)["groups"], exact)
        self.export(proposal)
        self.assertNotIn("FileNotFoundError", (self.cwd / "rules/count-check.md").read_text())

    def test_group_normalizes_variable_details_in_command_and_error(self):
        pairs = [
            ("python /srv/one/app.py", "python /opt/two/main.py"),
            (r"python C:\work\one\app.py", r"python D:\work\two\main.py"),
            ('python "/srv/example one/app.py"', 'python "/opt/example two/main.py"'),
            ("src/one.py:12:8: TypeError", "lib/two.py:91:3: TypeError"),
            ("app.py:12:8: TypeError", "main.py:91:3: TypeError"),
            ("src/app.ts(12,8): error TS2345", "lib/main.ts(91,3): error TS2345"),
            ("line 12, column 8", "line 91, column 3"),
            ('File "app.py", line 12', 'File "main.py", line 91'),
            ("2026-10-20T12:30:00.123Z failed", "2026-10-21T15:12:32+02:00 failed"),
            ("12:30:00 failed", "15:12:32 failed"),
            ("request deadbeef at 0x1234", "request abc123def at 0x5678"),
            ("request 123e4567-e89b-12d3-a456-426614174000", "request 123e4567-e89b-12d3-a456-426614174001"),
            ("listen localhost:3000 port 3000", "listen localhost:8080 port 8080"),
            (f"listen {LOOPBACK}:3000 [::1]:3000", f"listen {LOOPBACK}:8080 [::1]:8080"),
            ("tmp-abc123 pytest-3 failed", "tmp-xyz789 pytest-12 failed"),
        ]
        for index, (first, second) in enumerate(pairs):
            events = [self.event(command=f"check {index} {value}", error=f"failure {index} {value}", repo=f"example-{n}")
                      for n, value in enumerate((first, second))]
            self.run_cli("ingest", stdin="\n".join(json.dumps(row) for row in events))
        self.assertEqual(json.loads(self.run_cli("group").stdout), {"groups": len(pairs), "members": len(pairs) * 2})
        groups = json.loads((self.store / "groups.json").read_text())["groups"]
        self.assertTrue(all(len(row["members"]) == 2 for row in groups))

    def test_group_preserves_meaningful_numbers_errors_and_commands_without_truncation(self):
        errors = ["exit 1", "exit 2", "HTTP 404", "HTTP 500", "error TS2345", "error TS2322",
                  "[Errno 2] missing", "[Errno 13] denied", "expected 2 got 3", "expected 3 got 2",
                  "TypeError", "ValueError", "x" * 20000 + "first", "x" * 20000 + "second"]
        events = [self.event(error=error) for error in errors]
        events += [self.event(command="pytest"), self.event(command="unittest")]
        self.run_cli("ingest", stdin="\n".join(json.dumps(row) for row in events))
        self.assertEqual(json.loads(self.run_cli("group").stdout), {"groups": 16, "members": 16})

    def test_group_rebuild_is_deterministic_and_replaces_stale_members_and_corrupt_cache(self):
        first = self.capture()
        snapshot = self.store / "groups.json"
        self.run_cli("group")
        original = snapshot.read_bytes()
        self.run_cli("group")
        self.assertEqual(snapshot.read_bytes(), original)
        later = self.capture()
        self.assertEqual(snapshot.read_bytes(), original)
        self.assertEqual(json.loads(self.run_cli("group").stdout), {"groups": 1, "members": 2})
        row, = json.loads(snapshot.read_text())["groups"]
        self.assertEqual(row["members"], sorted([first["failure_id"], later["failure_id"]]))
        (self.store / "failures" / (first["failure_id"] + ".json")).unlink()
        snapshot.write_text("corrupt private-canary cache")
        self.capture(command="other")
        self.run_cli("ingest", stdin="")
        self.run_cli("propose")
        self.assertEqual(json.loads(self.run_cli("group").stdout), {"groups": 2, "members": 2})
        self.assertNotIn(first["failure_id"], snapshot.read_text())
        snapshot.unlink()
        self.run_cli("group")
        self.assertEqual(json.loads(snapshot.read_text())["version"], 1)

    def test_group_range_compares_offsets_and_preserves_dates_and_v1_created(self):
        captured = self.capture()
        path = self.store / "failures" / (captured["failure_id"] + ".json")
        record = json.loads(path.read_text())
        record["created"] = "2026-10-20 23:00:00-02:00"
        path.write_text(json.dumps(record))
        dates = ["2026-10-21T03:00:00+04:00", "2026-10-20T23:30:00Z", "2026-10-21"]
        self.run_cli("ingest", stdin="\n".join(json.dumps(self.event(occurred=value)) for value in dates))
        self.run_cli("group")
        row, = json.loads((self.store / "groups.json").read_text())["groups"]
        self.assertEqual((row["first_seen"], row["last_seen"]), (dates[0], record["created"]))
        self.run_cli("ingest", stdin=json.dumps(self.event(occurred="2026-10-19", source_ref="thread:earlier-occurrence")))
        self.run_cli("group")
        row, = json.loads((self.store / "groups.json").read_text())["groups"]
        self.assertEqual(row["first_seen"], "2026-10-19")

    def test_group_corrupt_events_block_publish_and_keep_old_exact_signature_validation(self):
        self.run_cli("ingest", stdin=json.dumps(self.event()))
        self.run_cli("group")
        snapshot = self.store / "groups.json"
        original = snapshot.read_bytes()
        path, = (self.store / "failures").glob("*.json")
        record = json.loads(path.read_text())
        for changes in (dict(signature="0" * 64), dict(group_id="0" * 64), dict(error="changed")):
            with self.subTest(changes=changes):
                path.write_text(json.dumps(dict(record, **changes)))
                result = self.run_cli("group", code=2)
                self.assertIn("corrupt store record", result.stderr)
                self.assertEqual(snapshot.read_bytes(), original)

    def test_group_atomic_publish_failure_preserves_snapshot_events_and_permissions(self):
        self.capture()
        self.run_cli("group")
        snapshot = self.store / "groups.json"
        original = snapshot.read_bytes()
        self.capture()
        spec = importlib.util.spec_from_file_location("coldfix", CLI)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        root, store = module.load_store(self.store)
        records = {p: p.read_bytes() for p in (root / "failures").glob("*.json")}
        for operation in ("fsync", "replace"):
            with self.subTest(operation=operation), patch.object(module.os, operation, side_effect=OSError("interrupted")):
                with self.assertRaises(OSError):
                    module.group(None, root, store)
            self.assertEqual(snapshot.read_bytes(), original)
            self.assertEqual({p: p.read_bytes() for p in records}, records)
            self.assertEqual(list(root.glob(".pending-*")), [])
        self.run_cli("group")
        self.assertEqual(json.loads(snapshot.read_text())["groups"][0]["members"], sorted(row["id"] for row in store["failures"].values()))
        self.assertEqual(root.stat().st_mode & 0o777, 0o700)
        self.assertEqual((root / "failures").stat().st_mode & 0o777, 0o700)
        self.assertEqual({p.stat().st_mode & 0o777 for p in root.rglob("*.json")}, {0o600})

    def test_group_empty_snapshot_and_symlink_are_safe(self):
        self.assertEqual(json.loads(self.run_cli("group").stdout), {"groups": 0, "members": 0})
        snapshot = self.store / "groups.json"
        self.assertEqual(json.loads(snapshot.read_text()), {"version": 1, "groups": []})
        snapshot.unlink()
        target = self.cwd / "private-canary"
        target.write_text("untouched")
        snapshot.symlink_to(target)
        result = self.run_cli("group", code=2)
        self.assertNotIn("private-canary", result.stderr)
        self.assertTrue(snapshot.is_symlink())
        self.assertEqual(target.read_text(), "untouched")

    def test_ingest_preserves_unicode_line_characters_inside_json_strings(self):
        error = "first\u0085second\u2028third\u2029last"
        event = self.event(error=error)
        receipt = json.loads(self.run_cli("ingest", stdin=json.dumps(event, ensure_ascii=False) + "\n").stdout)
        path = self.store / "failures" / (receipt["failure_ids"][0] + ".json")
        self.assertEqual(json.loads(path.read_text())["error"], error)

    def test_ingest_all_kinds_from_file_preserves_metadata_and_private_permissions(self):
        kinds = ("test", "ci", "review", "correction", "wrong_fix", "stuck", "revert", "issue")
        events = [self.event(kind=kind, occurred="2026-10-20", thread="private-session") for kind in kinds]
        path = self.cwd / "events.jsonl"
        path.write_text("\n".join(json.dumps(row) for row in events))
        result = self.run_cli("ingest", "--input", str(path))
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["imported_records"], 8)
        records = [json.loads((self.store / "failures" / (ref + ".json")).read_text()) for ref in receipt["failure_ids"]]
        self.assertEqual([row["kind"] for row in records], list(kinds))
        self.assertTrue(all(row["source"] == str(path) and row["thread"] == "private-session" for row in records))
        self.assertEqual(self.store.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.store / "failures").stat().st_mode & 0o777, 0o700)
        self.assertEqual({p.stat().st_mode & 0o777 for p in self.store.rglob("*.json")}, {0o600})
        group = records[0]["group_id"]
        self.assertTrue(all(row["group_id"] == group for row in records))
        self.assertNotIn("private-session", result.stdout + result.stderr)

    def test_ingest_malformed_later_row_validates_whole_batch_before_writes(self):
        invalid = ["{private-canary", "[]", "[" * 2000 + "]" * 2000, '{"kind":"test","kind":"ci"}',
                   json.dumps(self.event(occurred="2026-02-31")),
                   json.dumps(self.event(occurred="2026-10-20T12:00:00")),
                   json.dumps(self.event(occurred=True)), json.dumps(self.event(kind=[])),
                   json.dumps(self.event(source_ref="")), json.dumps(self.event(command=None)),
                   json.dumps(self.event(signature="private-canary")), json.dumps(self.event(error="\x00private-canary"))]
        for row in invalid:
            with self.subTest(row=row):
                result = self.run_cli("ingest", stdin=json.dumps(self.event()) + "\n" + row, code=2)
                self.assertIn("line 2", result.stderr)
                self.assertNotIn("private-canary", result.stdout + result.stderr)
                self.assertFalse(self.store.exists())
        first = self.capture()
        snapshot = {p: p.read_bytes() for p in self.store.rglob("*.json")}
        self.run_cli("ingest", stdin=json.dumps(self.event()) + "\n{}", code=2)
        self.assertEqual({p: p.read_bytes() for p in self.store.rglob("*.json")}, snapshot)
        self.assertEqual(json.loads(self.run_cli("ingest", stdin="\n").stdout), {"imported_records": 0, "skipped_duplicates": 0, "failure_ids": []})
        self.assertEqual(json.loads(self.run_cli("propose").stdout)["groups"][0]["evidence"], [first["failure_id"]])

    def test_agentfleet_adapter_imports_sections_once_with_provenance_and_date_precision(self):
        evidence = self.collector_evidence()
        path = self.cwd / "evidence.json"
        path.write_text(json.dumps(evidence))
        result = self.run_cli("ingest", "--format", "agentfleet", "--input", str(path))
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["imported_records"], 8)
        records = [json.loads((self.store / "failures" / (ref + ".json")).read_text()) for ref in receipt["failure_ids"]]
        self.assertEqual([row["kind"] for row in records], ["correction", "review", "ci", "test", "correction", "test", "test", "test"])
        refs = [(section, index) for section in ("scope", "codex", "ci", "verify", "corrections", "coldpass", "coldpass_untriaged")
                for index in range(len(evidence[section]))]
        for record, (section, index) in zip(records, refs):
            self.assertEqual(record["source_ref"], f"{path}#/{section}/{index}")
            self.assertEqual(record["occurred"], evidence[section][index]["date"])
            self.assertEqual(record["signature"], record["group_id"])
        self.assertEqual(records[1]["error"], "Check the count\nThe count is wrong")
        self.assertEqual(records[2]["command"], "verify")
        self.assertEqual(records[2]["error"], "tests: count check")
        self.assertEqual(records[2]["source"], "https://example.invalid/run/2")
        self.assertEqual(records[3]["thread"], "session1")
        self.assertEqual(records[3]["source"], "codex-session")
        self.assertEqual(records[-1]["repo"], "unknown")
        groups = json.loads(self.run_cli("propose").stdout)["groups"]
        self.assertEqual(sum(group["count"] for group in groups), 8)
        self.assertTrue(all(group["count"] == 1 and not group["recurring"] for group in groups))
        self.assertNotIn(str(path), result.stdout + result.stderr)

    def test_agentfleet_incomplete_errors_and_malformed_later_sections_write_nothing(self):
        path = self.cwd / "private-canary.json"
        invalid = []
        for changes in (dict(status="INCOMPLETE", errors=["private-canary"]), dict(errors=["private-canary"]), dict(status="FAIL")):
            evidence = self.collector_evidence()
            evidence.update(changes)
            invalid.append(evidence)
        for section, row in (("coldpass_untriaged", {}), ("verify", dict(kind="collection-error", detail="private-canary")),
                             ("ci", dict(repo="example", date="2026-10-20", workflow="verify", failed=[None])),
                             ("corrections", dict(repo="example", date="2026-02-31", text="private-canary"))):
            evidence = self.collector_evidence()
            evidence[section].append(row)
            invalid.append(evidence)
        evidence = self.collector_evidence()
        del evidence["scope"]
        invalid.append(evidence)
        invalid.extend([[], dict(self.collector_evidence(), window=None)])
        for evidence in invalid:
            path.write_text(json.dumps(evidence))
            result = self.run_cli("ingest", "--format", "agentfleet", "--input", str(path), code=2)
            self.assertNotIn("private-canary", result.stdout + result.stderr)
            self.assertFalse(self.store.exists())
        self.run_cli("ingest", "--format", "agentfleet", stdin=json.dumps(self.collector_evidence()), code=2)
        self.assertFalse(self.store.exists())

    def test_agentfleet_empty_errors_and_reasons_use_recorded_exit_and_status(self):
        evidence = self.collector_evidence()
        evidence["verify"][0]["error"] = ""
        evidence["verify"][0].pop("source")
        evidence["coldpass"][0]["reason"] = ""
        path = self.cwd / "evidence.json"
        path.write_text(json.dumps(evidence))
        receipt = json.loads(self.run_cli("ingest", "--format", "agentfleet", "--input", str(path)).stdout)
        records = [json.loads((self.store / "failures" / (ref + ".json")).read_text()) for ref in receipt["failure_ids"]]
        self.assertEqual(records[3]["error"], "exit 1")
        self.assertEqual(records[3]["source"], "agentfleet:verify")
        self.assertEqual(records[5]["error"], "status: fixed")

    def test_ingest_metadata_never_exports_and_credential_screen_includes_source_ref(self):
        event = self.event(source_ref="https://example.invalid/?token=private-canary", thread="raw-thread-canary",
                           agent="raw-agent-canary", repo="raw-repo-canary", error="raw-error-canary")
        result = self.run_cli("ingest", stdin=(json.dumps(event) + "\n") * 2)
        self.assertNotIn("canary", result.stdout + result.stderr)
        group = json.loads(self.run_cli("propose").stdout)["groups"][0]["group_id"]
        self.run_cli("propose", "--group", group, "--kind", "test", "--title", "Safe title",
                     "--rule", "Do not repeat private-canary", "--check-command", "true", code=2)
        proposal = self.proposal(group)
        self.export(proposal)
        content = (self.cwd / "rules/count-check.md").read_text()
        self.assertNotIn("canary", content)
        self.assertNotIn(event["occurred"], content)
        self.assertNotIn(event["source_ref"], content)

    def test_corrupt_v2_stores_block_ingest_and_other_commands(self):
        receipt = json.loads(self.run_cli("ingest", stdin=json.dumps(self.event())).stdout)
        path = self.store / "failures" / (receipt["failure_ids"][0] + ".json")
        original = json.loads(path.read_text())
        invalid = [dict(original, version=True), dict(original, version=3), dict(original, signature="0" * 64),
                   dict(original, occurred="2026-02-31"), dict(original, kind="unsupported"), dict(original, source_ref=[])]
        missing = original.copy()
        del missing["signature"]
        invalid.append(missing)
        for record in invalid:
            content = json.dumps(record)
            path.write_text(content)
            for args in (("ingest",), ("propose",), ("capture", "--command", "false", "--repo", "r", "--agent", "a")):
                result = self.run_cli(*args, stdin=json.dumps(self.event()), code=2)
                self.assertIn("corrupt store record", result.stderr)
                self.assertEqual(path.read_text(), content)
                self.assertEqual(len(list((self.store / "failures").glob("*.json"))), 1)

    def test_capture_propose_export_keeps_repeats_and_provenance(self):
        group, evidence = self.recurring()
        records = [json.loads(path.read_text()) for path in sorted((self.store / "failures").glob("*.json"))]
        self.assertEqual({record["agent"] for record in records}, {"codex", "other"})
        self.assertEqual({record["thread"] for record in records}, {"", "thread:example"})
        self.assertEqual({record["source"] for record in records}, {"stdin"})
        self.assertTrue(all(record["command"] == "python3 -m unittest" for record in records))
        self.assertTrue(all(record["error"] == "AssertionError: expected 2" for record in records))
        self.assertTrue(all(record["created"].endswith("+00:00") for record in records))
        groups = json.loads(self.run_cli("propose").stdout)["groups"]
        self.assertEqual(groups, [{"group_id": group, "count": 2, "recurring": True, "evidence": evidence}])
        proposal = self.proposal(group)
        result = json.loads(self.export(proposal).stdout)
        self.assertEqual(result, {"exported": True, "proposal_id": proposal, "evidence_count": 2})
        rule = (self.cwd / "rules/count-check.md").read_text()
        self.assertIn("# Verify the count\n\nCheck the expected count", rule)
        self.assertIn("Kind: test\n\n```sh\npython3 -m unittest\n```", rule)
        self.assertIn("Coldfix has not executed or verified it.", rule)
        for ref in evidence:
            self.assertIn("coldfix:failure:" + ref, rule)
        self.assertNotIn("AssertionError", rule)
        self.assertNotIn("thread:example", rule)
        self.assertEqual(self.store.stat().st_mode & 0o777, 0o700)
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in self.store.rglob("*.json")))

    def test_log_file_and_argument_error_capture(self):
        log = self.cwd / "failure.log"
        log.write_text("failed from file\n")
        result = self.run_cli("capture", "--command", "false", "--repo", "example", "--agent", "codex", "--log-file", "failure.log")
        record_id = json.loads(result.stdout)["failure_id"]
        record = json.loads((self.store / "failures" / (record_id + ".json")).read_text())
        self.assertEqual(record["error"], "failed from file\n")
        self.assertEqual(record["source"], str(log))
        result = self.run_cli("capture", "--command", "false", "--repo", "example", "--agent", "codex", "--error", "argument failure")
        record_id = json.loads(result.stdout)["failure_id"]
        self.assertEqual(json.loads((self.store / "failures" / (record_id + ".json")).read_text())["source"], "argument")

    def test_capture_preserves_line_endings_from_files_and_stdin(self):
        error = "failure\r\nsecond line\r\n"
        log = self.cwd / "crlf.log"
        log.write_bytes(error.encode("utf-8"))
        args = ["capture", "--command", "false", "--repo", "example", "--agent", "codex"]
        file_result = json.loads(self.run_cli(*args, "--log-file", str(log)).stdout)
        stdin_result = subprocess.run([sys.executable, str(CLI), *args], cwd=self.cwd,
                                      input=error.encode("utf-8"), capture_output=True)
        self.assertEqual(stdin_result.returncode, 0)
        captured = json.loads(stdin_result.stdout)
        self.assertEqual(file_result["group_id"], captured["group_id"])
        for result in (file_result, captured):
            record = json.loads((self.store / "failures" / (result["failure_id"] + ".json")).read_text())
            self.assertEqual(record["error"], error)
        lf_result = json.loads(self.run_cli(*args, "--error", error.replace("\r\n", "\n")).stdout)
        self.assertNotEqual(file_result["group_id"], lf_result["group_id"])

    def test_exact_groups_are_deterministic_with_no_limit(self):
        self.run_cli("propose")
        self.assertFalse(self.store.exists())
        one = self.capture()
        self.capture(error="AssertionError: expected 3")
        for _ in range(10):
            self.capture()
        first = self.run_cli("propose").stdout
        self.assertEqual(first, self.run_cli("propose").stdout)
        groups = json.loads(first)["groups"]
        self.assertEqual(len(groups), 2)
        self.assertEqual(sorted(group["count"] for group in groups), [1, 11])
        self.assertEqual([group["recurring"] for group in groups if group["group_id"] == one["group_id"]], [True])

    def test_every_explicit_kind_and_rule_file(self):
        group, _ = self.recurring()
        rule = self.cwd / "authored.md"
        rule.write_text("Review counts.\nKeep the expected value explicit.\n")
        for kind in ("lint", "test", "hook"):
            with self.subTest(kind=kind):
                result = self.run_cli("propose", "--group", group, "--kind", kind, "--title", "Count check",
                                      "--rule-file", str(rule), "--check-command", "python3 -m unittest")
                proposal = json.loads(result.stdout)["proposal_id"]
                self.export(proposal, kind + ".md")
                exported = (self.cwd / (kind + ".md")).read_text()
                self.assertIn("Kind: " + kind, exported)
                self.assertIn(rule.read_text(), exported)

    def test_secret_fields_never_reach_cli_or_export(self):
        sentinel = "sensitive-value-" + "9" * 15
        command = "tool --token=" + sentinel
        error = "failure password=" + sentinel + "\nprivate raw log"
        results = []
        for _ in range(2):
            results.append(self.run_cli("capture", "--command", command, "--repo", sentinel, "--agent", sentinel,
                                        "--thread", sentinel, stdin=error))
        group = json.loads(results[0].stdout)["group_id"]
        results.append(self.run_cli("propose"))
        proposal = self.proposal(group, check="python3 -m unittest")
        results.append(self.export(proposal))
        for result in results:
            self.assertNotIn(sentinel, result.stdout + result.stderr)
            self.assertNotIn("private raw log", result.stdout + result.stderr)
        exported = (self.cwd / "rules/count-check.md").read_text()
        self.assertNotIn(sentinel, exported)
        self.assertNotIn(command, exported)
        self.assertNotIn(error, exported)
        result = self.run_cli("propose", "--group", group, "--kind", "test", "--title", "Check",
                              "--rule", "Do not publish " + sentinel, "--check-command", "true", code=2)
        self.assertIn("repeats a captured credential", result.stderr)
        self.assertNotIn(sentinel, result.stderr + result.stdout)
        self.assertEqual(len(list((self.store / "proposals").glob("*.json"))), 1)

    def test_short_credentials_skip_generated_ids_but_screen_unrelated_groups(self):
        first = self.capture(command="tool --token=1")
        self.capture(command="tool --token=1")
        self.assertIn("1", first["group_id"])
        proposal = self.proposal(first["group_id"])
        self.export(proposal, "short-credential.md")
        self.assertIn(first["group_id"], (self.cwd / "short-credential.md").read_text())
        self.capture(command="tool --token=count")
        clean_group, _ = self.recurring()
        clean_proposal = self.proposal(clean_group)
        self.export(clean_proposal, "unrelated.md", code=2)
        self.assertFalse((self.cwd / "unrelated.md").exists())

    def test_privacy_canaries_block_legacy_final_artifact_without_writes(self):
        group, _ = self.recurring()
        proposal = self.proposal(group)
        inputs = self.privacy_inputs()
        path = self.store / "proposals" / (proposal + ".json")
        original = json.loads(path.read_text())
        output = self.cwd / "privacy/rule.md"
        for case, (category, canary) in self.privacy_canaries().items():
            with self.subTest(case=case):
                path.write_text(json.dumps(dict(original, rule="Check " + canary)))
                self.assert_privacy_block(["export", "--proposal", proposal, "--reviewed", "--output",
                                           str(output), *inputs], output, canary, category)

    def test_generated_protocol_is_not_a_private_repository_label(self):
        self.capture(error="password=x")
        group, _ = self.recurring()
        proposal = self.proposal(group)
        self.capture(error="unrelated", command="other")
        self.run_cli("capture", "--command", "other", "--repo", "sample-org/coldfix", "--agent", "worker", stdin="unrelated")
        self.export(proposal, "protocol.md")
        self.assertIn("coldfix:failure:", (self.cwd / "protocol.md").read_text())
        inputs = self.public_inputs()
        gate = self.cwd / "gate.py"
        gate.write_text(gate.read_text().replace("assert 'example' in a.private_repo",
            "assert 'example' in a.private_repo and 'sample-org/coldfix' in a.private_repo and 'coldfix' in a.private_repo"))
        self.run_cli("export", "--proposal", proposal, "--reviewed", "--output", "public-protocol.md", *inputs)
        self.assertTrue((self.cwd / "public-protocol.md").exists())
        self.run_cli("capture", "--command", "other", "--repo", "coldfix", "--agent", "worker", stdin="unrelated")
        self.run_cli("export", "--proposal", proposal, "--reviewed", "--output", "bare-label.md", *inputs)
        self.assertTrue((self.cwd / "bare-label.md").exists())
        output = self.cwd / "private-label.md"
        blocked = self.proposal(group, rule="Check coldfix before accepting changes.")
        self.run_cli("export", "--proposal", blocked, "--reviewed", "--output", str(output), code=2)
        self.assertFalse(output.exists())
        secret = self.proposal(group, rule="Require x before accepting changes.")
        self.run_cli("export", "--proposal", secret, "--reviewed", "--output", "short-secret.md", code=2)
        self.assertFalse((self.cwd / "short-secret.md").exists())

    def test_protocol_classification_refuses_a_candidate_path_with_history(self):
        group, _ = self.recurring()
        proposal = self.proposal(group)
        self.run_cli("capture", "--command", "other", "--repo", "sample-org/coldfix", "--agent", "worker", stdin="unrelated")
        inputs = self.public_inputs()
        source = self.cwd / "gate-source"
        candidate = source / "exports/prior.md"
        candidate.parent.mkdir()
        candidate.write_text("coldfix")
        for args in (("add", "."), ("-c", "user.name=Stage Author", "-c", "user.email=stage-author",
                     "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "commit", "-qm", "Prior content")):
            subprocess.run(["git", "-C", str(source), *args], capture_output=True, check=True)
        candidate.unlink()
        for args in (("add", "-u"), ("-c", "user.name=Stage Author", "-c", "user.email=stage-author",
                     "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "commit", "-qm", "Remove prior content")):
            subprocess.run(["git", "-C", str(source), *args], capture_output=True, check=True)
        result = self.run_cli("export", "--proposal", proposal, "--reviewed", "--output", "prior.md", *inputs, code=2)
        self.assertIn("prior history", result.stderr)
        self.assertFalse((self.cwd / "prior.md").exists())
        self.assertFalse((self.cwd / "gate.seen").exists())

    def test_code_paths_public_hosts_and_url_paths_are_not_private_hostnames(self):
        spec = importlib.util.spec_from_file_location("coldfix_privacy", CLI)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for value in ("subprocess.run", "fix_vm.subprocess.run", "git config user.name", "setuptools.build_meta",
                      "fix.pr_template", "test_packaging.PackagingTests.test_x", "urllib.request.OpenerDirector.open",
                      "https://api.github.com/repos/o/r", "raw.githubusercontent.com", "see example.org",
                      "https://github.com/o/r/blob/main/GUIDE.md", "https://github.com/o/r/pull/7#pullrequestreview-456",
                      "comment-2", "pr-1", "view-example-viewer-pull-7",
                      "tests." + "LongTestClass" * 6 + ".test_example"):
            with self.subTest(value=value):
                self.assertEqual(module.privacy_counts(value), {})
        for value in ("https://build.run/x", "ops@deploy.name", "secret.internal.test_x", "db.internal.corp.read_text",
                      "node-07", "api.example.org", "evilgithub.com", "https://ci.example.com/GUIDE.md"):
            with self.subTest(value=value):
                self.assertGreater(module.privacy_counts(value).get("hostname", 0), 0)

    def test_reserved_example_names_method_calls_and_badges_pass_beside_real_hosts(self):
        spec = importlib.util.spec_from_file_location("coldfix_privacy", CLI)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cases = (
            ("git -c user.email=fixture@example.invalid fetch https://example.invalid/run/1", {}),
            ("see https://ci.example.invalid/job, box.example.test and user@host.example", {}),
            ("self.total() and runner.fail() differ", {}),
            ("![build](https://img.shields.io/badge/x)", {}),
            ("fixture@example.invalid and jane.doe@gmail.com", {"email": 1, "hostname": 1}),
            ("see https://ci.example.invalid/job and ssh build.corp.internal", {"hostname": 1}),
            ("badge from img.shields.io, data from db.mycorp.io", {"hostname": 1}),
            ("Deploy to node-02 and runner-warm-1 via fixture@example.invalid", {"hostname": 2}),
            ("Move the cache off node-03 before self.total() runs", {"hostname": 1}),
            ("build.invalid", {"hostname": 1}),
            ("api.internal", {"hostname": 1}),
            ("https://db.test/x", {"hostname": 1}),
            ("notexample.invalid and example.invalid.corp", {"hostname": 2}),
            ("self.total() but curl https://db.mycorp.io(", {"hostname": 1}),
            ("mail ops@db.mycorp.io(", {"email": 1, "hostname": 1}),
        )
        for value, expected in cases:
            for exempt in (False, True):
                with self.subTest(value=value, filename_exempt=exempt):
                    self.assertEqual(module.privacy_counts(value, filename_exempt=exempt), expected)

    def test_privacy_screen_counts_filename_sources_and_unrelated_captured_secret(self):
        spec = importlib.util.spec_from_file_location("coldfix_privacy", CLI)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.privacy_counts("example@company.engineering 100.99.88.77 2001:db8::7"),
                         {"email": 1, "hostname": 1, "ip": 2})
        self.assertEqual(module.privacy_counts("example@example.invalid 100.99.88.77 2001:db8::7"), {"ip": 2})
        self.assertEqual(module.privacy_counts("Connect to 192.0.2.123."), {"ip": 1})
        self.assertEqual(module.privacy_counts("Contact example@company.engineering."), {"email": 1, "hostname": 1})
        for phone in ("020 7946 0958", "0044 20 7946 0958", "555-0147",
                      "030/12345678", "202–555–0147", "020\t7946\t0958"):
            self.assertEqual(module.privacy_counts(phone), {"phone": 1})
        self.assertEqual(module.privacy_counts("Address 2001:db8::1234:"), {"ip": 1})
        for value, category in (("server_192.168.1.1", "ip"), ("db_2001:db8::1", "ip"),
                                ("office_020 7946 0958", "phone")):
            self.assertEqual(module.privacy_counts(value), {category: 1})
        for value, category in (("server_node-01", "hostname"), ("db.example.com_backup", "hostname"),
                                ("example@example.com_suffix", "email"), ("prefix_ghp_" + "A" * 24, "credential"),
                                ("/root/example-private", "home_path"), ("~example/private", "home_path")):
            self.assertGreater(module.privacy_counts(value).get(category, 0), 0)
        for host in ("database.example.io", "database.example.com.", "database.example.engineering",
                     "example.engineering", "https://example.engineering/path",
                     "example.xn--p1ai", "пример.рф"):
            self.assertEqual(module.privacy_counts(host), {"hostname": 1})
        self.assertEqual(module.privacy_counts("json.dumps sys.exit count.txt"), {})
        for sample, category in (("_example@example.com_", "email"), ("_db.example.com_", "hostname"),
                                 ("_020 7946 0958_", "phone"), ("_100.99.88.77_", "ip"),
                                 ("_ghp_" + "ExampleOnlyCanary1234567890_", "credential"),
                                 ("__outer _db.example.com_ text__", "hostname")):
            self.assertGreater(module.privacy_counts(sample).get(category, 0), 0)
        group, _ = self.recurring()
        proposal = self.proposal(group)
        inputs = self.privacy_inputs()
        for filename in ("example-private-project.md", "example-person-canary.md", "example-node-47.md", "example-canary-author.md"):
            if "person" in filename:
                denylist = Path(inputs[1])
                denylist.write_text(json.dumps(["example-person-canary"]))
            self.assert_privacy_block(["export", "--proposal", proposal, "--reviewed", "--output", filename, *inputs],
                                      self.cwd / filename, filename[:-3])
        for label in ("example-owner/example-private-project", "git@example.invalid:example-owner/example-private-project.git",
                      "/private/example-owner/example-private-project.git", "C:\\private\\example-private-project.git"):
            self.assertIn("example-private-project", module.repository_terms(label))
        self.capture(error="password=" + "example-unrelated-secret")
        path = self.store / "proposals" / (proposal + ".json")
        record = json.loads(path.read_text())
        path.write_text(json.dumps(dict(record, rule="Check example-unrelated-secret")))
        self.assert_privacy_block(["export", "--proposal", proposal, "--reviewed", "--output", "secret.md"],
                                  self.cwd / "secret.md", "example-unrelated-secret")

    def test_denylist_formats_and_invalid_sources_fail_closed_at_cli(self):
        group, _ = self.recurring()
        proposal = self.proposal(group, rule="Check Example Congregation Canary")
        denylist = self.cwd / "terms"
        forms = [json.dumps(["Example Congregation Canary"]), json.dumps({"terms": ["Example Congregation Canary"]}),
                 json.dumps({"categories": {"church": ["Example Congregation Canary"]}}),
                 json.dumps({"categories": {"church": {"terms": ["Example Congregation Canary"]}}}),
                 "# private terms\nExample Congregation Canary\n"]
        for value in forms:
            denylist.write_text(value)
            self.assert_privacy_block(["export", "--proposal", proposal, "--reviewed", "--output", "rule.md",
                                       "--denylist", str(denylist)], self.cwd / "rule.md", "Example Congregation Canary")
        for value in ("[]", '{"terms":[1]}', '{"categories":{"bad":null}}', '{"terms":["ok"],"terms":[]}', "", "{"):
            denylist.write_text(value)
            self.export_with_privacy_source(proposal, "--denylist", str(denylist))
        self.export_with_privacy_source(proposal, "--denylist", str(self.cwd / "missing"))
        self.export_with_privacy_source(proposal, "--git-repo", str(self.cwd))
        history = self.cwd / "empty-history"
        history.mkdir()
        subprocess.run(["git", "-C", str(history), "init", "--quiet"], check=True, capture_output=True)
        self.export_with_privacy_source(proposal, "--git-repo", str(history))
        subprocess.run(["git", "-C", str(history), "-c", "user.name=Example", "-c", "user.email=example",
                        "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "commit", "--quiet",
                        "--allow-empty", "-m", "Example"], check=True, capture_output=True)
        head = subprocess.run(["git", "-C", str(history), "rev-parse", "HEAD"], check=True, capture_output=True).stdout
        (history / ".git/shallow").write_bytes(head)
        self.export_with_privacy_source(proposal, "--git-repo", str(history))
        with patch.dict(os.environ, RELEASE_CHECK_DENYLIST=str(denylist)):
            self.export_with_privacy_source(proposal)

    def test_export_screens_credentials_from_unrelated_private_replay_output(self):
        revision = self.author_lesson()
        script = self.lesson_record(revision)["check_script"] + "print('password=" + "example-replay-secret', file=sys.stderr)\n"
        revision = self.author_lesson(script)
        repo, before, after = self.history()
        self.run_replay(revision, repo, before, after)
        group, _ = self.recurring()
        proposal = self.proposal(group, rule="Check prefixexample-replay-secretsuffix")
        self.assert_privacy_block(["export", "--proposal", proposal, "--reviewed", "--output", "replay-secret.md"],
                                  self.cwd / "replay-secret.md", "example-replay-secret")

    def test_public_gate_runs_exact_candidate_at_real_cli_boundary_on_every_export_path(self):
        import hashlib
        group, _ = self.recurring()
        proposal = self.proposal(group)
        revision = self.author_lesson()
        repo, before, after = self.history()
        proven = json.loads(self.run_replay(revision, repo, before, after, "--status", "active").stdout)["id"]
        inputs = self.public_inputs()
        original = subprocess.run(["git", "-C", str(self.cwd / "gate-source"), "rev-parse", "HEAD"], capture_output=True).stdout
        for target in (["--proposal", proposal], ["--lesson", proven], ["--format", "briefing"]):
            with self.subTest(target=target[0]):
                output = self.cwd / (target[0][2:] + ".md")
                result = self.run_cli("export", *target, "--reviewed", "--output", str(output), *inputs)
                self.assertTrue(json.loads(result.stdout)["exported"])
                self.assertNotIn("example-private-subprocess-canary", result.stdout + result.stderr)
                self.assertEqual((self.cwd / "gate.seen").read_text(), hashlib.sha256(output.read_bytes()).hexdigest())
                self.assertEqual(list(self.store.glob(".release-check-*")), [])
        self.assertEqual(subprocess.run(["git", "-C", str(self.cwd / "gate-source"), "rev-parse", "HEAD"], capture_output=True).stdout, original)
        self.assertFalse((self.cwd / "gate-source" / "exports").exists())

    def test_public_gate_fails_closed_without_creating_or_overwriting_output(self):
        group, _ = self.recurring()
        proposal = self.proposal(group)
        for mode in ("nonzero", "malformed", "summary-only", "bad-report", "incomplete", "missing-field", "no-commands", "blocked-dependencies",
                     "false-pass", "bad-count", "changed", "symlink", "failed", "traversal"):
            with self.subTest(mode=mode):
                inputs = self.public_inputs(mode)
                result = self.run_cli("export", "--proposal", proposal, "--reviewed", "--output", "blocked/rule.md", *inputs, code=2)
                self.assertIn("release-check", result.stderr)
                self.assertNotIn("example-private-subprocess-canary", result.stdout + result.stderr)
                self.assertFalse((self.cwd / "blocked").exists())
                self.assertEqual(list(self.store.glob(".release-check-*")), [])
        for inputs in (["--public"], ["--public", "--release-check", str(self.cwd / "missing")],
                       ["--public", "--release-check-repo", str(self.cwd / "missing")]):
            self.run_cli("export", "--proposal", proposal, "--reviewed", "--output", "blocked/rule.md", *inputs, code=2)
        inputs = self.public_inputs()
        (self.cwd / "gate.py").unlink()
        self.run_cli("export", "--proposal", proposal, "--reviewed", "--output", "blocked/rule.md", *inputs, code=2)
        self.assertFalse((self.cwd / "blocked").exists())
        self.public_inputs()
        output = self.cwd / "existing.md"
        output.write_text("example existing bytes")
        seen = self.cwd / "gate.seen"
        seen.unlink(missing_ok=True)
        self.run_cli("export", "--proposal", proposal, "--reviewed", "--output", str(output), *inputs, code=2)
        self.assertEqual(output.read_text(), "example existing bytes")
        self.assertFalse(seen.exists())

    def test_public_gate_runs_installed_command_from_path_and_blocks_when_missing_or_erroring(self):
        group, _ = self.recurring()
        proposal = self.proposal(group)
        bin_dir = self.cwd / "path-bin"
        bin_dir.mkdir()
        command = bin_dir / "coldrelease"

        def installed(mode):
            inputs = self.public_inputs(mode)
            command.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{self.cwd / "gate.py"}" "$@"\n')
            command.chmod(0o755)
            inputs[inputs.index("--release-check") + 1] = "coldrelease"
            return inputs

        with patch.dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ["PATH"]):
            output = self.cwd / "path-gate.md"
            result = self.run_cli("export", "--proposal", proposal, "--reviewed", "--output", str(output), *installed("pass"))
            self.assertTrue(json.loads(result.stdout)["exported"])
            self.assertEqual((self.cwd / "gate.seen").read_text(), hashlib.sha256(output.read_bytes()).hexdigest())
            for mode in ("nonzero", "malformed", "failed", "incomplete"):
                with self.subTest(mode=mode):
                    result = self.run_cli("export", "--proposal", proposal, "--reviewed", "--output", "blocked/rule.md",
                                          *installed(mode), code=2)
                    self.assertIn("public release-check failed or returned incomplete evidence", result.stderr)
                    self.assertFalse((self.cwd / "blocked").exists())
            inputs = installed("pass")
            inputs[inputs.index("--release-check") + 1] = "coldrelease-absent-" + uuid.uuid4().hex
            (self.cwd / "gate.seen").unlink()
            result = self.run_cli("export", "--proposal", proposal, "--reviewed", "--output", "blocked/rule.md", *inputs, code=2)
            self.assertIn("release-check command unavailable", result.stderr)
            self.assertFalse((self.cwd / "blocked").exists())
            self.assertFalse((self.cwd / "gate.seen").exists())
            self.assertEqual(list(self.store.glob(".release-check-*")), [])

    def test_public_gate_with_real_release_check_when_supplied(self):
        entry = os.environ.get("COLDFIX_RELEASE_CHECK")
        if entry is None:
            self.skipTest("set COLDFIX_RELEASE_CHECK to coldrelease or a release_check.py path")
        if os.sep in entry:
            self.assertTrue(Path(entry).is_file(), "supplied release-check entry must exist")
            entry = str(Path(entry).resolve())
        else:
            self.assertIsNotNone(shutil.which(entry), "supplied release-check command must be on PATH")
        group, _ = self.recurring()
        proposal = self.proposal(group)
        inputs = self.public_inputs()
        inputs[inputs.index("--release-check") + 1] = entry
        source = self.cwd / "gate-source"
        (source / "README.md").write_text("# Rule staging\n## Limitations\nReviewed local fragments only.\npython -m unittest discover -s tests -q\n")
        (source / "CONTRIBUTING.md").write_text("Review each fragment.\n")
        (source / "SECURITY.md").write_text("Keep raw evidence private.\n")
        licence = (CLI.parent / "LICENSE").read_text()
        (source / "LICENSE").write_text(re.sub(r"(?m)^Copyright[^\n]*", "Copyright (c) 2026 Stage Contributors", licence))
        (source / ".github/ISSUE_TEMPLATE").mkdir(parents=True)
        (source / ".github/ISSUE_TEMPLATE/bug.md").write_text("Describe the failure.\n")
        (source / "tests").mkdir()
        (source / "tests/test_fragment.py").write_text(
            "from pathlib import Path\nimport unittest\n"
            "class FragmentTests(unittest.TestCase):\n"
            " def test_staged_fragment(self):\n"
            "  candidate, = Path('exports').glob('*.md')\n"
            "  self.assertIn('# Verify the count', candidate.read_text())\n")
        (source / "release-check.json").write_text(json.dumps(dict(
            quickstart=[dict(file="README.md", line=4, argv=["python", "-m", "unittest", "discover", "-s", "tests", "-q"])],
            checks=[dict(role="tests", argv=["python", "-m", "unittest", "discover", "-s", "tests", "-q"])])))
        for args in (("add", "."), ("-c", "user.name=Stage Author", "-c", "user.email=stage-author",
                     "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m", "Gate checks")):
            subprocess.run(["git", "-C", str(source), *args], capture_output=True, check=True)
        self.run_cli("export", "--proposal", proposal, "--reviewed", "--output", "real-gate.md", *inputs)
        self.assertIn("# Verify the count", (self.cwd / "real-gate.md").read_text())
        self.run_cli("capture", "--command", "other", "--repo", "sample-org/coldfix", "--agent", "worker", stdin="unrelated")
        self.run_cli("export", "--proposal", proposal, "--reviewed", "--output", "real-protocol.md", *inputs)
        self.assertTrue((self.cwd / "real-protocol.md").exists())
        self.run_cli("capture", "--command", "other", "--repo", "sample-org/private-staging-label", "--agent", "worker", stdin="unrelated")
        (source / "SECURITY.md").write_text("Archive private-staging-label.\n")
        subprocess.run(["git", "-C", str(source), "add", "."], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(source), "-c", "user.name=Stage Author", "-c", "user.email=stage-author",
                        "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m", "Private label fixture"], capture_output=True, check=True)
        self.run_cli("export", "--proposal", proposal, "--reviewed", "--output", "private-label-blocked.md", *inputs, code=2)
        self.assertFalse((self.cwd / "private-label-blocked.md").exists())
        self.run_cli("capture", "--command", "other", "--repo", "private-staging-label", "--agent", "worker", stdin="unrelated")
        self.run_cli("export", "--proposal", proposal, "--reviewed", "--output", "bare-label-blocked.md", *inputs, code=2)
        self.assertFalse((self.cwd / "bare-label-blocked.md").exists())
        (source / "SECURITY.md").write_text("example.person@example.invalid\n")
        subprocess.run(["git", "-C", str(source), "add", "."], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(source), "-c", "user.name=Stage Author", "-c", "user.email=stage-author",
                        "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m", "Leak fixture"], capture_output=True, check=True)
        result = self.run_cli("export", "--proposal", proposal, "--reviewed", "--output", "real-blocked.md", *inputs, code=2)
        self.assertFalse((self.cwd / "real-blocked.md").exists())
        self.assertNotIn("example.person", result.stdout + result.stderr)

    def test_authored_credentials_and_control_characters_are_refused(self):
        group, _ = self.recurring()
        for rule in ("token=private123", "Bearer " + "a" * 30, "ghp_" + "x" * 30, "text\x1b[31m"):
            with self.subTest(rule_type=rule.split()[0][:5]):
                result = self.run_cli("propose", "--group", group, "--kind", "lint", "--title", "Safe title",
                                      "--rule", rule, "--check-command", "true", code=2)
                self.assertNotIn(rule, result.stderr + result.stdout)
        self.assertFalse((self.store / "proposals").exists())

    def test_commands_are_never_executed(self):
        marker = self.cwd / "executed"
        command = "python3 -c \"open('executed','w').write('bad')\""
        first = self.capture(command=command)
        self.capture(command=command)
        proposal = self.proposal(first["group_id"], check=command)
        self.export(proposal)
        self.assertFalse(marker.exists())
        self.assertIn(command, (self.cwd / "rules/count-check.md").read_text())

    def test_missing_empty_invalid_arguments_are_safe(self):
        for args, stdin in [(("capture",), None),
                            (("capture", "--command", " ", "--repo", "r", "--agent", "a"), "failure"),
                            (("capture", "--command", "false", "--repo", "r", "--agent", "a"), ""),
                            (("capture", "--command", "false", "--repo", "", "--agent", "a"), "failure"),
                            (("capture", "--command", "false", "--repo", "r", "--agent", "a", "--log-file", "missing"), None),
                            (("propose", "--kind", "secret-invalid-choice"), None),
                            (("propose", "--rule", ""), None),
                            (("capture", "--command", "false", "--repo", "r", "--agent", "a", "--log-file", ""), None),
                            (("--store", "", "propose"), None),
                            (("unknown-secret-command",), None)]:
            result = self.run_cli(*args, stdin=stdin, code=2)
            self.assertNotIn("secret-invalid-choice", result.stdout + result.stderr)
            self.assertNotIn("unknown-secret-command", result.stdout + result.stderr)
        self.assertFalse(self.store.exists())

    def test_singletons_unknown_groups_and_missing_authoring_are_refused(self):
        first = self.capture()
        result = json.loads(self.run_cli("propose").stdout)["groups"][0]
        self.assertEqual((result["count"], result["recurring"]), (1, False))
        self.run_cli("propose", "--group", first["group_id"], code=2)
        self.capture()
        self.run_cli("propose", "--group", first["group_id"], code=2)
        self.run_cli("propose", "--group", "unknown", code=2)
        self.assertFalse((self.store / "proposals").exists())

    def test_corrupt_state_is_preserved_and_blocks_writes(self):
        first = self.capture()
        path = self.store / "failures" / (first["failure_id"] + ".json")
        original = json.loads(path.read_text())
        malformed = ["{broken-sensitive-log", "[]", '{"id": 1, "id": 2}',
                     json.dumps(dict(original, version=True)), json.dumps(dict(original, command="changed")),
                     json.dumps(dict(original, agent=[])), json.dumps(dict(original, created="2026-02-31"))]
        for content in malformed:
            path.write_text(content)
            result = self.run_cli("capture", "--command", "false", "--repo", "r", "--agent", "a", stdin="failure", code=2)
            self.assertIn("corrupt store record", result.stderr)
            self.assertEqual(path.read_text(), content)
            self.assertEqual(len(list((self.store / "failures").glob("*.json"))), 1)
            self.assertNotIn("sensitive-log", result.stdout + result.stderr)

    def test_export_rechecks_evidence_and_secret_safety(self):
        group, evidence = self.recurring()
        proposal = self.proposal(group)
        proposal_path = self.store / "proposals" / (proposal + ".json")
        original = json.loads(proposal_path.read_text())
        proposal_path.write_text(json.dumps(dict(original, rule="token=privatevalue")))
        self.export(proposal, code=2)
        self.assertFalse((self.cwd / "rules").exists())
        proposal_path.write_text(json.dumps(dict(original, evidence=["0" * 32, evidence[-1]])))
        self.export(proposal, code=2)
        proposal_path.write_text(json.dumps(original))
        (self.store / "failures" / (evidence[0] + ".json")).unlink()
        self.export(proposal, code=2)
        self.assertFalse((self.cwd / "rules").exists())

    def test_export_needs_no_approval_and_refuses_existing_and_unsafe_paths(self):
        group, _ = self.recurring()
        proposal = self.proposal(group)
        self.run_cli("export", "--proposal", proposal, "--output", "count.md")
        self.export("unknown", code=2)
        for path in ("../escape.md", "Bad_Name.md", ".coldfix/count.md", "count.txt"):
            self.export(proposal, path, code=2)
        existing = self.cwd / "count.md"
        existing.write_text("unfamiliar work")
        self.export(proposal, "count.md", code=2)
        self.assertEqual(existing.read_text(), "unfamiliar work")
        directory = self.cwd / "target"
        directory.mkdir()
        (self.cwd / "linked").symlink_to(directory, target_is_directory=True)
        self.export(proposal, "linked/count.md", code=2)
        (self.cwd / "symlink.md").symlink_to(existing)
        self.export(proposal, "symlink.md", code=2)
        self.assertEqual(list(directory.iterdir()), [])
        self.assertEqual(existing.read_text(), "unfamiliar work")
        self.export(proposal, "fresh.md")
        before = (self.cwd / "fresh.md").read_bytes()
        self.export(proposal, "fresh.md", code=2)
        self.assertEqual((self.cwd / "fresh.md").read_bytes(), before)

    def test_store_and_log_symlinks_are_refused(self):
        target = self.cwd / "target"
        target.mkdir()
        self.store.symlink_to(target, target_is_directory=True)
        self.run_cli("capture", "--command", "false", "--repo", "r", "--agent", "a", stdin="failure", code=2)
        self.assertEqual(list(target.iterdir()), [])
        self.store.unlink()
        log = self.cwd / "log"
        log.symlink_to(CLI)
        self.run_cli("capture", "--command", "false", "--repo", "r", "--agent", "a", "--log-file", "log", code=2)
        self.assertFalse(self.store.exists())

    def test_concurrent_captures_do_not_lose_repeats(self):
        def capture(_):
            result = subprocess.run([sys.executable, str(CLI), "capture", "--command", "false", "--repo", "r", "--agent", "a"],
                                    cwd=self.cwd, input="failure", capture_output=True, text=True)
            return result.returncode, json.loads(result.stdout)
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(capture, range(12)))
        self.assertEqual([code for code, _ in results], [0] * 12)
        self.assertEqual(len({value["failure_id"] for _, value in results}), 12)
        groups = json.loads(self.run_cli("propose").stdout)["groups"]
        self.assertEqual([group["count"] for group in groups], [12])

    def test_interrupted_atomic_publish_preserves_existing_and_cleans_temporary(self):
        spec = importlib.util.spec_from_file_location("coldfix", CLI)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        output = self.cwd / "rule.md"
        with patch.object(module.os, "link", side_effect=OSError("simulated disk failure")):
            with self.assertRaises(OSError):
                module.atomic_create(output, "complete content")
        self.assertFalse(output.exists())
        self.assertEqual(list(self.cwd.iterdir()), [])
        module.atomic_create(output, "complete content")
        self.assertEqual(output.read_text(), "complete content")
        with self.assertRaises(module.ColdfixError):
            module.atomic_create(output, "replacement")
        self.assertEqual(output.read_text(), "complete content")

    def test_proposal_keeps_its_evidence_snapshot(self):
        group, evidence = self.recurring()
        proposal = self.proposal(group)
        later = self.capture()
        self.export(proposal)
        content = (self.cwd / "rules/count-check.md").read_text()
        self.assertNotIn(later["failure_id"], content)
        for ref in evidence:
            self.assertIn(ref, content)
        groups = json.loads(self.run_cli("propose").stdout)["groups"]
        self.assertEqual(groups[0]["count"], 3)

    def test_read_and_write_errors_do_not_echo_sensitive_paths(self):
        sensitive = "private-path-" + "9" * 15
        result = self.run_cli("capture", "--command", sensitive, "--repo", "r", "--agent", "a",
                              "--log-file", sensitive, code=2)
        self.assertNotIn(sensitive, result.stdout + result.stderr)
        binary = self.cwd / sensitive
        binary.write_bytes(b"\xff\xfe")
        result = self.run_cli("capture", "--command", "false", "--repo", "r", "--agent", "a",
                              "--log-file", sensitive, code=2)
        self.assertNotIn(sensitive, result.stdout + result.stderr)
        self.assertFalse(self.store.exists())
        group, _ = self.recurring()
        proposal = self.proposal(group)
        parent = self.cwd / "blocked"
        parent.write_text("unfamiliar file")
        self.export(proposal, "blocked/" + sensitive + ".md", code=2)
        self.assertEqual(parent.read_text(), "unfamiliar file")

    def test_export_fences_a_check_containing_backticks(self):
        group, _ = self.recurring()
        command = "printf '%s' ```sample```"
        proposal = self.proposal(group, check=command)
        self.export(proposal)
        self.assertIn("````sh\n" + command + "\n````\n", (self.cwd / "rules/count-check.md").read_text())

    def test_export_screens_embedded_authored_script_credentials(self):
        revision = self.author_lesson()
        script = self.lesson_record(revision)["check_script"] + "# password=" + "example-private-canary\n"
        revision = self.author_lesson(script)
        repo, before, after = self.history()
        proven = json.loads(self.run_replay(revision, repo, before, after, "--status", "active").stdout)["id"]
        result = self.run_cli("export", "--lesson", proven, "--reviewed", "--output", "secret.md", code=2)
        self.assertFalse((self.cwd / "secret.md").exists())
        self.assertNotIn("example-private-canary", result.stderr + result.stdout)

    def test_replay_runs_the_check_through_the_configured_runner(self):
        revision = self.author_lesson()
        marker = self.cwd / "runner-used"
        runner = f"echo used >> {shlex.quote(str(marker))} && sh -c {{script}}"
        with patch.dict(os.environ, {"COLDFIX_REPLAY_RUNNER": runner}):
            result = json.loads(self.run_replay(revision, *self.history()).stdout)
        self.assertEqual(result["verification"], "proven")
        self.assertEqual(marker.read_text(), "used\nused\n")

    def test_replay_preserves_sha256_repository_format(self):
        revision = self.author_lesson()
        repo = self.cwd / "sha256-history"
        subprocess.run(["git", "init", "--quiet", "--object-format=sha256", str(repo)], check=True)
        def git(*args):
            return subprocess.run(["git", "-C", str(repo), "-c", "user.name=Example",
                                   "-c", "user.email=example@example.invalid", *args],
                                  check=True, capture_output=True, text=True).stdout.strip()
        revisions = []
        for value in ("1", "2"):
            (repo / "count.txt").write_text(value)
            git("add", ".")
            git("commit", "--quiet", "-m", "Example count " + value)
            revisions.append(git("rev-parse", "HEAD"))
        result = json.loads(self.run_replay(revision, repo, *revisions).stdout)
        self.assertEqual(result["verification"], "proven")

    def test_scope_is_global_only_after_replay_proof_in_two_distinct_repositories(self):
        def root(repo):
            return subprocess.run(["git", "-C", str(repo), "rev-list", "--max-parents=0", "HEAD"],
                                  capture_output=True, text=True, check=True).stdout.strip()
        revision = self.author_lesson()
        self.assertEqual(self.lesson_record(revision)["scope"], "repo:example-one")
        second, second_before, second_after = self.history("second")
        one = json.loads(self.run_replay(revision, second, second_before, second_after, "--status", "active").stdout)
        self.assertEqual(one["verification"], "proven")
        self.assertEqual(self.lesson_record(one["id"])["scope"], "repo:" + root(second))
        clone = self.cwd / "second-clone"
        subprocess.run(["git", "clone", "--quiet", str(second), str(clone)], check=True)
        unrelated, _, _ = self.history("unrelated")
        for command in (("config", "user.name", "Example"), ("config", "user.email", "example@example.invalid"),
                        ("fetch", "--quiet", str(unrelated), "HEAD"),
                        ("merge", "--quiet", "--allow-unrelated-histories", "-s", "ours", "-m", "Merge unrelated", "FETCH_HEAD")):
            subprocess.run(["git", "-C", str(clone), *command], capture_output=True, check=True)
        merged = subprocess.run(["git", "-C", str(clone), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        again = json.loads(self.run_replay(one["id"], clone, second_before, merged).stdout)
        self.assertEqual(again["verification"], "proven")
        self.assertEqual(len(self.lesson_record(again["id"])["scope"].split(",")), 2)
        self.assertNotEqual(self.lesson_record(again["id"])["scope"], "global")
        first, before, after = self.history("first")
        two = json.loads(self.run_replay(again["id"], first, before, after).stdout)
        self.assertEqual(two["verification"], "proven")
        self.assertEqual(self.lesson_record(two["id"])["scope"], "global")
        self.run_cli("lesson")
        check = self.cwd / "revised-check.py"
        check.write_text((self.cwd / "authored-check.py").read_text() + "\n")
        revised = json.loads(self.run_cli("lesson", "--revise", two["id"], "--check-file", str(check)).stdout)["id"]
        self.assertEqual(self.lesson_record(revised)["scope"], "repo:example-one")
        reproven = json.loads(self.run_replay(revised, first, before, after).stdout)
        self.assertEqual(self.lesson_record(reproven["id"])["scope"], "repo:" + root(first))



class ReleaseQuickstartTests(unittest.TestCase):
    def test_first_run_finds_a_shipped_fixture_before_recurring_setup(self):
        root = CLI.parent
        readme = (root / "README.md").read_text()
        fixture = "library/reject-linter-error-output/fixtures/bad"
        scan = f"coldfix check {fixture}"
        before_setup = readme.split("coldfix init --yes", 1)[0]
        self.assertIn(scan, before_setup.splitlines())
        self.assertIn(f"{fixture}/lint.py", (root / "public-export.txt").read_text().splitlines())
        done = subprocess.run([sys.executable, str(CLI), "check", fixture], cwd=root,
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("reject-linter-error-output", done.stdout)
        self.assertIn("lint.py:2", done.stdout)

    def test_each_quickstart_entry_names_its_readme_line(self):
        root = CLI.parent
        manifest = json.loads((root / "release-check.json").read_text())
        for entry in manifest["quickstart"]:
            lines = (root / entry["file"]).read_text().splitlines()
            self.assertEqual(shlex.split(lines[entry["line"] - 1]), entry["argv"], entry)

    def test_every_tracked_file_is_exported_or_excluded_exactly_once(self):
        root = CLI.parent
        listed = subprocess.run(["git", "-C", str(root), "ls-files"], capture_output=True, text=True)
        if listed.returncode:
            self.skipTest("not a git checkout")
        patterns = json.loads((root / "release-check.json").read_text())["exclude"]
        exported = set((root / "public-export.txt").read_text().split())

        def excluded(path):
            parts = Path(path).parts
            return any(fnmatch.fnmatchcase(prefix, pattern) or fnmatch.fnmatchcase(Path(prefix).name, pattern)
                       for prefix in ("/".join(parts[:end]) for end in range(1, len(parts) + 1)) for pattern in patterns)

        for path in listed.stdout.splitlines():
            with self.subTest(path=path):
                self.assertNotEqual(path in exported, excluded(path),
                                    path + " must be in exactly one of public-export.txt and release-check.json exclude")

    def test_the_package_installs_every_exported_top_level_module(self):
        root = CLI.parent
        exported = {Path(path).stem for path in (root / "public-export.txt").read_text().split()
                    if "/" not in path and path.endswith(".py")}
        block = re.search(r"^py-modules = \[\n(.*?)^\]", (root / "pyproject.toml").read_text(), re.M | re.S)
        modules = re.findall(r'^    "(\w+)",$', block.group(1), re.M)
        self.assertEqual(sorted(exported - {"test_suite"}), modules,
                         "pyproject.toml py-modules must list every exported top-level module, sorted")


if __name__ == "__main__":
    unittest.main()
