import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import Route, resolve_dates, load_config  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _route(dates):
    return Route(id="t", origin="SHA", dest="NRT", dates=dates)


class TestResolveDates(unittest.TestCase):
    def setUp(self):
        self.today = date(2026, 1, 1)

    def test_fixed(self):
        r = _route({"mode": "fixed", "fixed_dates": ["2026-10-03", "2026-10-01", "2026-10-02"]})
        self.assertEqual(resolve_dates(r, self.today),
                         ["2026-10-01", "2026-10-02", "2026-10-03"])

    def test_rolling(self):
        r = _route({"mode": "rolling", "depart_in_days": 3})
        # future days: today+1 .. today+3
        self.assertEqual(resolve_dates(r, self.today),
                         ["2026-01-02", "2026-01-03", "2026-01-04"])

    def test_both_union_dedup(self):
        r = _route({"mode": "both", "depart_in_days": 2,
                    "fixed_dates": ["2026-01-03", "2026-05-01"]})
        # rolling -> 01-02, 01-03 ; fixed -> 01-03, 05-01 ; union dedup+sorted
        self.assertEqual(resolve_dates(r, self.today),
                         ["2026-01-02", "2026-01-03", "2026-05-01"])

    def test_empty_rolling(self):
        r = _route({"mode": "rolling"})  # no depart_in_days
        self.assertEqual(resolve_dates(r, self.today), [])


class TestLoadConfig(unittest.TestCase):
    def test_load_yaml_or_json(self):
        cfg = load_config(os.path.join(ROOT, "config.yaml"))
        self.assertEqual(cfg.timezone, "Asia/Shanghai")
        ids = [r.id for r in cfg.routes]
        self.assertEqual(ids, ["sha-nrt", "pek-cdg", "sha-can"])
        nrt = cfg.route_by_id("sha-nrt")
        self.assertEqual(nrt.origin, "SHA")
        self.assertEqual(nrt.dest, "NRT")
        self.assertIn("MM", nrt.airlines["blacklist"])
        self.assertTrue(nrt.enabled)
        self.assertFalse(cfg.route_by_id("sha-can").enabled)

    def test_json_fallback_path(self):
        # Force the no-PyYAML path by loading config.json directly.
        cfg = load_config(os.path.join(ROOT, "config.json"))
        self.assertEqual([r.id for r in cfg.routes], ["sha-nrt", "pek-cdg", "sha-can"])
        self.assertEqual(cfg.cross_check["daily_quota"], 3)


if __name__ == "__main__":
    unittest.main()
