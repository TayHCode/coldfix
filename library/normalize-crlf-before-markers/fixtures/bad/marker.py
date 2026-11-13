MARKER = "<!-- delivered -->"


def is_delivered(text):
    return text.startswith(MARKER + "\n")
