def failures_since(runs, since):
    return [run for run in runs if run["conclusion"] == "failure" and run["updated_at"] >= since]
