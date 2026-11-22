import unittest

from agent_runtime import safe_calculate


class SafeCalculatorTests(unittest.TestCase):
    def test_arithmetic_and_precedence(self):
        self.assertEqual(safe_calculate("(125 * 1.15) / 5"), 28.75)

    def test_approved_math_functions(self):
        self.assertEqual(safe_calculate("sqrt(81) + round(2.4)"), 11)

    def test_constants(self):
        self.assertAlmostEqual(safe_calculate("pi * 2"), 6.283185307179586)

    def test_rejects_code_execution(self):
        with self.assertRaises(ValueError):
            safe_calculate("__import__('os').system('whoami')")

    def test_rejects_attribute_access(self):
        with self.assertRaises(ValueError):
            safe_calculate("(1).__class__")

    def test_rejects_large_exponents(self):
        with self.assertRaises(ValueError):
            safe_calculate("2 ** 99")

    def test_rejects_non_finite_result(self):
        with self.assertRaises(ValueError):
            safe_calculate("1 / 0")

    def test_rejects_invalid_syntax_as_value_error(self):
        with self.assertRaises(ValueError):
            safe_calculate("2 +")

    def test_rejects_math_domain_errors(self):
        with self.assertRaises(ValueError):
            safe_calculate("sqrt(-1)")


if __name__ == "__main__":
    unittest.main()
