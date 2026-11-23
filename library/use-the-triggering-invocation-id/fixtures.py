FIXTURES = {
    "bad": {'handler.py': 'import subprocess\nvalue = subprocess.check_output(["systemctl", "show", "-p", "InvocationID", unit])\nsubprocess.run(["journalctl", value])\n'},
    "good": {'handler.py': 'import os\nimport subprocess\nvalue = os.environ["MONITOR_INVOCATION_ID"]\nsubprocess.run(["journalctl", value])\n'},
}
