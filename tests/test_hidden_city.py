import os
import sys
import json
import tempfile
import unittest
from datetime import date
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import load_config, HiddenCityConfig  # noqa: E402
from src.models import FlightQuote, iso_now  # noqa: E402
from src.fetchers.serpapi import parse_layover_flights  # noqa: E402
from src.fetchers.fast_flights import DEFAULT_FX_RATES  # noqa: E402
from src import hidden_city as hc  # noqa: E402
from src.notifiers.feishu import build_hidden_city_card, HIDDEN_CITY_RISK  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---- SerpAPI 官方文档样例结构（best_flights / other_flights 里每航班挂 layovers[]，
#      元素形如 {"duration":135,"name":"Shanghai Pudong International Airport","id":"PVG"}）。
SAMPLE_SERP = {
    "best_flights": [
        {
            "flights": [
                {"airline": "Air Canada", "flight_number": "AC 88"},
                {"airline": "Air China", "flight_number": "CA 880"},
            ],
            "layovers": [
                {"duration": 135, "name": "Beijing Capital International Airport", "id": "PEK"},
            ],
            "price": 4200,
        },
    ],
    "other_flights": [
        {
            "flights": [
                {"airline": "Asiana", "flight_number": "OZ 1"},
                {"airline": "Asiana", "flight_number": "OZ 2"},
            ],
            "layovers": [
                {"duration": 425, "name": "Incheon International Airport", "id": "ICN"},
            ],
            "price": 3900,
        },
        {
            "flights": [
                {"airline": "China Eastern", "flight_number": "MU 500"},
                {"airline": "China Eastern", "flight_number": "MU 501"},
            ],
            "layovers": [
                {"duration": 90, "name": "Shanghai Pudong International Airport", "id": "PVG"},
            ],
            "price": 4050,
        },
    ],
}


def _quote(dest, dd, price, airline, stops, flight_no="XX1"):
    return FlightQuote(
        fetched_at=iso_now(), route_id="hc", origin="YUL", dest=dest,
        depart_date=dd, airline=airline, flight_no=flight_no, stops=stops,
        price=price, currency="CNY", raw_price=float(price), raw_currency="CNY",
        source="fast_flights",
    )


class FakeFast:
    """返回可控报价的假 fast-flights fetcher。"""
    name = "fast_flights"

    def __init__(self, per_route):
        self.per_route = per_route  # dest -> list[FlightQuote]
        self.calls = []

    def available(self):
        return True

    def fetch(self, route, depart_date):
        self.calls.append((route.dest, depart_date))
        return [
            _quote(route.dest, depart_date, q.price, q.airline, q.stops, q.flight_no)
            for q in self.per_route.get(route.dest, [])
        ]


class FakeSerp:
    """假 SerpAPI fetcher，统计 fetch_layovers 调用次数并遵守额度语义。"""
    name = "serpapi"

    def __init__(self, remaining=90):
        self._remaining = remaining
        self.calls = 0

    def available(self):
        return True

    def remaining_quota(self):
        return self._remaining

    def fetch_layovers(self, origin, dest, depart_date):
        self.calls += 1
        return parse_layover_flights(
            SAMPLE_SERP["best_flights"] + SAMPLE_SERP["other_flights"], DEFAULT_FX_RATES
        )


class TestConfigHiddenCity(unittest.TestCase):
    def test_repo_config_parses_hidden_city(self):
        cfg = load_config(os.path.join(ROOT, "config.json"))
        self.assertIsNotNone(cfg.hidden_city)
        self.assertTrue(cfg.hidden_city.enabled)
        self.assertEqual(cfg.hidden_city.origin, "YUL")
        self.assertIn("PEK", cfg.hidden_city.chinese_hubs)
        self.assertIn("BKK", cfg.hidden_city.onward_routes)

    def test_from_dict_defaults(self):
        c = HiddenCityConfig.from_dict(None)
        self.assertFalse(c.enabled)
        self.assertEqual(c.max_dates_per_onward, 15)
        # 新增：默认只确认疑似中国承运人候选，且带默认中国承运人名单。
        self.assertTrue(c.confirm_only_suspected)
        self.assertIn("Air China", c.cn_carriers)

    def test_repo_config_parses_cn_carrier_fields(self):
        cfg = load_config(os.path.join(ROOT, "config.json"))
        self.assertTrue(cfg.hidden_city.confirm_only_suspected)
        self.assertIn("China Southern", cfg.hidden_city.cn_carriers)

    def test_from_dict_confirm_flag_override(self):
        c = HiddenCityConfig.from_dict({"confirm_only_suspected": False,
                                        "cn_carriers": ["Air China"]})
        self.assertFalse(c.confirm_only_suspected)
        self.assertEqual(c.cn_carriers, ["Air China"])

    def test_from_dict_uppercases_codes(self):
        c = HiddenCityConfig.from_dict({"origin": "yul", "onward_routes": ["bkk"],
                                        "chinese_hubs": ["pek"]})
        self.assertEqual(c.origin, "YUL")
        self.assertEqual(c.onward_routes, ["BKK"])
        self.assertEqual(c.chinese_hubs, ["PEK"])


class TestLayoverParsing(unittest.TestCase):
    def test_parse_extracts_layover_ids(self):
        rows = parse_layover_flights(
            SAMPLE_SERP["best_flights"] + SAMPLE_SERP["other_flights"], DEFAULT_FX_RATES)
        self.assertEqual(len(rows), 3)
        ids = [lo["id"] for r in rows for lo in r["layovers"]]
        self.assertEqual(ids, ["PEK", "ICN", "PVG"])
        self.assertEqual(rows[0]["airline"], "Air Canada")
        self.assertEqual(rows[0]["price_cny"], 4200)

    def test_match_confirmed_hub_picks_chinese_hub(self):
        rows = parse_layover_flights(
            SAMPLE_SERP["best_flights"] + SAMPLE_SERP["other_flights"], DEFAULT_FX_RATES)
        # hubs include PEK & PVG but not ICN -> cheapest china-hub flight = PVG @4050
        match = hc._match_confirmed_hub(rows, ["PEK", "PVG", "CAN"])
        self.assertIsNotNone(match)
        self.assertEqual(match["layover_cn"], "PVG")
        self.assertEqual(match["price_cny"], 4050)

    def test_match_returns_none_when_no_chinese_hub(self):
        rows = parse_layover_flights(SAMPLE_SERP["other_flights"][:1], DEFAULT_FX_RATES)  # ICN only
        self.assertIsNone(hc._match_confirmed_hub(rows, ["PEK", "PVG"]))


class TestHeuristic(unittest.TestCase):
    def test_air_china_maps_to_pek(self):
        self.assertEqual(hc.heuristic_hub("Air China", ["PEK", "PVG"]), "PEK")

    def test_china_eastern_maps_to_pvg(self):
        self.assertEqual(hc.heuristic_hub("China Eastern, Air Canada", ["PVG"]), "PVG")

    def test_hub_must_be_in_config(self):
        # China Southern -> CAN, but CAN not configured -> no suspected hit.
        self.assertIsNone(hc.heuristic_hub("China Southern", ["PEK", "PVG"]))

    def test_unknown_airline_none(self):
        self.assertIsNone(hc.heuristic_hub("United", ["PEK"]))


class TestCnCarrier(unittest.TestCase):
    CN = HiddenCityConfig.from_dict(None).cn_carriers  # 默认中国承运人名单

    def test_multi_carrier_substring_match(self):
        # fast-flights 多航司串，只要包含任一中国承运人即视为疑似中国中转。
        self.assertTrue(hc.is_cn_carrier("WestJet, China Southern", self.CN))

    def test_case_insensitive(self):
        self.assertTrue(hc.is_cn_carrier("air china", self.CN))
        self.assertTrue(hc.is_cn_carrier("XIAMEN AIRLINES", self.CN))

    def test_cathay_hkg_not_counted(self):
        # 国泰经 HKG 中转，HKG 不在 chinese_hubs，默认名单不含 Cathay -> 不算疑似。
        self.assertFalse(hc.is_cn_carrier("Cathay Pacific", self.CN))

    def test_non_cn_carrier(self):
        self.assertFalse(hc.is_cn_carrier("Asiana", self.CN))
        self.assertFalse(hc.is_cn_carrier("", self.CN))

    def test_custom_list_override(self):
        self.assertTrue(hc.is_cn_carrier("Cathay Pacific", ["Cathay"]))


class TestRunHiddenCity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.data = os.path.join(self.tmp, "data")
        self.docs = os.path.join(self.tmp, "docs")
        os.makedirs(self.data)
        os.makedirs(self.docs)
        self.today = date(2026, 7, 13)
        self.dates = {"mode": "fixed", "fixed_dates": ["2030-01-01", "2030-01-02"]}

    def _cfg(self, **over):
        base = dict(enabled=True, origin="YUL", onward_routes=["BKK", "SGN"],
                    chinese_hubs=["PEK", "PVG"], dates=self.dates,
                    max_serpapi_per_run=10, min_saving_pct=0,
                    max_dates_per_onward=15, max_direct_lookups=0)
        base.update(over)
        return SimpleNamespace(hidden_city=HiddenCityConfig(**base))

    def test_confirmed_hits_via_serpapi(self):
        fast = FakeFast({
            "BKK": [_quote("BKK", "", 5000, "Air Canada, Air China", 1)],
            "SGN": [_quote("SGN", "", 5200, "Asiana", 1)],
        })
        serp = FakeSerp(remaining=90)
        cfg = self._cfg()
        res = hc.run_hidden_city(cfg, self.data, self.docs, today=self.today,
                                 fast_fetcher=fast, serp_fetcher=serp,
                                 sleep_fn=lambda *_: None, request_interval=0)
        self.assertTrue(res["hits"])
        self.assertTrue(all(not h["suspected"] for h in res["hits"]))
        # confirmed hub is a chinese hub
        for h in res["hits"]:
            self.assertIn(h["layover_cn"], ["PEK", "PVG"])
        # dashboard json written
        self.assertTrue(os.path.exists(os.path.join(self.docs, "data", "hidden_city.json")))

    def test_candidates_cn_carrier_sorted_first(self):
        # BKK 更便宜但非中国承运人；SGN 更贵但中国承运人 -> 排序后 CN 候选在最前。
        fast = FakeFast({
            "BKK": [_quote("BKK", "", 3000, "Asiana", 1)],
            "SGN": [_quote("SGN", "", 5000, "WestJet, China Southern", 1)],
        })
        cfg = self._cfg()
        cands = hc._gather_candidates(cfg.hidden_city, self.today, fast,
                                      lambda *_: None, 0)
        self.assertTrue(cands)
        self.assertTrue(cands[0]["is_cn_carrier"])
        self.assertEqual(cands[0]["onward_dest"], "SGN")
        # 非 CN 候选排在所有 CN 候选之后
        first_non_cn = next(i for i, c in enumerate(cands) if not c["is_cn_carrier"])
        self.assertTrue(all(cands[i]["is_cn_carrier"] for i in range(first_non_cn)))

    def test_confirm_prioritizes_cn_carrier_over_cheaper(self):
        # 预算只够 1 次确认：应花在中国承运人候选(SGN)上，而非更便宜的非 CN(BKK)。
        fast = FakeFast({
            "BKK": [_quote("BKK", "", 3000, "Asiana", 1)],          # 更便宜、非 CN
            "SGN": [_quote("SGN", "", 5000, "China Southern", 1)],  # 更贵、CN
        })
        serp = FakeSerp(remaining=90)
        cfg = self._cfg(max_serpapi_per_run=1)
        res = hc.run_hidden_city(cfg, self.data, self.docs, today=self.today,
                                 fast_fetcher=fast, serp_fetcher=serp,
                                 sleep_fn=lambda *_: None, request_interval=0)
        self.assertEqual(serp.calls, 1)
        self.assertTrue(res["hits"])
        self.assertTrue(all(h["onward_dest"] == "SGN" for h in res["hits"]))
        self.assertTrue(all(not h["suspected"] for h in res["hits"]))

    def test_confirm_only_suspected_no_cn_zero_serpapi(self):
        # confirm_only_suspected=True 且无中国承运人候选 -> 一次 SerpAPI 都不花。
        fast = FakeFast({
            "BKK": [_quote("BKK", "", 3000, "Asiana", 1)],
            "SGN": [_quote("SGN", "", 5000, "Air Canada", 1)],
        })
        serp = FakeSerp(remaining=90)
        cfg = self._cfg(max_serpapi_per_run=10, confirm_only_suspected=True)
        res = hc.run_hidden_city(cfg, self.data, self.docs, today=self.today,
                                 fast_fetcher=fast, serp_fetcher=serp,
                                 sleep_fn=lambda *_: None, request_interval=0)
        self.assertEqual(serp.calls, 0)
        self.assertEqual(res["hits"], [])
        self.assertEqual(res["stats"]["cn_carrier_candidates"], 0)

    def test_confirm_only_suspected_false_falls_back_to_cheapest(self):
        # confirm_only_suspected=False：无 CN 候选时仍确认最便宜的其它候选。
        fast = FakeFast({
            "BKK": [_quote("BKK", "", 3000, "Asiana", 1)],
            "SGN": [_quote("SGN", "", 5000, "Air Canada", 1)],
        })
        serp = FakeSerp(remaining=90)
        cfg = self._cfg(max_serpapi_per_run=10, confirm_only_suspected=False)
        res = hc.run_hidden_city(cfg, self.data, self.docs, today=self.today,
                                 fast_fetcher=fast, serp_fetcher=serp,
                                 sleep_fn=lambda *_: None, request_interval=0)
        self.assertGreater(serp.calls, 0)

    def test_heuristic_degrade_when_no_serpapi(self):
        fast = FakeFast({
            "BKK": [_quote("BKK", "", 5000, "Air China", 1)],       # -> PEK suspected
            "SGN": [_quote("SGN", "", 5200, "China Eastern", 1)],   # -> PVG suspected
        })
        cfg = self._cfg()
        res = hc.run_hidden_city(cfg, self.data, self.docs, today=self.today,
                                 fast_fetcher=fast, serp_fetcher=None,
                                 sleep_fn=lambda *_: None, request_interval=0)
        self.assertTrue(res["hits"])
        self.assertTrue(all(h["suspected"] for h in res["hits"]))
        hubs = {h["layover_cn"] for h in res["hits"]}
        self.assertTrue(hubs <= {"PEK", "PVG"})

    def test_serpapi_budget_not_exceeded(self):
        # 4 candidate route×date pairs but max_serpapi_per_run=2 -> at most 2 calls.
        fast = FakeFast({
            "BKK": [_quote("BKK", "", 5000, "Air Canada", 1)],
            "SGN": [_quote("SGN", "", 5200, "Air Canada", 1)],
        })
        serp = FakeSerp(remaining=90)
        cfg = self._cfg(max_serpapi_per_run=2)
        res = hc.run_hidden_city(cfg, self.data, self.docs, today=self.today,
                                 fast_fetcher=fast, serp_fetcher=serp,
                                 sleep_fn=lambda *_: None, request_interval=0)
        self.assertLessEqual(serp.calls, 2)
        self.assertEqual(res["stats"]["serpapi_used"], serp.calls)
        self.assertLessEqual(res["stats"]["serpapi_used"], res["stats"]["serpapi_budget"])

    def test_month_remaining_caps_budget(self):
        fast = FakeFast({"BKK": [_quote("BKK", "", 5000, "Air Canada", 1)]})
        serp = FakeSerp(remaining=1)  # only 1 left this month
        cfg = self._cfg(max_serpapi_per_run=10)
        hc.run_hidden_city(cfg, self.data, self.docs, today=self.today,
                           fast_fetcher=fast, serp_fetcher=serp,
                           sleep_fn=lambda *_: None, request_interval=0)
        self.assertLessEqual(serp.calls, 1)

    def test_disabled_returns_no_hits(self):
        cfg = self._cfg(enabled=False)
        res = hc.run_hidden_city(cfg, self.data, self.docs, today=self.today,
                                 fast_fetcher=FakeFast({}), serp_fetcher=None)
        self.assertEqual(res["hits"], [])

    def test_storage_append_and_dedup(self):
        row = {
            "fetched_at": iso_now(), "origin": "YUL", "onward_dest": "BKK",
            "depart_date": "2030-01-01", "layover_cn": "PEK", "price_cny": 5000,
            "source": "serpapi",
        }
        n1 = hc.append_hidden_hits(self.data, [row])
        n2 = hc.append_hidden_hits(self.data, [dict(row)])  # duplicate
        self.assertEqual(n1, 1)
        self.assertEqual(n2, 0)
        recent = hc.read_recent_hits(self.data)
        self.assertEqual(len(recent), 1)


class TestHiddenCityCard(unittest.TestCase):
    def _hit(self, suspected=False, saving=True):
        return {
            "origin": "YUL", "onward_dest": "BKK", "depart_date": "2030-01-01",
            "layover_cn": "PEK", "layover_city_cn": "北京", "price_cny": 4200,
            "airline": "Air China", "flight_no": "CA880", "depart_time": "20:55",
            "direct_price_cny": 6000 if saving else None,
            "saving_pct": 30.0 if saving else None,
            "suspected": suspected, "source": "serpapi",
        }

    def _text(self, card):
        return json.dumps(card, ensure_ascii=False)

    def test_card_contains_risk_line(self):
        card = build_hidden_city_card([self._hit()])
        self.assertIn(HIDDEN_CITY_RISK, self._text(card))

    def test_card_title(self):
        card = build_hidden_city_card([self._hit()])
        self.assertEqual(card["card"]["header"]["title"]["content"], "🧳 隐藏城市特价")

    def test_confirmed_has_no_suspected_flag(self):
        card = build_hidden_city_card([self._hit(suspected=False)])
        self.assertNotIn("未确认中转站", self._text(card))

    def test_suspected_marked(self):
        card = build_hidden_city_card([self._hit(suspected=True)])
        self.assertIn("未确认中转站", self._text(card))

    def test_saving_line_present(self):
        card = build_hidden_city_card([self._hit(saving=True)])
        self.assertIn("省 30%", self._text(card))

    def test_limit_folds_rest(self):
        hits = [self._hit() for _ in range(12)]
        card = build_hidden_city_card(hits, limit=8)
        self.assertIn("还有 4 条", self._text(card))

    def test_empty_hits_card(self):
        card = build_hidden_city_card([])
        self.assertIn("未发现", self._text(card))


if __name__ == "__main__":
    unittest.main()
