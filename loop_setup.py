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
LOG_CAP = 64 * 1024
CRON_FAILURE = "cron-start-failure.sh"
CRON_FAILURE_SCRIPT = f"""#!/bin/sh
set -eu
cd "${{0%/*}}"
umask 077
work=$(command -p mktemp -d .loop.XXXXXXXX)
trap 'command -p rm -f "$work/failure" "$work/log"; command -p rmdir "$work"' EXIT
message="coldfix-loop failed to start: $1"
echo "$message" > "$work/failure"
[ -d loop.log.failure ] || command -p mv -f "$work/failure" loop.log.failure
if [ ! -L loop.log ] && [ -f loop.log ]; then
    command -p tail -c $(({LOG_CAP} - ${{#message}} - 1)) loop.log > "$work/log"
fi
echo "$message" >> "$work/log"
[ -d loop.log ] || command -p mv -f "$work/log" loop.log
"""
TIMER = ("[Unit]\nDescription=Run the Coldfix learning loop hourly\n\n[Timer]\nOnCalendar=hourly\n"
         "RandomizedDelaySec=300\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n")


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


def wrapper_text(interpreter, script, cli):
    return f'#!/bin/sh\nexec {shlex.join([interpreter, script, cli])} "$@"\n'


def managed_wrapper(path):
    if not path.is_file():
        return False
    try:
        content = c.read_text(path)
        command = shlex.split(content.removeprefix("#!/bin/sh\n"))
    except (ValueError, UnicodeError):
        return False
    if len(command) != 5:
        return False
    _, interpreter, script, cli, _ = command
    return (Path(interpreter).is_absolute() and Path(script).is_absolute() and Path(script).name == "loop_setup.py"
            and cli in MODEL_CLIS and content == wrapper_text(interpreter, script, cli))


def write_wrapper(root, cli):
    path = c.ordinary_path(root / WRAPPER)
    c.require(not path.exists() or managed_wrapper(path), "model wrapper has different content; left unchanged")
    c.atomic_create(path, wrapper_text(sys.executable, str(HERE / "loop_setup.py"), cli), replace=True, mode=0o700)
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


def owner_path():
    return c.ordinary_path(Path.home() / ".local/state/coldfix/schedule.json")


def owns_cron(line, root):
    if not line.endswith(" " + CRON_TAG):
        return False
    try:
        words = shlex.split(line.replace("\\%", "%"))
    except ValueError:
        return False
    if (words[:5] != ["17", "*", "*", "*", "*"] or words[5:6] not in (["cd"], ["status=$(cd"])
            or words[6:9] != [str(root.parent), "&&", "env"]):
        return False
    start = 9
    while start < len(words) and "=" in words[start] and not words[start].startswith("/"):
        start += 1
    args = words[start:-2]
    if len(args) >= 4 and Path(args[1]).name == "loop_setup.py" and args[2:4] == ["cron", str(root)]:
        return True
    return (len(args) >= 10 and Path(args[1]).name == "coldfix.py"
            and args[2:7] == ["--store", str(root), "setup", "--backend", "local"]
            and words[9] == "COLDFIX_MODEL_CMD=" + shlex.quote(str(root / WRAPPER))
            and args[-3:] == [">>", str(root / "loop.log"), "2>&1"])


def owns_service(service, root):
    binding = ' "--store" ' + systemd_quote(str(root)).replace("$", "$$") + ' "setup" "--backend" "local" '
    wrapper = systemd_quote("COLDFIX_MODEL_CMD=" + shlex.quote(str(root / WRAPPER)))
    lines = service.splitlines()
    return ("Environment=" + wrapper in lines
            and any(line.startswith("ExecStart=") and '/coldfix.py"' + binding in line for line in lines))


def schedule_state(root, *, removing=False):
    path = owner_path()
    owner = c.read_install_record(path)
    ours = owner.get("store") == str(root)
    c.require(not owner or ours or removing, "learning schedule belongs to another store; uninstall it there first")
    if "units" in owner:
        c.require(isinstance(owner["units"], str) and Path(owner["units"]).is_absolute(), "invalid schedule unit directory")
    units = Path(owner["units"]) if ours and owner.get("units") else unit_folder()
    service = c.ordinary_path(units / (UNIT + ".service"))
    timer = c.ordinary_path(units / (UNIT + ".timer"))
    service_text = c.read_text(service) if service.exists() else ""
    owned_timer = owns_service(service_text, root)
    if owned_timer:
        c.require(not timer.exists() or c.read_text(timer) == TIMER, "schedule timer has been edited")
        c.require(not ours or not owner.get("service") or c.digest(service_text) == owner["service"],
                  "schedule service has been edited")
    elif not removing or (ours and owner.get("units")):
        c.require(not service.exists() and not timer.exists(), "learning schedule belongs to another store or is unrecognized")
    has_cron = shutil.which("crontab")
    c.require(has_cron or not (ours and owner.get("cron")), "install crontab to manage this store's cron schedule")
    current = read_crontab() if has_cron else []
    kept = [line for line in current if not owns_cron(line, root)]
    if ours and owner.get("cron"):
        c.require([c.digest(line) for line in current if owns_cron(line, root)] == [owner["cron"]],
                  "schedule cron entry has been edited")
    if not removing:
        c.require(ours or owned_timer or kept != current or not any(line.endswith(CRON_TAG) for line in kept),
                  "learning schedule belongs to another store or is unrecognized")
    return units, owned_timer, current, kept


def retire_timer(units):
    timer = units / (UNIT + ".timer")
    if timer.exists():
        c.require(shutil.which("systemctl") and subprocess.run(
            ["systemctl", "--user", "disable", "--now", timer.name], capture_output=True).returncode == 0,
            "cannot disable learning schedule timer")
        timer.unlink(missing_ok=True)
    (units / (UNIT + ".service")).unlink(missing_ok=True)


def install_schedule(root, command, environment):
    c.require(not any("\n" in text or "\r" in text for text in [str(root), *command, *environment.values()]),
              "a schedule value contains a line break; left unchanged")
    units, owned_timer, current, kept = schedule_state(root)
    owner = dict(store=str(root))
    if shutil.which("systemctl") and lingering() and subprocess.run(["systemctl", "--user", "show-environment"],
                                                                    capture_output=True).returncode == 0:
        lines = "".join(f"Environment={systemd_quote(key + '=' + value)}\n" for key, value in environment.items())
        service = (f"[Unit]\nDescription=Coldfix learning loop\n\n[Service]\nType=oneshot\n{lines}"
                   f"WorkingDirectory={str(root.parent).replace('%', '%%')}\n"
                   f"ExecStart={' '.join(systemd_quote(arg).replace('$', '$$') for arg in command)}\n")
        if kept != current:
            write_crontab(kept)
        owner["units"] = str(units)
        c.atomic_create(owner_path(), c.encoded(owner), replace=True)
        c.atomic_create(units / (UNIT + ".service"), service, replace=True, mode=0o600)
        owner["service"] = c.digest(service)
        c.atomic_create(owner_path(), c.encoded(owner), replace=True)
        c.atomic_create(units / (UNIT + ".timer"), TIMER, replace=True, mode=0o600)
        for argv in (["daemon-reload"], ["enable", "--now", UNIT + ".timer"]):
            c.require(subprocess.run(["systemctl", "--user", *argv], capture_output=True).returncode == 0,
                      "systemctl --user " + argv[0] + " failed")
        return "systemd user timer " + UNIT + ".timer"
    c.require(shutil.which("crontab"), "no scheduler: install cron, or run loginctl enable-linger and enable systemd user services")
    failure_script = c.ordinary_path(root / CRON_FAILURE)
    if failure_script.exists():
        c.require(c.read_text(failure_script) == CRON_FAILURE_SCRIPT, "cron failure script has been edited")
    else:
        c.atomic_create(failure_script, CRON_FAILURE_SCRIPT, mode=0o600)
    if owned_timer:
        retire_timer(units)
    assignments = " ".join(shlex.quote(f"{key}={value}") for key, value in environment.items())
    runner = [sys.executable, str(HERE / "loop_setup.py"), "cron", str(root), *command]
    command_line = f"cd {shlex.quote(str(root.parent))} && env {assignments} {shlex.join(runner)}"
    entry = (f'17 * * * * status=$({command_line} ) && exit "$status"; rc=$?; '
             f'/bin/sh {shlex.quote(str(failure_script))} "$rc"; exit "$rc"')
    entry = entry.replace("%", "\\%") + " " + CRON_TAG
    c.atomic_create(owner_path(), c.encoded(owner), replace=True)
    write_crontab([*kept, entry])
    owner["cron"] = c.digest(entry)
    c.atomic_create(owner_path(), c.encoded(owner), replace=True)
    return "cron entry at minute 17 of every hour"


def uninstall_schedule(store):
    root = c.ordinary_path(store)
    marker = owner_path()
    with c.lesson_lock(marker.parent, name=".schedule.lock"):
        units, owned_timer, current, kept = schedule_state(root, removing=True)
        files = []
        wrapper = c.ordinary_path(root / WRAPPER)
        if managed_wrapper(wrapper):
            files.append(wrapper)
        failure_script = c.ordinary_path(root / CRON_FAILURE)
        if failure_script.is_file() and failure_script.read_bytes() == CRON_FAILURE_SCRIPT.encode():
            files.append(failure_script)
        if c.read_install_record(marker).get("store") == str(root):
            files.append(marker)
        for directory in {path.parent for path in files} | ({units} if owned_timer else set()):
            c.require(os.access(directory, os.W_OK | os.X_OK), "cannot remove installed files in " + str(directory))
        if kept != current:
            write_crontab(kept)
        if owned_timer:
            try:
                retire_timer(units)
            except (c.ColdfixError, OSError):
                if kept != current:
                    write_crontab(current)
                raise
        for path in files:
            path.unlink()


def cron_run(root, command):
    root = c.ordinary_path(root)
    tail = bytearray()
    try:
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as child:
            with child.stdout:
                while chunk := child.stdout.read(LOG_CAP):
                    tail.extend(chunk)
                    del tail[:-LOG_CAP]
            code = child.wait()
    except OSError as error:
        code = 1
        tail.extend(str(error).encode())
    if code:
        tail.extend(f"\ncoldfix: setup exited {code}\n".encode())
    with c.lesson_lock(root, name=".loop-log.lock"):
        log = c.ordinary_path(root / "loop.log")
        previous = b""
        if log.exists():
            c.require(log.is_file(), "loop.log must be a regular file")
            with log.open("rb") as stream:
                stream.seek(max(0, os.fstat(stream.fileno()).st_size - LOG_CAP))
                previous = stream.read(LOG_CAP)
        if code:
            c.atomic_create(root / "loop.log.failure", bytes(tail[-LOG_CAP:]), replace=True)
        if len(previous) + len(tail) > LOG_CAP:
            c.atomic_create(root / "loop.log.1", previous, replace=True)
            previous = b""
        c.atomic_create(log, (previous + tail)[-LOG_CAP:], replace=True)
    return code if code >= 0 else 128 - code


def init_loop(store):
    root = c.ordinary_path(store)
    with c.lesson_lock(owner_path().parent, name=".schedule.lock"):
        schedule_state(root)
        return init_owned_loop(root)


def init_owned_loop(root):
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
    if argv[0] == "cron":
        print(cron_run(argv[1], argv[2:]))
        return 0
    shape = json.loads(argv[2])
    print(json.dumps(ask_cli(argv[0], shape, sys.stdin.read())))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
