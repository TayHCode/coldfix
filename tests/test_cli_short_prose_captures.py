import json
import unittest

from test_cli import CliCase


class ShortProseCaptureTests(CliCase):
    def export_briefing_after_capture(self, error, failure, code):
        revision = self.author_lesson()
        self.capture(error=error)
        revised = json.loads(self.run_cli('lesson', '--revise', revision, '--failure', failure).stdout)['id']
        repo, before, after = self.history()
        proven = json.loads(self.run_replay(revised, repo, before, after, '--status', 'active').stdout)['id']
        self.prove_elsewhere(proven)
        return self.run_cli('export', '--format', 'briefing', '--output', 'briefing.md', code=code)

    def test_short_word_after_prose_prone_keyword_does_not_block_unrelated_lessons(self):
        for error in ('so \\"bearer abc\\" captures it', 'Authorization: Bearer abc', 'token=abc', '--token abc',
                      'api_key: abc', 'authorization: "abc"', 'token=abc\r\nnext line'):
            with self.subTest(error=error):
                self.setUp()
                self.export_briefing_after_capture(error, 'Use abc for login.', 0)
                self.assertIn('Use abc for login.', (self.cwd / 'briefing.md').read_text())

    def test_short_value_after_password_like_keyword_still_blocks(self):
        for error, value in (('password=abc', 'abc'), ('secret: abc', 'abc'), ('--password abc', 'abc'),
                             ('credential="abc"', 'abc'), ('passwd=a', 'a'), ('password_token=abc', 'abc')):
            with self.subTest(error=error):
                self.setUp()
                result = self.export_briefing_after_capture(error, 'Use ' + value + ' for login.', 2)
                self.assertIn('captured_credential', result.stderr)
                self.assertFalse((self.cwd / 'briefing.md').exists())

    def test_other_short_or_longer_values_after_prose_prone_keyword_still_block(self):
        for error, value in (('so \\"bearer ab1\\" here', 'ab1'), ('token=123', '123'), ('token=a_b', 'a_b'),
                             ('so \\"bearer abcd\\" here', 'abcd'), ('Authorization: Bearer Wq7rT2pZx9', 'Wq7rT2pZx9')):
            with self.subTest(error=error):
                self.setUp()
                result = self.export_briefing_after_capture(error, 'Use ' + value + ' for login.', 2)
                self.assertIn('captured_credential', result.stderr)
                self.assertFalse((self.cwd / 'briefing.md').exists())

    def test_plain_value_followed_by_a_word_after_prose_prone_keyword_still_blocks(self):
        for error, value in (('--token zqxvwkpt for prod', 'zqxvwkpt'), ('token=839251 by default', '839251'),
                             ('tokens=0 by default', '0'), ('token: zqxvwkpt in staging', 'zqxvwkpt'),
                             ('bearer zqxvwkpt was sent', 'zqxvwkpt'), ('Authorization: Bearer zqxvwkpt then retry', 'zqxvwkpt'),
                             ('token=bearer zqxvwkpt', 'zqxvwkpt'), ('token: Bearer zqxvwkpt', 'zqxvwkpt'),
                             ('--token Bearer zqxvwkpt', 'zqxvwkpt'), ('token=bearer zqxvwkpt was sent', 'zqxvwkpt'),
                             ('password-token=abc', 'abc'), ('--token secret was rejected', 'secret')):
            with self.subTest(error=error):
                self.setUp()
                result = self.export_briefing_after_capture(error, 'Use ' + value + ' for login.', 2)
                self.assertIn('captured_credential', result.stderr)
                self.assertFalse((self.cwd / 'briefing.md').exists())

    def test_credential_form_in_the_lesson_itself_is_still_refused(self):
        for failure in ('Login sent password=abc today.', 'Login sent token=abc', 'Login sent "bearer abc" today.'):
            with self.subTest(failure=failure):
                self.setUp()
                result = self.export_briefing_after_capture('so \\"bearer abc\\" captures it', failure, 2)
                self.assertIn('credential', result.stderr)
                self.assertFalse((self.cwd / 'briefing.md').exists())


if __name__ == '__main__':
    unittest.main()
