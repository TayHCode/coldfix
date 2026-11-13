from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def is_checkout(path):
    found = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return found.returncode == 0 and Path(found.stdout.strip()).resolve() == path.resolve()


def checkout(dest):
    if is_checkout(ROOT):
        dest.mkdir()
        for name in git(ROOT, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split("\0"):
            if name and (ROOT / name).is_file():
                (dest / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / name, dest / name)
    else:
        shutil.copytree(ROOT, dest, ignore=shutil.ignore_patterns(".git", ".worktrees"))
    git(dest, "init", "--quiet")
    git(dest, "add", "-A")
    git(dest, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "core.hooksPath=/dev/null",
        "commit", "--quiet", "-m", "Working-tree snapshot")
    return dest
