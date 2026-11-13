import json


def save(path, records):
    with open(path, "w") as stream:
        for record in records:
            stream.write(json.dumps(record) + "\n")
