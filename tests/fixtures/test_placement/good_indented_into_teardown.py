import unittest


class ToolStatsTest(unittest.TestCase):
  def setUp(self):
    self.calls = []

  def tearDown(self):
    self.calls.clear()

  # ok: test-placement
  def test_records_a_call(self):
    self.calls.append("search")
    self.assertEqual(self.calls, ["search"])

  # ok: test-placement
  def test_starts_empty(self):
    self.assertEqual(self.calls, [])
