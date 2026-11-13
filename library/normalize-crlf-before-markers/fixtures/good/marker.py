MARKER = "<!-- delivered -->"


def is_delivered(text):
    return text.replace("\r\n", "\n").startswith(MARKER + "\n")
