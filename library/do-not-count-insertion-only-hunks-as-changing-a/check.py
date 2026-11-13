import json
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.getcwd())
FAILURE = "insertion-counted-as-fix"


def holds():
    from hunks import changes_line
    insertion_after_reviewed_line = (10, 0, 11, 2)
    edit_of_reviewed_line = (10, 1, 10, 1)
    removal_of_reviewed_line = (9, 2, 9, 1)
    edit_next_to_reviewed_line = (11, 1, 11, 1)
    return (not changes_line(insertion_after_reviewed_line, 10)
            and changes_line(edit_of_reviewed_line, 10)
            and changes_line(removal_of_reviewed_line, 10)
            and not changes_line(edit_next_to_reviewed_line, 10))


def main():
    try:
        ok = holds()
    except Exception as error:
        print(json.dumps({"status": "INCOMPLETE", "reason": repr(error)}))
        return 2
    print(json.dumps({"status": "PASS"} if ok else {"status": "FAIL", "failure": FAILURE}))
    return 0 if ok else 1


sys.exit(main())
