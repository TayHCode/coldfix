import re

PYTHON_FILE = re.compile(r"(?:\S+/)*\S+\.py\b")


def python_files(text):
    return PYTHON_FILE.findall(text)
