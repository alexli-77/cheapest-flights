import json
import os
import sys
import tempfile
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import (  # noqa: E402
    Route, resolve_dates, load_config, _rolling_offsets, MAX_DATES_PER_ROUTE,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _route(dates, rid="t"):
    return Route(id=rid, origin="SHA", dest="NRT", dates=dates)


class TestResolveDates(unittest.TestCase):
    def setUp(self):
        self.today = date(2026, 1, 1)

    def test_fixed(self):
        r = _route({"mode": "fixed", "fixed_dates": ["2026-10-03", "2026-10-01", "2026-10-02"]})
        self.assertEqual(resolve_dates(r, self.today),
                         ["2026-10-01", "2026-10-02", "2026-10-03"])

    def test_rolling_small_scalar(self):
        # N <= 30 -> every future day today+1..today+N.
        r = _route({"mode": "rolling", "depart_in_days": 3})
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


class TestRollingOffsets(unittest.TestCase):
    def test_scalar_90_samples(self):
        # 1..30 daily (30) + 33,36,...,90 step 3 (20) = 50 offsets.
        offs = _rolling_offsets(90)
        self.assertEqual(offs[:30], list(range(1, 31)))
        self.assertEqual(offs[30:], list(range(33, 91, 3)))
        self.assertEqual(len(offs), 50)

    def test_scalar_30_all_daily(self):
        self.assertEqual(_rolling_offsets(30), list(range(1, 31)))

    def test_scalar_45(self):
        # 30 daily + 33,36,39,42,45 = 35 offsets
        offs = _rolling_offsets(45)
        self.assertEqual(offs[30:], [33, 36, 39, 42, 45])
        self.assertEqual(len(offs), 35)

    def test_list_form_explicit_offsets(self):
        # Legacy list form [7,14,30] -> those exact offsets.
        self.assertEqual(_rolling_offsets([7, 14, 30]), [7, 14, 30])

    def test_list_form_dedup_sorted(self):
        self.assertEqual(_rolling_offsets([30, 7, 7, 14]), [7, 14, 30])

    def test_cap_enforced(self):
        # A huge scalar must be truncated to MAX_DATES_PER_ROUTE concrete dates.
        r = _route({"mode": "rolling", "depart_in_days": 365})
        got = resolve_dates(r, date(2026, 1, 1))
        self.assertEqual(len(got), MAX_DATES_PER_ROUTE)


class TestLoadConfig(unittest.TestCase):
    def test_load_repo_config(self):
        # Load the real repo config (json-preferred). Assert structural
        # invariants rather than specific route ids (routes change over time).
        cfg = load_config(os.path.join(ROOT, "config.yaml"))
        self.assertEqual(cfg.timezone, "Asia/Shanghai")
        self.assertTrue(cfg.routes)
        for r in cfg.routes:
            self.assertTrue(r.id and r.origin and r.dest)
        self.assertEqual(cfg.cross_check["daily_quota"], 3)

    def test_config_json_is_authoritative(self):
        # When both files exist and differ, config.json must win — even when the
        # .yaml path is passed and PyYAML is available.
        d = tempfile.mkdtemp()
        jpath = os.path.join(d, "config.json")
        ypath = os.path.join(d, "config.yaml")
        with open(jpath, "w", encoding="utf-8") as f:
            json.dump({"timezone": "Asia/Shanghai",
                       "routes": [{"id": "json-route", "from": "AAA", "to": "BBB"}]}, f)
        with open(ypath, "w", encoding="utf-8") as f:
            f.write("timezone: Asia/Shanghai\nroutes:\n  - id: yaml-route\n    from: CCC\n    to: DDD\n")
        cfg = load_config(ypath)
        self.assertEqual([r.id for r in cfg.routes], ["json-route"])

    def test_yaml_fallback_when_no_json(self):
        # No .json sibling -> parse the YAML mirror.
        d = tempfile.mkdtemp()
        ypath = os.path.join(d, "config.yaml")
        with open(ypath, "w", encoding="utf-8") as f:
            f.write("timezone: Asia/Shanghai\nroutes:\n  - id: yaml-only\n    from: CCC\n    to: DDD\n")
        cfg = load_config(ypath)
        self.assertEqual([r.id for r in cfg.routes], ["yaml-only"])


if __name__ == "__main__":
    unittest.main()
