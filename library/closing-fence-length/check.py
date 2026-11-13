import json
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.getcwd())
FAILURE = "unclosed-fence"


def holds():
    from fences import headings
    return (headings("```\ncode\n````\n# After\n") == ["# After"]
            and headings("```\ncode\n```  \n# After\n") == ["# After"]
            and headings("```\n# inside\n```\n# Real\n") == ["# Real"])


def main():
    try:
        ok = holds()
    except Exception as error:
        print(json.dumps({"status": "INCOMPLETE", "reason": repr(error)}))
        return 2
    print(json.dumps({"status": "PASS"} if ok else {"status": "FAIL", "failure": FAILURE}))
    return 0 if ok else 1


sys.exit(main())
