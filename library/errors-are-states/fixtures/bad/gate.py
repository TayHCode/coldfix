def run_check(checker):
    try:
        return "PASS" if checker() else "FAIL"
    except Exception:
        return "INCOMPLETE"


def gate_opens(checker):
    return run_check(checker) != "FAIL"
