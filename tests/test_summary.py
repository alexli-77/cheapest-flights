import os
import sys
import json
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.models import FlightQuote  # noqa: E402
from src.storage import Storage  # noqa: E402
from src.main import mark_lowest_of_day  # noqa: E402


def q(price, flight_no, fetched_at, depart_date="2026-10-01"):
    return FlightQuote(
        fetched_at=fetched_at, route_id="sha-nrt", origin="SHA", dest="NRT",
        depart_date=depart_date, airline="MU", flight_no=flight_no, stops=0,
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

        # summary.json is actually written under docs/data/
        path = os.path.join(self.tmp, "docs", "data", "summary.json")
        self.assertTrue(os.path.exists(path))
        with open(path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["meta"]["serpapi_remaining_quota"], 87)


if __name__ == "__main__":
    unittest.main()
