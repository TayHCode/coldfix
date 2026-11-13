import json
import os
import unittest
from unittest.mock import patch

import coldfix as c
from test_cli import CliCase

LISTED = ("alpha-101", "bravo-202", "Charlie-303")
FORMS = ("{h}", "{H}", "Deploy to {h}.", "ssh {h}", "ssh user@{h}", "user@{h}", "https://{h}:8080/x",
         "{h}.tail1a2b3.ts.net", "{h}.local", "{h}.lan", "{h}_backup", "({h})",
         "_{h}_", "logs on {h}'s disk", "{h}_backup.invalid", "old-{h}", "x.{h}.invalid")
LISTED_ONLY_FORMS = ("ssh {h}(", "see /{h}", "#{h}")
FALSE_HITS = ("cv-16", "agent-bench-2", "runner.invalid", "fix (cv-16) and CI-03",
              "runner-warm-1 and original-0", "https://runner.invalid/job")
UNLISTED_IN_HOST_CONTEXT = ("ssh delta-404", "ssh delta-404(", "user@delta-404", "https://delta-404/x",
                            "delta-404.tail1a2b3.ts.net")


class PrivateHostListTests(CliCase):
    def setUp(self):
        super().setUp()
        self.missing = str(self.cwd / "no-such-hosts")
        self.listed = self.cwd / "private-hosts"
        self.listed.write_text("# private hosts\n" + "\n".join(LISTED) + "\n\n")

    def hosts(self, value, path):
        with patch.dict(os.environ, COLDFIX_PRIVATE_HOSTS=str(path)):
            return [c.privacy_counts(value, filename_exempt=exempt).get("hostname", 0) for exempt in (False, True)]

    def test_every_listed_host_is_caught_in_every_form_the_shape_rule_caught(self):
        for host in LISTED:
            for form in FORMS:
                value = form.format(h=host.lower(), H=host.upper())
                with self.subTest(value=value):
                    before = self.hosts(value, self.missing)
                    after = self.hosts(value, self.listed)
                    self.assertTrue(all(before), before)
                    self.assertTrue(all(after), after)

    def test_a_listed_host_is_caught_where_the_shape_rule_skipped_it(self):
        for host in LISTED:
            for form in LISTED_ONLY_FORMS:
                value = form.format(h=host.lower())
                with self.subTest(value=value):
                    self.assertTrue(all(self.hosts(value, self.listed)))

    def test_task_ids_fixture_names_and_invalid_tld_pass_only_with_a_list(self):
        for value in FALSE_HITS:
            with self.subTest(value=value):
                self.assertTrue(all(self.hosts(value, self.missing)))
                self.assertEqual(self.hosts(value, self.listed), [0, 0])

    def test_an_unlisted_host_shaped_name_in_a_host_context_is_still_caught(self):
        for value in UNLISTED_IN_HOST_CONTEXT:
            with self.subTest(value=value):
                self.assertTrue(all(self.hosts(value, self.listed)))
        self.assertEqual(self.hosts("delta-404", self.listed), [0, 0])

    def test_an_empty_list_keeps_the_shape_rule(self):
        empty = self.cwd / "empty-hosts"
        empty.write_text("# nothing yet\n\n")
        self.assertTrue(all(self.hosts("runner-warm-1", empty)))

    def test_editing_the_list_in_a_running_process_takes_effect(self):
        self.assertEqual(self.hosts("runner-warm-1", self.listed), [0, 0])
        self.listed.write_text("")
        self.assertTrue(all(self.hosts("runner-warm-1", self.listed)))
        self.listed.write_text("runner-warm-1\n")
        self.assertTrue(all(self.hosts("runner-warm-1", self.listed)))
        stamp = self.listed.stat()
        self.listed.write_text("runner-warm-2\n")
        os.utime(self.listed, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        self.assertEqual(self.hosts("runner-warm-1", self.listed), [0, 0])
        self.assertTrue(all(self.hosts("ssh runner-warm-2", self.listed)))
        self.assertTrue(all(self.hosts("runner-warm-2", self.listed)))

    def test_doctor_warns_when_the_host_list_is_missing_and_counts_it_when_present(self):
        with patch.dict(os.environ, COLDFIX_PRIVATE_HOSTS=self.missing):
            self.assertIn(f"coldfix: private_hosts: warning: {self.missing} is missing or empty; "
                          "the name-NN hostname shape rule runs unnarrowed\n", self.run_cli("doctor").stderr)
        with patch.dict(os.environ, COLDFIX_PRIVATE_HOSTS=str(self.listed)):
            result = self.run_cli("doctor")
            self.assertIn(f"coldfix: private_hosts: 3 hosts listed in {self.listed}\n", result.stderr)
            self.assertNotIn("private_hosts", json.loads(result.stdout))


if __name__ == "__main__":
    unittest.main()
