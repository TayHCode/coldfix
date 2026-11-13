import json
import os


def save(path, records):
    temporary = path + ".tmp"
    with open(temporary, "w") as stream:
        for record in records:
            stream.write(json.dumps(record) + "\n")
    os.replace(temporary, path)
