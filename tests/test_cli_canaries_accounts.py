import unittest

from test_cli import CliCase


class LessonCanaryAccountsTests(CliCase):
    def test_privacy_canaries_block_lesson_embedded_scripts_and_briefing_for_accounts(self):
        self.assert_lesson_canaries_blocked("accounts")


if __name__ == "__main__":
    unittest.main()
