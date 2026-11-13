import re

PYTHON_FILE = re.compile(r"(?<!\S)\S*\.py(?!\S)")


def python_files(text):
    return PYTHON_FILE.findall(text)
