import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.models import FlightQuote  # noqa: E402
from src.storage import Storage  # noqa: E402


def q(price, flight_no, fetched_at="2026-07-09T08:25:31+08:00",
      depart_date="2026-10-01", route_id="sha-nrt", source="fast_flights"):
    return FlightQuote(
        fetched_at=fetched_at, route_id=route_id, origin="SHA", dest="NRT",
        depart_date=depart_date, airline="MU", flight_no=flight_no, stops=0,
        price=price, currency="CNY", raw_price=float(price), raw_currency="CNY",
        price_type="total_with_tax", source=source,
    )


class TestStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.data = os.path.join(self.tmp, "data")
        self.docs = os.path.join(self.tmp, "docs")
        self.st = Storage(self.data, self.docs)

    def test_month_sharding(self):
        self.st.append_quotes([q(1000, "MU100")])
        self.st.append_quotes([q(1200, "MU200", fetched_at="2026-08-01T09:00:00+08:00")])
        # July fetch and August fetch land in different monthly shards.
        self.assertTrue(os.path.exists(os.path.join(self.data, "sha-nrt", "2026-07.jsonl")))
        self.assertTrue(os.path.exists(os.path.join(self.data, "sha-nrt", "2026-08.jsonl")))

    def test_dedup_on_primary_key(self):
        # Same (route, depart, flight_no, fetch_date, source) => duplicate.
        w1 = self.st.append_quotes([q(1000, "MU100")])
        w2 = self.st.append_quotes([q(999, "MU100")])  # dup key, different price
        self.assertEqual(w1, 1)
        self.assertEqual(w2, 0)
        self.assertEqual(len(self.st.read_route("sha-nrt")), 1)

    def test_dedup_within_batch(self):
        written = self.st.append_quotes([q(1000, "MU100"), q(1001, "MU100")])
        self.assertEqual(written, 1)

    def test_different_source_not_dup(self):
        self.st.append_quotes([q(1000, "MU100", source="fast_flights")])
        w = self.st.append_quotes([q(1000, "MU100", source="serpapi")])
        self.assertEqual(w, 1)

    def test_queries(self):
        # two fetch dates, cheapest tracked per day
        self.st.append_quotes([
            q(1000, "MU100", fetched_at="2026-07-09T08:00:00+08:00"),
            q(1200, "MU200", fetched_at="2026-07-09T08:00:00+08:00"),
            q(800, "MU100", fetched_at="2026-07-10T08:00:00+08:00"),
        ])
        latest = self.st.latest_low("sha-nrt", "2026-10-01")
        self.assertEqual(latest["price"], 800)
        self.assertEqual(latest["fetch_date"], "2026-07-10")
        hlow = self.st.historical_low("sha-nrt", "2026-10-01")
        self.assertEqual(hlow["price"], 800)
        series = self.st.series("sha-nrt", "2026-10-01")
        self.assertEqual([s["price"] for s in series], [1000, 800])


if __name__ == "__main__":
    unittest.main()
