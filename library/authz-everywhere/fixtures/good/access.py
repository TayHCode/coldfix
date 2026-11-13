PUBLIC = "/static"


def is_public(path):
    return path == PUBLIC or path.startswith(PUBLIC + "/")
