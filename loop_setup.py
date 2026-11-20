"""Check the automatic loop's prerequisites, then run its first pass: collect GitHub CI failures, scan transcripts
for corrected mistakes and list the candidates that pass found. `init --yes` finds the prerequisites, writes the
model command, schedules the pass hourly and runs it once."""
import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile

import coldfix as c
import learn_candidates
import learn_collect
import replay_isolation
import transcript_mistakes

SCAN_LEDGER = "transcript-scan.sqlite"
SCAN_THRESHOLD = 0.6
HERE = Path(__file__).resolve().parent
TRANSCRIPT_FOLDERS = (".claude/projects", ".codex/sessions")
MODEL_CLIS = ("claude", "codex")
WRAPPER = "model-cmd"
UNIT = "coldfix-loop"
CRON_TAG = "# coldfix-loop"


def github_owner():
    reply = subprocess.run(["gh", "api", "user", "--jq", ".login"], capture_output=True, text=True) if shutil.which("gh") else None
    c.require(reply and reply.returncode == 0 and reply.stdout.strip(),
              "GitHub unavailable: install gh and run gh auth login")
    return reply.stdout.strip()


def setup(args):
    command = shlex.split(os.environ.get("COLDFIX_MODEL_CMD", ""))
    c.require(command and shutil.which(command[0]), "set COLDFIX_MODEL_CMD to a runnable model command (README Requirements)")
    folders = [c.ordinary_path(folder) for folder in args.transcripts]
    c.require(all(folder.is_dir() for folder in folders), "--transcripts must be existing directories")
    c.require(args.limit > 0, "--limit must be positive")
    replay_isolation.require_sandbox()
    root = c.ordinary_path(args.store)
    root.mkdir(mode=0o700, exist_ok=True)
    github, unreachable = learn_collect.collect(root, args.owner or github_owner())
    c.require(not unreachable, "GitHub unavailable: install gh and set GH_TOKEN, or run gh auth login")
    c.require(not github["github"].get("unlisted"), "GitHub could not list Actions runs for "
              f"{github['github'].get('unlisted')} repositories; give the token Actions read access")
    judge, label = transcript_mistakes.judge("COLDFIX_MODEL_CMD", None)
    try:
        scan = transcript_mistakes.scan(transcript_mistakes.transcripts(folders), root / SCAN_LEDGER, root,
                                        "COLDFIX_MODEL_CMD", label, judge, args.limit, args.limit, SCAN_THRESHOLD)
    except BlockingIOError:
        raise c.ColdfixError("another setup run is scanning this store")
    except ValueError:
        raise c.ColdfixError("the scan ledger belongs to another model command; use a new --store")
    c.require(not scan["errors"], "transcript scan failed: " + str(scan.get("error")))
    found = learn_candidates.candidates(c.load_store(root)[1])
    first = found[0] if found else None
    return dict(backend=args.backend, github=github["github"], transcripts=scan, candidates=len(found),
                first_candidate=first and dict(headline=first["headline"], evidence=first["evidence"][:3],
                                               repos=first["repos"], transcript=first["transcript"]))


def ask_cli(cli, shape, prompt):
    with tempfile.TemporaryDirectory() as work:
        if cli == "claude":
            command = ["claude", "-p", "--tools", "", "--no-session-persistence", "--setting-sources", "",
                       "--output-format", "json", "--json-schema", json.dumps(shape)]
            reply = json.loads(subprocess.run(command, input=prompt, text=True, cwd=work, capture_output=True, check=True).stdout)
            c.require(not reply.get("is_error") and isinstance(reply.get("structured_output"), dict),
                      "claude returned no structured output")
            return reply["structured_output"]
        schema, output = Path(work) / "schema.json", Path(work) / "reply.json"
        schema.write_text(json.dumps(shape))
        subprocess.run(["codex", "exec", "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check",
                        "--sandbox", "read-only", "--ephemeral", "-c", 'approval_policy="never"',
                        "-c", "project_doc_max_bytes=0", "-C", work, "--output-schema", str(schema),
                        "--output-last-message", str(output), "-"],
                       input=prompt, text=True, cwd=work, capture_output=True, check=True)
        return json.loads(output.read_text())


def write_wrapper(root, cli):
    path = root / WRAPPER
    c.atomic_create(path, f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(HERE / 'loop_setup.py'))} {cli} \"$@\"\n",
                    replace=True, mode=0o700)
    return path


def systemd_quote(text):
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def read_crontab():
    current = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if current.returncode == 0:
        return current.stdout.splitlines()
    c.require("no crontab" in current.stderr.lower(), "crontab -l failed; left unchanged")
    return []


def write_crontab(lines):
    c.require(subprocess.run(["crontab", "-"], input="".join(line + "\n" for line in lines), text=True,
                             capture_output=True).returncode == 0, "crontab could not be written")


def lingering():
    if not shutil.which("loginctl"):
        return False
    reply = subprocess.run(["loginctl", "show-user", str(os.getuid()), "-p", "Linger", "--value"],
                           capture_output=True, text=True)
    return reply.returncode == 0 and reply.stdout.strip() == "yes"


def unit_folder():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user"


def retire_timer():
    units = unit_folder()
    if not (units / (UNIT + ".timer")).exists():
        return
    if shutil.which("systemctl"):
        subprocess.run(["systemctl", "--user", "disable", "--now", UNIT + ".timer"], capture_output=True)
    for path in (units / "timers.target.wants" / (UNIT + ".timer"), units / (UNIT + ".timer"), units / (UNIT + ".service")):
        path.unlink(missing_ok=True)


def install_schedule(root, command, environment):
    c.require(not any("\n" in text or "\r" in text for text in [str(root), *command, *environment.values()]),
              "a schedule value contains a line break; left unchanged")
    if shutil.which("systemctl") and lingering() and subprocess.run(["systemctl", "--user", "show-environment"],
                                                                    capture_output=True).returncode == 0:
        if shutil.which("crontab"):
            current = read_crontab()
            kept = [line for line in current if not line.endswith(CRON_TAG)]
            if kept != current:
                write_crontab(kept)
        units = unit_folder()
        lines = "".join(f"Environment={systemd_quote(key + '=' + value)}\n" for key, value in environment.items())
        c.atomic_create(units / (UNIT + ".service"),
                        f"[Unit]\nDescription=Coldfix learning loop\n\n[Service]\nType=oneshot\n{lines}"
                        f"WorkingDirectory={str(root.parent).replace('%', '%%')}\n"
                        f"ExecStart={' '.join(systemd_quote(arg).replace('$', '$$') for arg in command)}\n", replace=True, mode=0o600)
        c.atomic_create(units / (UNIT + ".timer"),
                        "[Unit]\nDescription=Run the Coldfix learning loop hourly\n\n[Timer]\nOnCalendar=hourly\n"
                        "RandomizedDelaySec=300\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n",
                        replace=True, mode=0o600)
        for argv in (["daemon-reload"], ["enable", "--now", UNIT + ".timer"]):
            c.require(subprocess.run(["systemctl", "--user", *argv], capture_output=True).returncode == 0,
                      "systemctl --user " + argv[0] + " failed")
        return "systemd user timer " + UNIT + ".timer"
    c.require(shutil.which("crontab"), "no scheduler: install cron, or run loginctl enable-linger and enable systemd user services")
    kept = [line for line in read_crontab() if not line.endswith(CRON_TAG)]
    retire_timer()
    assignments = " ".join(shlex.quote(f"{key}={value}") for key, value in environment.items())
    entry = (f"17 * * * * cd {shlex.quote(str(root.parent))} && env {assignments} {shlex.join(command)}"
             f" >> {shlex.quote(str(root / 'loop.log'))} 2>&1").replace("%", "\\%")
    write_crontab([*kept, entry + " " + CRON_TAG])
    return "cron entry at minute 17 of every hour"


def init_loop(store):
    root = c.ordinary_path(store).resolve()
    github_owner()
    cli = next((name for name in MODEL_CLIS if shutil.which(name)), None)
    c.require(cli, "no model CLI: install claude or codex and sign in")
    folders = [str(Path.home() / name) for name in TRANSCRIPT_FOLDERS if (Path.home() / name).is_dir()]
    c.require(folders, "no transcripts: neither ~/.claude/projects nor ~/.codex/sessions exists")
    root.mkdir(mode=0o700, exist_ok=True)
    os.environ["COLDFIX_MODEL_CMD"] = shlex.quote(str(write_wrapper(root, cli)))
    command = [sys.executable, str(HERE / "coldfix.py"), "--store", str(root), "setup", "--backend", "local",
               "--transcripts", *folders]
    environment = dict(COLDFIX_MODEL_CMD=os.environ["COLDFIX_MODEL_CMD"], PATH=os.environ.get("PATH", ""))
    environment.update({key: os.environ[key] for key in ("GH_CONFIG_DIR", "XDG_CONFIG_HOME") if os.environ.get(key)})
    first = setup(argparse.Namespace(store=str(root), owner=None, transcripts=folders, backend="local", limit=100))
    schedule = install_schedule(root, command, environment)
    return dict(model_cli=cli, model_command=os.environ["COLDFIX_MODEL_CMD"], transcripts=folders,
                schedule=schedule, first_pass=first)


def main(argv):
    shape = json.loads(argv[2])
    print(json.dumps(ask_cli(argv[0], shape, sys.stdin.read())))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
