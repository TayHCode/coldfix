from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve().relative_to(ROOT).as_posix()
FIX = ("exported files must not assume one machine's layout: read the path from a COLDFIX_* variable or an argument "
       "and set the deployment default in the private service wrapper, start interpreters with sys.executable, "
       "and write /home/example in fixtures")
LITERALS = re.compile(r"/home/(?!example(?:$|[/\s\"'`]))|(?:~|\$\{?HOME\}?)/projects\b|home\(\)\s*/\s*[\"']projects\b|"
                      r"\.local/bin\b|projects/coldlocker\b|/usr(?:/local)?/bin/python")

MODELS_FILE_DEFAULT = '''
def routing():
    path = Path(os.environ.get("FLEET_MODELS_FILE") or Path.home() / "projects/coldlocker/models.yaml")
    if not path.exists():
        return {}, set()
'''
MODELS_FILE_FIXED = MODELS_FILE_DEFAULT.replace('Path.home() / "projects/coldlocker/models.yaml"', '""')
VM_RUN_COMMAND_DEFAULT = '''
job = ['sudo', '-n', 'bash', os.environ.get('COLDFIX_VM_RUN_COMMAND', '/home/user/bootstrap/run-command.sh'),
       guest, str(script)]
'''
VM_RUN_COMMAND_FIXED = VM_RUN_COMMAND_DEFAULT.replace("/home/user/bootstrap/run-command.sh", "run-command.sh")
CHECKOUTS_DEFAULT = '''
summary["spread"] = learn_replay.spread(root, args.projects or Path.home() / "projects")
'''
CHECKOUTS_FIXED = CHECKOUTS_DEFAULT.replace('Path.home() / "projects"', 'os.environ.get("COLDFIX_CHECKOUTS")')
FAKE_ACT_DEFAULT = '''
act.write_text('#!/usr/bin/python3\\n' + CHECK)
'''
FAKE_ACT_FIXED = FAKE_ACT_DEFAULT.replace("'#!/usr/bin/python3\\n'", "'#!' + sys.executable + '\\n'")
SERVICE_PATH_DEFAULT = '''
PATH="$HOME/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
'''
SERVICE_PATH_FIXED = SERVICE_PATH_DEFAULT.replace("$HOME/.local/bin:", "")


def literals(text):
    return [number for number, line in enumerate(text.split("\n"), 1) if LITERALS.search(line)]


def exported():
    return [name for name in (ROOT / "public-export.txt").read_text().split() if name != SELF]


class EnvironmentLiteralTests(unittest.TestCase):
    def test_exported_files_assume_no_machine_layout(self):
        offenders = [f"{name}:{line}" for name in exported() for line in literals((ROOT / name).read_text())]
        self.assertEqual(offenders, [], FIX)

    def test_rejects_the_shapes_that_shipped_and_accepts_their_fixes(self):
        for before, after, line in ((MODELS_FILE_DEFAULT, MODELS_FILE_FIXED, 3),
                                    (VM_RUN_COMMAND_DEFAULT, VM_RUN_COMMAND_FIXED, 2),
                                    (CHECKOUTS_DEFAULT, CHECKOUTS_FIXED, 2),
                                    (FAKE_ACT_DEFAULT, FAKE_ACT_FIXED, 2),
                                    (SERVICE_PATH_DEFAULT, SERVICE_PATH_FIXED, 2)):
            with self.subTest(before=before.strip()):
                self.assertEqual(literals(before), [line])
                self.assertEqual(literals(after), [])

    def test_placeholder_home_and_venv_interpreters_are_allowed(self):
        self.assertEqual(literals("raw guest output with a path /home/example/private"), [])
        self.assertEqual(literals("/home/example"), [])
        self.assertEqual(literals("/home/example-team/private\n/home/examplecorp\n/home/example.test\n"
                                  "/home/example+user/private\n/home/example@company/private"), [1, 2, 3, 4, 5])
        self.assertEqual(literals('python = abspath(venv) + "/bin/python"'), [])
        self.assertEqual(literals("#!/usr/bin/env python3"), [])


if __name__ == "__main__":
    unittest.main()
