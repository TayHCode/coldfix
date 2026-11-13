from pathlib import Path


def load_tests(loader, tests, pattern):
    directory = str(Path(__file__).parent / "tests")
    return loader.discover(directory, pattern=pattern or "test*.py", top_level_dir=directory)
