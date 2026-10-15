import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.fetchers.skyscanner import _parse_items, SkyScannerFetcher  # noqa: E402
from src.fetchers.base import FetchError  # noqa: E402


class _Route:
    id = "yul-nrt"
    origin = "YUL"
    dest = "NRT"


# omkarcloud/Apify dataset item 形态(run-sync-get-dataset-items 返回的数组元素)。
SAMPLE = [{
    "price": {"amount": 4880, "currency": "CNY", "formatted": "CN¥4,880"},
    "stop_count": 2,
    "legs": [{
        "departure_at": "2026-12-11T17:55:00",
        "carriers": [{"name": "Xiamen Air", "code": "MF"}],
    }],
}, {
    "price": {"amount": 5987.0, "currency": "CNY"},
    "stop_count": 1,
    "legs": [{"departure_at": "2026-12-11T07:10:00",
              "carriers": [{"name": "Delta", "code": "DL"}]}],
}]


class TestParse(unittest.TestCase):
    def test_parse_cny_direct_no_fx(self):
        quotes = _parse_items(SAMPLE, _Route, "2026-12-11", "CNY")
        self.assertEqual(len(quotes), 2)
        q = quotes[0]
        self.assertEqual(q.price, 4880)
        self.assertEqual(q.currency, "CNY")
        self.assertEqual(q.raw_currency, "CNY")
        self.assertEqual(q.airline, "Xiamen Air")
        self.assertEqual(q.flight_no, "MF")
        self.assertEqual(q.depart_time, "17:55")
        self.assertEqual(q.stops, 2)
        self.assertEqual(q.source, "skyscanner")

    def test_parse_skips_priceless_and_bad(self):
        bad = [{"legs": []}, "nope", {"price": {"amount": None}}, {"price": {}}]
        self.assertEqual(_parse_items(bad, _Route, "2026-12-11", "CNY"), [])


class TestGuards(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.f = SkyScannerFetcher(state_dir=self.tmp)
        os.environ["APIFY_TOKEN"] = "test-token"
        os.environ["SKYSCANNER_EVERY_N_DAYS"] = "1"
        os.environ["SKYSCANNER_MONTHLY_CAP"] = "100"

    def tearDown(self):
        for k in ("APIFY_TOKEN", "SKYSCANNER_EVERY_N_DAYS", "SKYSCANNER_MONTHLY_CAP"):
            os.environ.pop(k, None)

    def test_quota_guard_blocks_over_cap(self):
        self.f._increment_usage(100)  # 打满 cap=100 条
        self.assertEqual(self.f.remaining_quota(), 0)
        with self.assertRaises(FetchError) as ctx:
            self.f.fetch(_Route, "2026-12-11")
        self.assertFalse(ctx.exception.retryable)  # 降级,不重试

    def test_usage_counts_by_itineraries(self):
        self.f._increment_usage(30)
        self.f._increment_usage(30)
        self.assertEqual(self.f._used_this_month(), 60)

    def test_offday_guard_degrades(self):
        os.environ["SKYSCANNER_EVERY_N_DAYS"] = "1000000"
        with self.assertRaises(FetchError) as ctx:
            self.f.fetch(_Route, "2026-12-11")
        self.assertFalse(ctx.exception.retryable)

    def test_missing_token_degrades(self):
        os.environ.pop("APIFY_TOKEN", None)
        self.assertFalse(self.f.available())
        with self.assertRaises(FetchError):
            self.f.fetch(_Route, "2026-12-11")


if __name__ == "__main__":
    unittest.main()
