FIXTURES = {
    "bad": {'build.py': 'import subprocess\nsubprocess.run(["git", "status"], env={"PATH": "/bin"})\n'},
    "good": {'build.py': 'import shutil\nimport subprocess\ngit = shutil.which("git")\nsubprocess.run([git, "status"], env={"PATH": "/bin"})\n'},
}
