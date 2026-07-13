import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Importing this module must NOT require the fast_flights library (lazy import).
from src.fetchers.fast_flights import (  # noqa: E402
    parse_price, convert_to_cny, DEFAULT_FX_RATES, parse_depart_time,
)


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


class TestParseDepartTime(unittest.TestCase):
    def test_english_am(self):
        self.assertEqual(parse_depart_time("8:30 AM on Thu, Aug 13"), "08:30")
        self.assertEqual(parse_depart_time("9:05 AM"), "09:05")

    def test_english_pm(self):
        self.assertEqual(parse_depart_time("1:05 PM"), "13:05")
        self.assertEqual(parse_depart_time("8:55 PM on Fri, Sep 1"), "20:55")

    def test_noon_and_midnight_boundaries(self):
        self.assertEqual(parse_depart_time("12:00 AM"), "00:00")   # 12AM -> midnight
        self.assertEqual(parse_depart_time("12:30 AM"), "00:30")
        self.assertEqual(parse_depart_time("12:00 PM"), "12:00")   # 12PM -> noon
        self.assertEqual(parse_depart_time("12:45 PM"), "12:45")

    def test_already_24h_no_meridiem(self):
        self.assertEqual(parse_depart_time("20:55"), "20:55")
        self.assertEqual(parse_depart_time("06:00"), "06:00")

    def test_lowercase_meridiem(self):
        self.assertEqual(parse_depart_time("7:15 pm"), "19:15")

    def test_unparsable_returns_empty(self):
        self.assertEqual(parse_depart_time(""), "")
        self.assertEqual(parse_depart_time(None), "")
        self.assertEqual(parse_depart_time("n/a"), "")
        self.assertEqual(parse_depart_time("morning"), "")


class TestConvert(unittest.TestCase):
    def test_cny_identity(self):
        self.assertEqual(convert_to_cny(1000, "CNY", DEFAULT_FX_RATES), 1000)

    def test_usd_to_cny(self):
        self.assertEqual(convert_to_cny(100, "USD", DEFAULT_FX_RATES), 720)

    def test_unknown_currency_passthrough(self):
        self.assertEqual(convert_to_cny(500, "XYZ", DEFAULT_FX_RATES), 500)


if __name__ == "__main__":
    unittest.main()
