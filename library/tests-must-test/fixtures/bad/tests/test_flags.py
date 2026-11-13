import unittest

from flags import parse_flag


class FlagTests(unittest.TestCase):
    def test_yes_is_true(self):
        self.assertTrue(parse_flag("yes"))


def test_empty_is_false():
    assert not parse_flag("")
