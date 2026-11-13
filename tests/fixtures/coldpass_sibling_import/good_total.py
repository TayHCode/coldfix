from .helpers import double


def total(values):
    return sum(double(value) for value in values)
