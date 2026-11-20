#!/usr/bin/env python3
"""Test fixture: stands in for the agent CLI; runs the configured PreToolUse hook and emits a stream-json transcript."""
import json
from pathlib import Path
import shlex
import subprocess
import sys


def emit(**event):
    print(json.dumps(event))


def option(name):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else None


def main():
    emit(type="system", subtype="fixture_args", argv=sys.argv[1:])
    target = Path.cwd() / "guarded.py"
    hooks = json.loads(Path(option("--settings")).read_text())
    for group in hooks.get("hooks", {}).get("PreToolUse", []):
        for hook in group["hooks"]:
            done = subprocess.run(shlex.split(hook["command"]), input=json.dumps(dict(cwd=str(Path.cwd()), tool_input=dict(file_path=str(target)))),
                                  capture_output=True, text=True)
            emit(type="system", subtype="hook_response", output=done.stdout)
    edits = [("t1", "bad", "worse"), ("t2", "bad", "good")]
    for id_, old, new in edits:
        emit(type="assistant", message=dict(id="m" + id_, usage=dict(input_tokens=10, output_tokens=5), content=[
            dict(type="tool_use", id=id_, name="Edit", input=dict(file_path=str(target), old_string=old, new_string=new))]))
        emit(type="user", message=dict(content=[dict(type="tool_result", tool_use_id=id_, is_error=id_ == "t1")]))
    target.write_text("good")
    emit(type="result", total_cost_usd=0.5, num_turns=3, usage=dict(input_tokens=100, output_tokens=20, cache_creation_input_tokens=0, cache_read_input_tokens=30))


main()
