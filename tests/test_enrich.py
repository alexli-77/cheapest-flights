import os
import sys
import json
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import Route  # noqa: E402
from src.fetchers.serpapi import (  # noqa: E402
    parse_flight_details, _baggage_note, _hhmm_from_serp_time,
    SerpApiFetcher, MONTHLY_CAP,
)
from src.fetchers.fast_flights import DEFAULT_FX_RATES  # noqa: E402
from src.enrich import enrich_summary  # noqa: E402
from src.notifiers.feishu import (  # noqa: E402
    build_digest_card, build_hidden_city_card, HIDDEN_CITY_BAGGAGE,
)

# ---- SerpAPI google_flights 文档样例：每 leg 挂 departure_airport{time},
#      flight_number, airplane；行程级 extensions 是粗略行李标记；layovers[].id
#      是中转机场三字码。
SAMPLE_DETAIL = {
    "best_flights": [
        {
            "flights": [
                {
                    "departure_airport": {"name": "Montréal-Trudeau", "id": "YUL",
                                          "time": "2026-08-06 20:55"},
                    "arrival_airport": {"name": "Beijing Capital", "id": "PEK",
                                        "time": "2026-08-07 23:40"},
                    "airline": "Air China", "flight_number": "CA 880",
                    "airplane": "Boeing 777",
                },
            ],
            "layovers": [],
            "extensions": ["1 free carry-on", "Checked baggage for a fee"],
            "price": 5000,
            "overnight": True,
        },
    ],
    "other_flights": [
        {
            "flights": [
                {
                    "departure_airport": {"name": "Montréal-Trudeau", "id": "YUL",
                                          "time": "2026-08-06 09:10"},
                    "arrival_airport": {"name": "Vancouver", "id": "YVR",
                                        "time": "2026-08-06 11:30"},
                    "airline": "Air Canada", "flight_number": "AC 301",
                    "airplane": "Airbus A320",
                },
                {
                    "departure_airport": {"name": "Vancouver", "id": "YVR",
                                          "time": "2026-08-06 13:05"},
                    "arrival_airport": {"name": "Beijing Capital", "id": "PEK",
                                        "time": "2026-08-07 15:10"},
                    "airline": "Air Canada", "flight_number": "AC 31",
                    "airplane": "Boeing 787",
                },
            ],
            "layovers": [{"duration": 95, "name": "Vancouver International", "id": "YVR"}],
            "extensions": ["1 free checked bag"],
            "price": 5200,
        },
    ],
}


class TestParseFlightDetails(unittest.TestCase):
    def test_time_normalization(self):
        self.assertEqual(_hhmm_from_serp_time("2026-08-06 20:55"), "20:55")
        self.assertEqual(_hhmm_from_serp_time("2026-08-06 09:10"), "09:10")
        self.assertEqual(_hhmm_from_serp_time(""), "")
        self.assertEqual(_hhmm_from_serp_time(None), "")

    def test_baggage_note_extracts_and_compresses(self):
        note = _baggage_note(["1 free carry-on", "Checked baggage for a fee"])
        self.assertIn("含1件随身", note)
        self.assertIn("托运需另购", note)
        # free checked bag -> 含托运
        self.assertIn("含1件托运", _baggage_note(["1 free checked bag"]))
        # nothing baggage-related -> ""
        self.assertEqual(_baggage_note(["Overnight flight", "Wi-Fi"]), "")

    def test_parse_direct_and_transfer(self):
        rows = parse_flight_details(SAMPLE_DETAIL, DEFAULT_FX_RATES)
        self.assertEqual(len(rows), 2)
        direct = rows[0]
        self.assertEqual(direct["flight_no"], "CA 880")
        self.assertEqual(direct["airline"], "Air China")
        self.assertEqual(direct["airplane"], "Boeing 777")
        self.assertEqual(direct["depart_time"], "20:55")
        self.assertEqual(direct["arrive_time"], "23:40")
        self.assertEqual(direct["stops"], 0)
        self.assertEqual(direct["layover_airports"], [])
        self.assertEqual(direct["price_cny"], 5000)
        self.assertTrue(direct["overnight"])
        self.assertIn("含1件随身", direct["baggage_note"])

        transfer = rows[1]
        self.assertEqual(transfer["stops"], 1)
        self.assertEqual(transfer["layover_airports"], ["YVR"])
        self.assertEqual(transfer["depart_time"], "09:10")   # first leg
        self.assertEqual(transfer["arrive_time"], "15:10")   # last leg
        self.assertIn("含1件托运", transfer["baggage_note"])

    def test_parse_default_rates_when_none(self):
        rows = parse_flight_details(SAMPLE_DETAIL)  # rates=None -> DEFAULT_FX_RATES
        self.assertEqual(rows[0]["price_cny"], 5000)

    def test_parse_empty_payload(self):
        self.assertEqual(parse_flight_details({}), [])
        self.assertEqual(parse_flight_details(None), [])


# ---------------------------------------------------------------- enrichment
class FakeSerp:
    """假 SerpAPI fetcher：统计 fetch_flight_detail 调用并遵守 available/额度语义。"""

    def __init__(self, remaining=240, available=True, payload=SAMPLE_DETAIL):
        self._remaining = remaining
        self._available = available
        self._payload = payload
        self.calls = 0

    def available(self):
        return self._available

    def remaining_quota(self):
        return self._remaining

    def fetch_flight_detail(self, origin, dest, depart_date):
        self.calls += 1
        return parse_flight_details(self._payload, DEFAULT_FX_RATES)


def _low(price, dd_present=True):
    return {"fetch_date": "2026-08-01", "price": price, "currency": "CNY",
            "airline": "", "flight_no": "", "depart_time": ""}


def _summary_two_routes():
    return {"routes": {
        "yul-pek": {"depart_dates": {
            "2026-08-06": {"latest": _low(5000), "historical_low": _low(5000),
                           "series": [{"price": 5000}]},
            "2026-08-10": {"latest": _low(5600), "historical_low": _low(5600),
                           "series": [{"price": 5600}]},
        }},
        "yul-yvr": {"depart_dates": {
            "2026-08-06": {"latest": _low(1200), "historical_low": _low(1200),
                           "series": [{"price": 1200}]},
        }},
    }}


def _cfg(routes):
    return SimpleNamespace(routes=routes)


def _routes():
    return [
        Route(id="yul-pek", origin="YUL", dest="PEK",
              dates={"mode": "rolling", "depart_in_days": 90}),
        Route(id="yul-yvr", origin="YUL", dest="YVR",
              dates={"mode": "rolling", "depart_in_days": 30}),
    ]


class TestEnrichSummary(unittest.TestCase):
    def test_backfills_headline_nearest_price(self):
        summary = _summary_two_routes()
        serp = FakeSerp(remaining=240)
        stats = enrich_summary(_cfg(_routes()), summary, serp_fetcher=serp,
                               max_per_run=3, sleep_fn=lambda *_: None)
        # both routes get a headline (budget 3 >= 2 routes)
        hp = summary["routes"]["yul-pek"]["headline"]
        self.assertEqual(hp["depart_date"], "2026-08-06")   # route's cheapest date
        # target price 5000 -> nearest candidate = CA880 @5000 (not the 5200 one)
        self.assertEqual(hp["flight_no"], "CA 880")
        self.assertEqual(hp["depart_time"], "20:55")
        self.assertEqual(hp["stops"], 0)
        self.assertIn("含1件随身", hp["baggage_note"])
        self.assertEqual(stats["enriched"], 2)
        self.assertEqual(serp.calls, 2)

    def test_max_per_run_hard_cap(self):
        summary = _summary_two_routes()
        serp = FakeSerp(remaining=240)
        stats = enrich_summary(_cfg(_routes()), summary, serp_fetcher=serp,
                               max_per_run=1, sleep_fn=lambda *_: None)
        self.assertEqual(stats["enriched"], 1)   # only first route
        self.assertEqual(serp.calls, 1)
        self.assertIn("headline", summary["routes"]["yul-pek"])
        self.assertNotIn("headline", summary["routes"]["yul-yvr"])

    def test_month_quota_caps_budget(self):
        summary = _summary_two_routes()
        serp = FakeSerp(remaining=1)   # only 1 left this month
        stats = enrich_summary(_cfg(_routes()), summary, serp_fetcher=serp,
                               max_per_run=3, sleep_fn=lambda *_: None)
        self.assertEqual(stats["budget"], 1)     # min(3, 1)
        self.assertLessEqual(serp.calls, 1)
        self.assertEqual(stats["enriched"], 1)

    def test_no_key_skips(self):
        summary = _summary_two_routes()
        serp = FakeSerp(available=False)
        stats = enrich_summary(_cfg(_routes()), summary, serp_fetcher=serp,
                               max_per_run=3, sleep_fn=lambda *_: None)
        self.assertEqual(stats["enriched"], 0)
        self.assertEqual(serp.calls, 0)
        self.assertNotIn("headline", summary["routes"]["yul-pek"])

    def test_no_fetcher_skips(self):
        summary = _summary_two_routes()
        stats = enrich_summary(_cfg(_routes()), summary, serp_fetcher=None)
        self.assertEqual(stats["enriched"], 0)


class TestQuotaGuard(unittest.TestCase):
    def test_monthly_cap_is_240(self):
        self.assertEqual(MONTHLY_CAP, 240)

    def test_detail_none_when_quota_exhausted(self):
        # With a key present but usage at the 240 cap, fetch_flight_detail must
        # return None WITHOUT any network call (guard short-circuits).
        tmp = tempfile.mkdtemp()
        fetcher = SerpApiFetcher(state_dir=tmp, monthly_cap=MONTHLY_CAP)
        mk = fetcher._month_key()
        with open(fetcher.usage_path, "w", encoding="utf-8") as f:
            json.dump({mk: MONTHLY_CAP}, f)
        os.environ["SERPAPI_KEY"] = "dummy-key-for-guard-test"
        try:
            self.assertEqual(fetcher.remaining_quota(), 0)
            self.assertIsNone(fetcher.fetch_flight_detail("YUL", "PEK", "2026-08-06"))
        finally:
            os.environ.pop("SERPAPI_KEY", None)

    def test_detail_none_when_no_key(self):
        tmp = tempfile.mkdtemp()
        fetcher = SerpApiFetcher(state_dir=tmp)
        os.environ.pop("SERPAPI_KEY", None)
        self.assertIsNone(fetcher.fetch_flight_detail("YUL", "PEK", "2026-08-06"))


# ------------------------------------------------------------- card rendering
class TestDigestCardEnrichment(unittest.TestCase):
    def _headline(self, stops=0, layovers=None, baggage="含1件随身;托运需另购"):
        return {"depart_date": "2026-08-06", "price": 5000, "airline": "Cathay Pacific",
                "flight_no": "CX 889", "airplane": "Airbus A350",
                "depart_time": "20:55", "arrive_time": "23:40", "stops": stops,
                "layover_airports": layovers or [], "baggage_note": baggage,
                "overnight": False, "source": "serpapi"}

    def _summary(self, headline):
        return {"routes": {"yul-pek": {
            "depart_dates": {"2026-08-06": {
                "latest": _low(5000), "historical_low": _low(5000),
                "series": [{"price": 5000}]}},
            "headline": headline,
        }}}

    def _route(self):
        return Route(id="yul-pek", origin="YUL", dest="PEK",
                     dates={"mode": "rolling", "depart_in_days": 90})

    def test_direct_flight_card(self):
        card = build_digest_card([], {"routes_failed": 0},
                                 summary=self._summary(self._headline(stops=0)),
                                 routes=[self._route()], run_date="2026-08-01")
        blob = json.dumps(card, ensure_ascii=False)
        self.assertIn("Cathay Pacific CX 889", blob)
        self.assertIn("直飞", blob)
        self.assertIn("20:55 起飞", blob)
        self.assertIn("🧳 行李", blob)
        self.assertIn("含1件随身", blob)
        # direct flight: NO transfer-baggage policy sentence
        self.assertNotIn("中转无需重新托运", blob)

    def test_transfer_flight_card(self):
        card = build_digest_card([], {"routes_failed": 0},
                                 summary=self._summary(self._headline(stops=1, layovers=["PEK"])),
                                 routes=[self._route()], run_date="2026-08-01")
        blob = json.dumps(card, ensure_ascii=False)
        self.assertIn("中转 北京首都(PEK)", blob)
        self.assertIn("单一订单行李直挂终点，中转无需重新托运", blob)

    def test_no_headline_falls_back(self):
        summary = {"routes": {"yul-pek": {"depart_dates": {"2026-08-06": {
            "latest": _low(5000), "historical_low": _low(5000),
            "series": [{"price": 5000}]}}}}}  # no headline key
        card = build_digest_card([], {"routes_failed": 0}, summary=summary,
                                 routes=[self._route()], run_date="2026-08-01")
        blob = json.dumps(card, ensure_ascii=False)
        self.assertNotIn("🧳 行李", blob)   # no enhanced lines, plain fallback


class TestHiddenCityBaggagePolicy(unittest.TestCase):
    def test_card_states_baggage_goes_to_ticketed_dest(self):
        hit = {"origin": "YUL", "onward_dest": "BKK", "depart_date": "2026-08-06",
               "layover_cn": "PEK", "layover_city_cn": "北京", "price_cny": 4200,
               "airline": "Air China", "flight_no": "CA880", "depart_time": "20:55",
               "direct_price_cny": None, "saving_pct": None,
               "suspected": False, "source": "serpapi"}
        card = build_hidden_city_card([hit])
        blob = json.dumps(card, ensure_ascii=False)
        self.assertIn(HIDDEN_CITY_BAGGAGE, blob)
        self.assertIn("直挂票面终点", blob)
        self.assertIn("只能带手提行李", blob)


if __name__ == "__main__":
    unittest.main()
