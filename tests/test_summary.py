import os
import sys
import json
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.models import FlightQuote  # noqa: E402
from src.storage import Storage  # noqa: E402
from src.main import mark_lowest_of_day  # noqa: E402


def q(price, flight_no, fetched_at, depart_date="2026-10-01",
      airline="Air China", depart_time="20:55"):
    return FlightQuote(
        fetched_at=fetched_at, route_id="sha-nrt", origin="SHA", dest="NRT",
        depart_date=depart_date, airline=airline, flight_no=flight_no,
        depart_time=depart_time, stops=0,
        price=price, source="fast_flights", raw_price=float(price),
    )


class TestSummary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.st = Storage(os.path.join(self.tmp, "data"), os.path.join(self.tmp, "docs"))

    def test_mark_lowest_of_day(self):
        quotes = [q(1200, "MU2", "2026-07-09T08:00:00+08:00"),
                  q(900, "MU1", "2026-07-09T08:00:00+08:00")]
        mark_lowest_of_day(quotes)
        lows = [x for x in quotes if x.is_lowest_of_day]
        self.assertEqual(len(lows), 1)
        self.assertEqual(lows[0].price, 900)

    def test_mark_lowest_of_day_tie_prefers_detail(self):
        # Two quotes tie at the day's lowest price; the one WITHOUT flight detail
        # (empty flight_no + depart_time) must not win the tie.
        bare = q(900, "", "2026-07-09T08:00:00+08:00", airline="", depart_time="")
        rich = q(900, "MU1", "2026-07-09T08:00:00+08:00",
                 airline="Air China", depart_time="08:30")
        mark_lowest_of_day([bare, rich])
        self.assertFalse(bare.is_lowest_of_day)
        self.assertTrue(rich.is_lowest_of_day)

    def test_build_summary_filters_to_given_route_ids(self):
        # A deleted route keeps its data/ folder but must be excluded from summary
        # when build_summary is called with the config's route id list.
        self.st.append_quotes([
            q(1000, "MU1", "2026-07-09T08:00:00+08:00", depart_date="2026-10-01"),
        ])
        old = FlightQuote(
            fetched_at="2026-07-09T08:00:00+08:00", route_id="pek-cdg", origin="PEK",
            dest="CDG", depart_date="2026-10-01", airline="AF", flight_no="AF1",
            depart_time="10:00", stops=0, price=2000, source="fast_flights",
        )
        self.st.append_quotes([old])
        summary = self.st.build_summary(route_ids=["sha-nrt"])
        self.assertIn("sha-nrt", summary["routes"])
        self.assertNotIn("pek-cdg", summary["routes"])

    def test_build_summary_structure(self):
        self.st.append_quotes([
            q(1000, "MU1", "2026-07-09T08:00:00+08:00"),
            q(1200, "MU2", "2026-07-09T08:00:00+08:00"),
            q(850, "MU1", "2026-07-10T08:00:00+08:00"),
        ])
        summary = self.st.build_summary(extra={"serpapi_remaining_quota": 87})

        self.assertIn("generated_at", summary)
        self.assertEqual(summary["meta"]["serpapi_remaining_quota"], 87)
        node = summary["routes"]["sha-nrt"]["depart_dates"]["2026-10-01"]
        self.assertEqual(node["latest"]["price"], 850)
        self.assertEqual(node["latest"]["fetch_date"], "2026-07-10")
        self.assertEqual(node["historical_low"]["price"], 850)
        self.assertEqual([s["price"] for s in node["series"]], [1000, 850])
        # enriched flight identity carried on latest / historical_low
        self.assertEqual(node["latest"]["airline"], "Air China")
        self.assertEqual(node["latest"]["flight_no"], "MU1")
        self.assertEqual(node["latest"]["depart_time"], "20:55")
        self.assertIn("airline", node["historical_low"])

        # summary.json is actually written under docs/data/
        path = os.path.join(self.tmp, "docs", "data", "summary.json")
        self.assertTrue(os.path.exists(path))
        with open(path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["meta"]["serpapi_remaining_quota"], 87)

    def test_summary_excludes_past_depart_dates(self):
        # Rolling window advances daily; stale past departure dates linger in the
        # JSONL but must NOT appear in the live summary (or be picked as cheapest).
        past = q(500, "CA9", "2020-01-01T08:00:00+08:00", depart_date="2020-01-01")
        future = q(3000, "CA1", "2020-01-01T08:00:00+08:00", depart_date="2099-12-31")
        self.st.append_quotes([past, future])
        summary = self.st.build_summary()
        dd = summary["routes"]["sha-nrt"]["depart_dates"]
        self.assertNotIn("2020-01-01", dd)   # past date filtered out
        self.assertIn("2099-12-31", dd)      # future date kept
        # cheapest visible fare must be the future one, not the cheaper past one
        prices = [v["latest"]["price"] for v in dd.values() if v.get("latest")]
        self.assertEqual(min(prices), 3000)

    def test_summary_tolerates_legacy_rows_without_depart_time(self):
        # Old JSONL rows may omit depart_time; from_dict + summary must cope.
        legacy = FlightQuote.from_dict({
            "fetched_at": "2026-07-09T08:00:00+08:00", "route_id": "sha-nrt",
            "origin": "SHA", "dest": "NRT", "depart_date": "2026-10-01",
            "airline": "EVA Air", "flight_no": "", "stops": 1, "price": 999,
            "source": "fast_flights",
        })
        self.assertEqual(legacy.depart_time, "")
        self.st.append_quotes([legacy])
        summary = self.st.build_summary()
        node = summary["routes"]["sha-nrt"]["depart_dates"]["2026-10-01"]
        self.assertEqual(node["latest"]["depart_time"], "")
        self.assertEqual(node["latest"]["airline"], "EVA Air")


if __name__ == "__main__":
    unittest.main()
