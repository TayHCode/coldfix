import io
import os
import unittest
from unittest import mock

import coldfix as c
import coldrecall_measure as cm
import failure_ingest as fi
import verify_pairs as vp

RECEIPT = dict(repo="example/project", pr=7, sha="a" * 40, model="m", verdict="FAIL", summary="s",
               findings=["Correct the calculation."])
FAIL = vp.Attempt("/logs/fail.log", "", "b" * 12, "FAIL", "", "AssertionError: 3 != 4", "2026-11-01T00:00:00Z")
PASS = vp.Attempt("/logs/pass.log", "", "c" * 12, "PASS", "", "OK", "2026-11-01T01:00:00Z")
PAIR = dict(slug="example/project", pr=7, repo="/repo", x="b" * 40, y="c" * 40, fail=FAIL, ok=PASS)
REVIEW_GROUP = "7bca703d9c30ccb925276bc078df747ccd29ef5a71da0461edda337e2bf9efba"
VERIFY_GROUP = "7f87484930bf2928ba0b125bd7b644a0a88564e7a96fcc1bdbfaab6a51255a1d"


class Repos:
    def evidence_diff(self, repo, before, after):
        return "diff"


def fleet(name):
    return mock.patch.dict(os.environ, {"COLDFIX_FLEET_CLI": name})


class FleetCliTests(unittest.TestCase):
    def test_setting_keeps_todays_labels_and_group_ids(self):
        with fleet("acme"):
            review = c.cold_review_events(RECEIPT)[0]
            verify = fi.record(vp.event(PAIR, Repos()))
            self.assertEqual((review["command"], review["group_id"]), ("acme review", REVIEW_GROUP))
            self.assertEqual((verify["command"], verify["group_id"]), ("acme verify", VERIFY_GROUP))
            self.assertEqual(fi.grouping_content(verify)[0], "acme verify")
            signature = c.signature(verify)
        for setting in ("", "other"):
            with fleet(setting):
                c.record_signature.cache_clear()
                self.assertEqual(fi.grouping_content(verify)[0], "acme verify")
                self.assertEqual(c.signature(verify), signature)

    def test_unset_setting_turns_fleet_integrations_off(self):
        with mock.patch.dict(os.environ), mock.patch.object(c, "process") as process:
            os.environ.pop("COLDFIX_FLEET_CLI", None)
            with self.assertRaisesRegex(c.ColdfixError, "model resolution is off: set COLDFIX_FLEET_CLI"):
                cm.resolve_model("builder-fast")
            with self.assertRaisesRegex(c.ColdfixError, "verify pair import is off: set COLDFIX_FLEET_CLI"):
                vp.run("/nonexistent", "/nonexistent", "/nonexistent", repos=Repos())
            with self.assertRaisesRegex(c.ColdfixError, "review receipt import is off: set COLDFIX_FLEET_CLI"):
                c.cold_review_events(RECEIPT)
            with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
                self.assertEqual(vp.main(["--projects", "/nonexistent"]), 1)
            self.assertEqual(stderr.getvalue(), "verify_pairs: verify pair import is off: set COLDFIX_FLEET_CLI\n")
            process.assert_not_called()

    def test_setting_must_be_a_plain_command_name(self):
        for name in ("a b", "x;rm", "$(id)", "a/b", "a.b", "acme\n"):
            with self.subTest(name=name), fleet(name), mock.patch.object(c, "process") as process:
                with self.assertRaisesRegex(c.ColdfixError, "plain command name"):
                    cm.resolve_model("builder-fast")
                process.assert_not_called()


if __name__ == "__main__":
    unittest.main()
