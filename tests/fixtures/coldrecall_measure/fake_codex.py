#!/usr/bin/env python3
"""Test fixture: stands in for `codex exec --json`; runs the hooks.json in CODEX_HOME and emits a Codex JSONL transcript."""
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys


def emit(**event):
    print(json.dumps(event))


def option(name):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else None


def fire(home, event, **payload):
    groups = json.loads((home / "hooks.json").read_text())["hooks"].get(event, [])
    for group in groups:
        if event == "PreToolUse" and not re.fullmatch(group["matcher"], payload["tool_name"]):
            continue
        for hook in group["hooks"]:
            subprocess.run(shlex.split(hook["command"]), input=json.dumps(dict(hook_event_name=event, session_id="fixture", cwd=str(Path.cwd()), **payload)),
                           capture_output=True, text=True)


def shell(id_, command, code=0):
    fire(HOME, "PreToolUse", tool_name="Bash", tool_input=dict(command=command))
    emit(type="item.completed", item=dict(id=id_, type="command_execution", command=shlex.join(["/bin/bash", "-lc", command]), exit_code=code,
                                         status="completed" if code == 0 else "failed"))


HOME = Path(os.environ["CODEX_HOME"])
emit(type="thread.started", thread_id="fixture", argv=sys.argv[1:], home_has_auth=(HOME / "auth.json").exists())
fire(HOME, "SessionStart", source="startup")
fire(HOME, "UserPromptSubmit", prompt=sys.argv[-1])
emit(type="turn.started")
shell("c1", "cat guarded.py")
shell("c2", "cat guarded.py")
Path("guarded.py").write_text("good")
emit(type="item.completed", item=dict(id="f1", type="file_change", status="completed", changes=[dict(path=str(Path.cwd() / "guarded.py"), kind="update")]))
emit(type="item.completed", item=dict(id="m1", type="agent_message", text="done"))
emit(type="turn.completed", usage=dict(input_tokens=100, cached_input_tokens=60, cache_write_input_tokens=0, output_tokens=20, reasoning_output_tokens=5))
