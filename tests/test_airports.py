import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import airports  # noqa: E402


class TestAirportsParse(unittest.TestCase):
    def setUp(self):
        airports._reset_cache()

    def test_lookup_known_codes(self):
        # docs/airports.js ships these; parser must read the JS (unquoted keys).
        self.assertEqual(airports.lookup("YUL")["city_cn"], "蒙特利尔")
        self.assertEqual(airports.lookup("PEK")["city_cn"], "北京")
        self.assertIn("首都", airports.lookup("PEK")["name_cn"])
        # case-insensitive
        self.assertEqual(airports.lookup("yul")["city_cn"], "蒙特利尔")

    def test_lookup_unknown_returns_empty(self):
        self.assertEqual(airports.lookup("ZZZ"), {"city_cn": "", "name_cn": ""})
        self.assertEqual(airports.lookup(""), {"city_cn": "", "name_cn": ""})
        self.assertEqual(airports.lookup(None), {"city_cn": "", "name_cn": ""})

    def test_display_label(self):
        self.assertEqual(airports.display_label("YUL"), "蒙特利尔(YUL)")
        self.assertEqual(airports.display_label("PEK"), "北京(PEK)")
        # unknown -> bare code, never raises
        self.assertEqual(airports.display_label("ZZZ"), "ZZZ")
        self.assertEqual(airports.display_label(""), "")

    def test_regex_parser_handles_js_object(self):
        text = (
            "var AIRPORTS = [\n"
            "  // comment\n"
            '  { iata: "ABC", name_cn: "测试机场", city_cn: "测试城", city_en: "Test" },\n'
            "];\n"
        )
        table = airports._parse_regex(text)
        self.assertEqual(table["ABC"], {"city_cn": "测试城", "name_cn": "测试机场"})

    def test_missing_file_returns_empty(self):
        # _load_table must never raise even if airports.js is unreadable.
        orig = airports._airports_js_path
        airports._airports_js_path = lambda: "/no/such/airports.js"
        try:
            airports._reset_cache()
            self.assertEqual(airports.lookup("YUL"), {"city_cn": "", "name_cn": ""})
        finally:
            airports._airports_js_path = orig
            airports._reset_cache()


if __name__ == "__main__":
    unittest.main()
