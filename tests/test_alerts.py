import os
import sys
import json
import tempfile
import unittest
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import Route, Config  # noqa: E402
from src.models import now_shanghai  # noqa: E402
from src.alerts.rules import (  # noqa: E402
    REGISTRY, RuleContext, BelowTargetRule, DropPctRule, HistoricalLowRule,
    HISTORICAL_MIN_DAYS,
)
from src.alerts.engine import run_alerts  # noqa: E402


def _route(rid="sha-nrt", target=1500, drop=15):
    return Route(id=rid, origin="SHA", dest="NRT", dates={},
                 target_price=target, drop_alert_pct=drop)


def _cfg(routes, alerts=None):
    return Config(
        timezone="Asia/Shanghai", defaults={}, routes=routes, cross_check={},
        alerts=alerts or {"max_urgent_per_day": 5, "urgent_dedup_hours": 24,
                          "daily_digest": True},
        notifiers={}, dashboard={}, raw={},
    )


def _series(prices, start="2026-07-01"):
    # prices -> list of {fetch_date, price, currency}
    from datetime import date, timedelta as td
    y, m, d = (int(x) for x in start.split("-"))
    base = date(y, m, d)
    return [{"fetch_date": (base + td(days=i)).isoformat(),
             "price": p, "currency": "CNY"} for i, p in enumerate(prices)]


def _node(series):
    latest = None
    if series:
        latest = {"fetch_date": series[-1]["fetch_date"],
                  "price": series[-1]["price"], "currency": "CNY"}
    hlow = min(series, key=lambda s: s["price"]) if series else None
    return {"latest": latest,
            "historical_low": ({"fetch_date": hlow["fetch_date"], "price": hlow["price"],
                                "currency": "CNY"} if hlow else None),
            "series": series}


def _ctx(route, series):
    return RuleContext(route=route, route_id=route.id, depart_date="2026-10-01",
                       node=_node(series), storage=None, cfg=_cfg([route]))


class TestRules(unittest.TestCase):
    def test_registry_has_three_rules(self):
        self.assertIn("below_target", REGISTRY)
        self.assertIn("drop_pct", REGISTRY)
        self.assertIn("historical_low", REGISTRY)

    # ------------------------------------------------------- below_target
    def test_below_target_fires(self):
        a = BelowTargetRule().evaluate(_ctx(_route(target=1500), _series([1400])))
        self.assertIsNotNone(a)
        self.assertEqual(a.level, "urgent")
        self.assertEqual(a.price, 1400)
        self.assertEqual(a.target_price, 1500)

    def test_below_target_boundary_not_fire(self):
        # price == target must NOT fire (strictly below only)
        self.assertIsNone(BelowTargetRule().evaluate(_ctx(_route(target=1500), _series([1500]))))
        self.assertIsNone(BelowTargetRule().evaluate(_ctx(_route(target=1500), _series([1600]))))

    def test_below_target_no_target(self):
        self.assertIsNone(BelowTargetRule().evaluate(_ctx(_route(target=None), _series([100]))))

    # ------------------------------------------------------- drop_pct
    def test_drop_pct_normal(self):
        # 1000 -> 850 = 15% drop, threshold 15 -> fires normal (<2x)
        a = DropPctRule().evaluate(_ctx(_route(drop=15), _series([1000, 850])))
        self.assertIsNotNone(a)
        self.assertEqual(a.level, "normal")
        self.assertEqual(a.prev_price, 1000)
        self.assertEqual(a.price, 850)

    def test_drop_pct_urgent_double_threshold(self):
        # 1000 -> 690 = 31% drop >= 2*15 -> urgent
        a = DropPctRule().evaluate(_ctx(_route(drop=15), _series([1000, 690])))
        self.assertEqual(a.level, "urgent")

    def test_drop_pct_below_threshold_not_fire(self):
        # 1000 -> 900 = 10% < 15
        self.assertIsNone(DropPctRule().evaluate(_ctx(_route(drop=15), _series([1000, 900]))))

    def test_drop_pct_needs_two_points(self):
        self.assertIsNone(DropPctRule().evaluate(_ctx(_route(drop=15), _series([900]))))

    # ------------------------------------------------------- historical_low
    def test_historical_low_cold_start_not_fire(self):
        # fewer than 7 days of history => cold start, never fires
        series = _series([1000, 900, 800, 700, 600, 500])  # 6 days, last is a low
        self.assertEqual(len(series), HISTORICAL_MIN_DAYS - 1)
        self.assertIsNone(HistoricalLowRule().evaluate(_ctx(_route(), series)))

    def test_historical_low_fires_new_low(self):
        # 7 days, last sets a new all-time low
        series = _series([1000, 950, 900, 950, 920, 930, 880])
        a = HistoricalLowRule().evaluate(_ctx(_route(), series))
        self.assertIsNotNone(a)
        self.assertEqual(a.level, "urgent")
        self.assertEqual(a.price, 880)

    def test_historical_low_not_new_low(self):
        # 7 days but last is not below the prior minimum (900 already seen)
        series = _series([900, 950, 1000, 950, 920, 930, 950])
        self.assertIsNone(HistoricalLowRule().evaluate(_ctx(_route(), series)))


class TestEngineMerge(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp()

    def _summary(self, series_map):
        # series_map: {route_id: {depart_date: [prices]}}
        routes = {}
        for rid, dd_map in series_map.items():
            dd_out = {}
            for dd, prices in dd_map.items():
                dd_out[dd] = _node(_series(prices))
            routes[rid] = {"depart_dates": dd_out}
        return {"generated_at": "2026-07-10T09:00:00+08:00", "routes": routes, "meta": {}}

    def test_normal_all_into_digest(self):
        cfg = _cfg([_route(target=100, drop=15)])  # target low so below_target won't fire
        summary = self._summary({"sha-nrt": {"2026-10-01": [1000, 850]}})  # 15% drop -> normal
        alerts = run_alerts(cfg, None, summary, state_dir=self.state)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].level, "normal")

    def test_urgent_dedup_24h(self):
        cfg = _cfg([_route(target=1500)])
        summary = self._summary({"sha-nrt": {"2026-10-01": [1400]}})  # below target -> urgent
        a1 = run_alerts(cfg, None, summary, state_dir=self.state)
        self.assertEqual(sum(1 for a in a1 if a.level == "urgent"), 1)
        # second run within 24h -> deduped -> downgraded to normal
        a2 = run_alerts(cfg, None, summary, state_dir=self.state)
        self.assertEqual(sum(1 for a in a2 if a.level == "urgent"), 0)
        self.assertEqual(len(a2), 1)
        # a sent-state file should exist
        self.assertTrue(os.path.exists(os.path.join(self.state, "alert_sent.json")))

    def test_urgent_cap_downgrade(self):
        # 3 urgent-eligible routes but cap = 1 -> only 1 stays urgent
        routes = [_route(rid=f"r{i}", target=1500) for i in range(3)]
        cfg = _cfg(routes, alerts={"max_urgent_per_day": 1, "urgent_dedup_hours": 24})
        summary = self._summary({f"r{i}": {"2026-10-01": [1400]} for i in range(3)})
        alerts = run_alerts(cfg, None, summary, state_dir=self.state)
        urgent = [a for a in alerts if a.level == "urgent"]
        self.assertEqual(len(urgent), 1)
        self.assertEqual(len(alerts), 3)  # the other two downgraded, still present

    def test_failure_watchdog_urgent(self):
        # consecutive_failures >= 2 -> urgent system alert
        with open(os.path.join(self.state, "failures.json"), "w", encoding="utf-8") as f:
            json.dump({"sha-nrt": {"consecutive_failures": 3}}, f)
        cfg = _cfg([_route(target=100, drop=99)])  # no price rules fire
        summary = self._summary({"sha-nrt": {"2026-10-01": [1000]}})
        alerts = run_alerts(cfg, None, summary, state_dir=self.state)
        sysa = [a for a in alerts if a.rule_id == "source_failure"]
        self.assertEqual(len(sysa), 1)
        self.assertEqual(sysa[0].level, "urgent")
        self.assertIn("连续 3 天", sysa[0].message)

    def test_failure_below_threshold_no_alert(self):
        with open(os.path.join(self.state, "failures.json"), "w", encoding="utf-8") as f:
            json.dump({"sha-nrt": {"consecutive_failures": 1}}, f)
        cfg = _cfg([_route(target=100, drop=99)])
        summary = self._summary({"sha-nrt": {"2026-10-01": [1000]}})
        alerts = run_alerts(cfg, None, summary, state_dir=self.state)
        self.assertEqual([a for a in alerts if a.rule_id == "source_failure"], [])


if __name__ == "__main__":
    unittest.main()
