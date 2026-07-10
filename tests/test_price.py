import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Importing this module must NOT require the fast_flights library (lazy import).
from src.fetchers.fast_flights import parse_price, convert_to_cny, DEFAULT_FX_RATES  # noqa: E402


class TestParsePrice(unittest.TestCase):
    def test_cny_symbol_and_thousands(self):
        self.assertEqual(parse_price("¥1,234"), (1234, "CNY"))
        self.assertEqual(parse_price("￥12,340"), (12340, "CNY"))

    def test_usd_prefix(self):
        self.assertEqual(parse_price("US$530"), (530, "USD"))
        self.assertEqual(parse_price("$1,299"), (1299, "USD"))

    def test_three_letter_code(self):
        self.assertEqual(parse_price("CNY 1,234"), (1234, "CNY"))
        self.assertEqual(parse_price("1234 USD"), (1234, "USD"))

    def test_euro_gbp(self):
        self.assertEqual(parse_price("€980"), (980, "EUR"))
        self.assertEqual(parse_price("£450"), (450, "GBP"))

    def test_plain_number_uses_default(self):
        self.assertEqual(parse_price("1234"), (1234, "CNY"))
        self.assertEqual(parse_price("1234", default_currency="USD"), (1234, "USD"))

    def test_invalid(self):
        with self.assertRaises(ValueError):
            parse_price("Price unavailable")
        with self.assertRaises(ValueError):
            parse_price(None)


class TestConvert(unittest.TestCase):
    def test_cny_identity(self):
        self.assertEqual(convert_to_cny(1000, "CNY", DEFAULT_FX_RATES), 1000)

    def test_usd_to_cny(self):
        self.assertEqual(convert_to_cny(100, "USD", DEFAULT_FX_RATES), 720)

    def test_unknown_currency_passthrough(self):
        self.assertEqual(convert_to_cny(500, "XYZ", DEFAULT_FX_RATES), 500)


if __name__ == "__main__":
    unittest.main()
