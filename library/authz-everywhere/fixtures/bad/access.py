PUBLIC = "/static"


def is_public(path):
    return path.startswith(PUBLIC)
