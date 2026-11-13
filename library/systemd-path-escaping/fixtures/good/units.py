def path_changed(path):
    escaped = path.replace("\\", "\\x5c").replace(" ", "\\x20").replace("%", "%%")
    return "PathChanged=" + escaped
