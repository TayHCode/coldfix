def passed(results, test_id):
    method = test_id.rsplit(".", 1)[-1]
    return any(name.rsplit(".", 1)[-1] == method and outcome == "ok" for name, outcome in results.items())
