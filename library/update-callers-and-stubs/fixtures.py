FIXTURES = {
    "bad": {'api.py': 'def send(message, retries):\n    return message, retries\n\nsend("hello", timeout=2)\n'},
    "good": {'api.py': 'def send(message, retries):\n    return message, retries\n\nsend("hello", retries=2)\n'},
}
