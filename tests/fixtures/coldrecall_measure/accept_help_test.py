import re
import subprocess
import sys

done = subprocess.run([sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-p", "test_help.py", "-v"], text=True, capture_output=True)
ran = re.search(r"^Ran (\d+) tests?", done.stderr, re.M)
sys.exit(0 if done.returncode == 0 and ran and int(ran.group(1)) >= 1 else 1)
