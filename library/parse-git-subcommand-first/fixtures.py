FIXTURES = {
    "bad": {'guard.py': 'def pushes(git_argv):\n    return "push" in git_argv\n'},
    "good": {'guard.py': 'def pushes(git_argv):\n    return git_argv[:2] == ["git", "push"]\n'},
}
