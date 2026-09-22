import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.fetchers.skyscanner import _parse_itineraries, SkyScannerFetcher  # noqa: E402
from src.fetchers.base import FetchError  # noqa: E402


class _Route:
    id = "yul-nrt"
    origin = "YUL"
    dest = "NRT"


SAMPLE = [{
    "id": "a", "price": {"raw": 4592.0, "formatted": "CN¥4,592"},
    "legs": [{
        "stopCount": 2, "departure": "2026-10-03T09:45:00",
        "carriers": {"marketing": [{"name": "China Eastern"}]},
        "segments": [{"flightNumber": "8888",
                      "marketingCarrier": {"alternateId": "MU"}}],
    }],
}, {
    "id": "b", "price": {"raw": 6934.0},
    "legs": [{"stopCount": 1, "departure": "2026-10-03T17:34:00",
              "carriers": {"marketing": [{"name": "American"}]}, "segments": []}],
}]


class TestParse(unittest.TestCase):
    def test_parse_cny_direct_no_fx(self):
        quotes = _parse_itineraries(SAMPLE, _Route, "2026-10-03", "CNY")
        self.assertEqual(len(quotes), 2)
        q = quotes[0]
        # 价直接是 CNY,不做 FX 换算
        self.assertEqual(q.price, 4592)
        self.assertEqual(q.currency, "CNY")
        self.assertEqual(q.raw_currency, "CNY")
        self.assertEqual(q.airline, "China Eastern")
        self.assertEqual(q.flight_no, "MU8888")
        self.assertEqual(q.depart_time, "09:45")
        self.assertEqual(q.stops, 2)
        self.assertEqual(q.source, "skyscanner")

    def test_parse_skips_priceless_and_bad(self):
        bad = [{"id": "x", "legs": []}, "nope", {"price": {"raw": None}}]
        self.assertEqual(_parse_itineraries(bad, _Route, "2026-10-03", "CNY"), [])


class TestGuards(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.f = SkyScannerFetcher(state_dir=self.tmp)
        os.environ["RAPIDAPI_KEY"] = "test-key"
        os.environ["SKYSCANNER_EVERY_N_DAYS"] = "1"  # 每天,排除隔天守卫干扰
        os.environ["SKYSCANNER_MONTHLY_CAP"] = "5"

    def tearDown(self):
        for k in ("RAPIDAPI_KEY", "SKYSCANNER_EVERY_N_DAYS", "SKYSCANNER_MONTHLY_CAP"):
            os.environ.pop(k, None)

    def test_quota_guard_blocks_over_cap(self):
        self.f._increment_usage(5)  # 打满 cap=5
        self.assertEqual(self.f.remaining_quota(), 0)
        with self.assertRaises(FetchError) as ctx:
            self.f.fetch(_Route, "2026-10-03")
        self.assertFalse(ctx.exception.retryable)  # 降级,不重试

    def test_offday_guard_degrades(self):
        os.environ["SKYSCANNER_EVERY_N_DAYS"] = "1000000"  # 几乎必然非当值日
        with self.assertRaises(FetchError) as ctx:
            self.f.fetch(_Route, "2026-10-03")
        self.assertFalse(ctx.exception.retryable)

    def test_missing_key_degrades(self):
        os.environ.pop("RAPIDAPI_KEY", None)
        self.assertFalse(self.f.available())
        with self.assertRaises(FetchError):
            self.f.fetch(_Route, "2026-10-03")


if __name__ == "__main__":
    unittest.main()
