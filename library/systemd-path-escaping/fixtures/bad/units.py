import json


def path_changed(path):
    return "PathChanged=" + json.dumps(path)
