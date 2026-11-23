FIXTURES = {
    "bad": {'guest.py': 'import subprocess\nresult = subprocess.run(command, capture_output=True, timeout=30)\nlog.write_text(result.stdout)\n'},
    "good": {'guest.py': 'import subprocess\nwith log.open("w") as output:\n    subprocess.run(command, stdout=output, timeout=30)\n'},
}
