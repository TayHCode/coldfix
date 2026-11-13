import unittest


class MiscTest(unittest.TestCase):
    def test_random_with_tensor_input(self):
        self.assertEqual(len([0.5, 0.25]), 2)

        # ruleid: test-placement
        def test_random_with_tensor_input(self):
            self.assertEqual(len([0.5, 0.25][:1]), 1)

    def test_zeros(self):
        self.assertEqual([0] * 2, [0, 0])

        # ruleid: test-placement
        def test_zeros_with_explicit_size(self):
            self.assertEqual([0] * 3, [0, 0, 0])
