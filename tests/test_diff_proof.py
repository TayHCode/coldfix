import unittest

import diff_proof


class DiffProofTests(unittest.TestCase):
    def test_a_check_that_fails_before_and_passes_after_is_proven(self):
        self.assertEqual(diff_proof.prove(r"except\s*:", "except: pass", "except ValueError: pass"),
                         (dict(proof_tier="diff", pattern=r"except\s*:", before="FAIL", after="PASS"), ""))

    def test_a_check_that_flags_both_or_neither_side_is_not_proven(self):
        self.assertEqual(diff_proof.prove(r"except", "except: pass", "except ValueError: pass")[1], "check fails the corrected code")
        self.assertEqual(diff_proof.prove(r"finally", "except: pass", "except ValueError: pass")[1], "check misses the mistake")

    def test_a_broken_slow_or_oversized_check_is_refused(self):
        self.assertEqual(diff_proof.prove("(", "a", "b"), ({}, "check pattern does not compile"))
        self.assertEqual(diff_proof.prove(r"(a+)+b", "a" * 40, "c"), ({}, "check pattern timed out"))
        for pattern, before, after in (("", "a", "b"), ("a" * 301, "a", "b"), ("a", " ", "b"), ("a", "a" * 4001, "b"), ("a", "a\0", "b")):
            with self.subTest(pattern=pattern[:5], before=before[:5]):
                self.assertEqual(diff_proof.prove(pattern, before, after),
                                 ({}, "diff proof needs a short check pattern and both code snippets"))


if __name__ == "__main__":
    unittest.main()
