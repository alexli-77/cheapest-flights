"""逐段行程展示测试：segments/layovers 解析 + format_itinerary 渲染 +
show_segments 开关回退 + 隐藏城市卡片逐段。"""

import os
import sys
import json
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import Route  # noqa: E402
from src.fetchers.serpapi import parse_segments, parse_flight_details  # noqa: E402
from src.fetchers.fast_flights import DEFAULT_FX_RATES  # noqa: E402
from src.notifiers.feishu import (  # noqa: E402
    format_itinerary, _fmt_dur, build_digest_card, build_hidden_city_card,
)

# ---- SerpAPI google_flights 文档样例：两段行程 YUL->YVR->PEK，中转 YVR 95min。
SAMPLE_MULTI = {
    "best_flights": [
        {
            "flights": [
                {"departure_airport": {"id": "YUL", "time": "2026-08-06 09:10"},
                 "arrival_airport": {"id": "YVR", "time": "2026-08-06 11:30"},
                 "airline": "Air Canada", "flight_number": "AC 301",
                 "airplane": "Airbus A320", "duration": 380},
                {"departure_airport": {"id": "YVR", "time": "2026-08-06 13:05"},
                 "arrival_airport": {"id": "PEK", "time": "2026-08-07 15:10"},
                 "airline": "Air China", "flight_number": "CA 992",
                 "airplane": "Boeing 787", "duration": 665},
            ],
            "layovers": [{"duration": 95, "name": "Vancouver International", "id": "YVR"}],
            "price": 5200,
        },
    ],
}


class TestParseSegments(unittest.TestCase):
    def test_parse_direct_segment(self):
        item = {"flights": [
            {"departure_airport": {"id": "PVG", "time": "2026-08-06 20:55"},
             "arrival_airport": {"id": "PEK", "time": "2026-08-06 23:40"},
             "airline": "Air China", "flight_number": "CA 880",
             "airplane": "Boeing 777", "duration": 165}],
            "layovers": []}
        segs, los = parse_segments(item)
        self.assertEqual(len(segs), 1)
        s = segs[0]
        self.assertEqual(s["leg"], 1)
        self.assertEqual(s["flight_no"], "CA 880")
        self.assertEqual(s["from"], "PVG")
        self.assertEqual(s["from_time"], "2026-08-06 20:55")  # 完整时刻保留
        self.assertEqual(s["to"], "PEK")
        self.assertEqual(s["to_time"], "2026-08-06 23:40")
        self.assertEqual(s["duration_min"], 165)
        self.assertEqual(s["airplane"], "Boeing 777")
        self.assertEqual(los, [])

    def test_parse_multi_segment_and_layover(self):
        item = SAMPLE_MULTI["best_flights"][0]
        segs, los = parse_segments(item)
        self.assertEqual([s["leg"] for s in segs], [1, 2])
        self.assertEqual(segs[0]["to"], "YVR")
        self.assertEqual(segs[1]["from"], "YVR")
        self.assertEqual(segs[1]["to"], "PEK")
        self.assertEqual(los, [{"airport": "YVR", "wait_min": 95}])

    def test_parse_tolerant_missing_fields(self):
        # 缺 departure/arrival/duration 不崩，字段置空/None。
        segs, los = parse_segments({"flights": [{"airline": "X"}], "layovers": [{}]})
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0]["from"], "")
        self.assertEqual(segs[0]["from_time"], "")
        self.assertIsNone(segs[0]["duration_min"])
        self.assertEqual(los, [{"airport": "", "wait_min": None}])

    def test_parse_empty(self):
        self.assertEqual(parse_segments({}), ([], []))
        self.assertEqual(parse_segments(None), ([], []))

    def test_flight_details_carries_segments(self):
        rows = parse_flight_details(SAMPLE_MULTI, DEFAULT_FX_RATES)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(len(row["segments"]), 2)
        self.assertEqual(row["layovers"], [{"airport": "YVR", "wait_min": 95}])
        self.assertEqual(row["layover_airports"], ["YVR"])  # 旧字段仍在


class TestFmtDur(unittest.TestCase):
    def test_friendly_minutes(self):
        self.assertEqual(_fmt_dur(165), "2h45m")
        self.assertEqual(_fmt_dur(200), "3h20m")
        self.assertEqual(_fmt_dur(60), "1h")
        self.assertEqual(_fmt_dur(45), "45m")
        self.assertEqual(_fmt_dur(0), "")
        self.assertEqual(_fmt_dur(None), "")
        self.assertEqual(_fmt_dur("bad"), "")


class TestFormatItinerary(unittest.TestCase):
    def _seg(self, leg, fno, frm, ft, to, tt, dur=None, airplane=""):
        return {"leg": leg, "airline": "", "flight_no": fno, "from": frm,
                "from_time": ft, "to": to, "to_time": tt,
                "duration_min": dur, "airplane": airplane}

    def test_single_segment_same_day(self):
        segs = [self._seg(1, "CA 880", "PVG", "2026-08-06 20:55",
                          "PEK", "2026-08-06 23:40", 165)]
        out = format_itinerary(segs, [])
        self.assertEqual(
            out, "✈️ CA880 · 08-06 20:55 上海浦东(PVG) → 23:40 北京首都(PEK)")
        self.assertNotIn("第1段", out)   # 单段不编号
        self.assertNotIn("+1", out)      # 同日到达无跨日标记

    def test_multi_segment_with_layover_and_crossday(self):
        segs = [
            self._seg(1, "CA 880", "PVG", "2026-08-06 20:55", "PEK", "2026-08-06 23:40", 165),
            self._seg(2, "CA 123", "PEK", "2026-08-07 03:00", "BKK", "2026-08-07 07:10", 250),
        ]
        los = [{"airport": "PEK", "wait_min": 200}]
        out = format_itinerary(segs, los)
        lines = out.split("\n")
        self.assertEqual(len(lines), 3)
        self.assertIn("✈️ 第1段 CA880 · 08-06 20:55 上海浦东(PVG) → 23:40 北京首都(PEK) · 飞行2h45m", lines[0])
        self.assertEqual(lines[1], "⏱ 中转 北京首都(PEK) 等待 3h20m")
        self.assertIn("✈️ 第2段 CA123 · 08-07 03:00 北京首都(PEK) → 07:10 曼谷素万那普(BKK) · 飞行4h10m", lines[2])

    def test_crossday_arrival_marker(self):
        segs = [self._seg(1, "AC 31", "YVR", "2026-08-06 22:05", "PEK", "2026-08-08 05:40", 900)]
        out = format_itinerary(segs, [])
        self.assertIn("05:40+2", out)   # 隔两天到达

    def test_truncation_over_four_segments(self):
        segs = [self._seg(i, f"XX {i}", "AAA", f"2026-08-0{i} 0{i}:00",
                          "BBB", f"2026-08-0{i} 0{i}:30", 30) for i in range(1, 6)]
        out = format_itinerary(segs, [])
        self.assertIn("第4段", out)
        self.assertNotIn("第5段", out)
        self.assertIn("… 还有 1 段", out)

    def test_missing_fields_tolerant(self):
        segs = [self._seg(1, "", "", "", "", "", None)]
        self.assertEqual(format_itinerary(segs, []), "✈️")  # 只剩机头图标不崩
        self.assertEqual(format_itinerary([], []), "")
        self.assertEqual(format_itinerary(None, None), "")

    def test_airport_falls_back_to_code(self):
        segs = [self._seg(1, "ZZ 1", "ZZZ", "2026-08-06 10:00", "QQQ", "2026-08-06 12:00")]
        out = format_itinerary(segs, [])
        self.assertIn("ZZZ", out)
        self.assertIn("QQQ", out)


def _low(price):
    return {"fetch_date": "2026-08-01", "price": price, "currency": "CNY",
            "airline": "", "flight_no": "", "depart_time": ""}


def _multi_headline():
    return {
        "depart_date": "2026-08-06", "price": 5200, "airline": "Air China",
        "flight_no": "CA 992", "airplane": "", "depart_time": "09:10",
        "arrive_time": "15:10", "stops": 1, "layover_airports": ["YVR"],
        "segments": [
            {"leg": 1, "airline": "Air Canada", "flight_no": "AC 301", "from": "YUL",
             "from_time": "2026-08-06 09:10", "to": "YVR", "to_time": "2026-08-06 11:30",
             "duration_min": 380, "airplane": "Airbus A320"},
            {"leg": 2, "airline": "Air China", "flight_no": "CA 992", "from": "YVR",
             "from_time": "2026-08-06 13:05", "to": "PEK", "to_time": "2026-08-07 15:10",
             "duration_min": 665, "airplane": "Boeing 787"},
        ],
        "layovers": [{"airport": "YVR", "wait_min": 95}],
        "baggage_note": "含1件随身", "overnight": True, "source": "serpapi",
    }


def _route():
    return Route(id="yul-pek", origin="YUL", dest="PEK",
                 dates={"mode": "rolling", "depart_in_days": 90})


def _summary(headline):
    return {"routes": {"yul-pek": {
        "depart_dates": {"2026-08-06": {
            "latest": _low(5200), "historical_low": _low(5200),
            "series": [{"price": 5200}]}},
        "headline": headline,
    }}}


class TestDigestShowSegments(unittest.TestCase):
    def test_expands_segments_when_on(self):
        card = build_digest_card([], {"routes_failed": 0}, summary=_summary(_multi_headline()),
                                 routes=[_route()], run_date="2026-08-01", show_segments=True)
        blob = json.dumps(card, ensure_ascii=False)
        self.assertIn("第1段", blob)
        self.assertIn("第2段", blob)
        self.assertIn("⏱ 中转", blob)
        self.assertIn("等待", blob)
        # 逐段展示时仍保留行李/中转托运说明
        self.assertIn("中转无需重新托运", blob)

    def test_falls_back_to_single_line_when_off(self):
        card = build_digest_card([], {"routes_failed": 0}, summary=_summary(_multi_headline()),
                                 routes=[_route()], run_date="2026-08-01", show_segments=False)
        blob = json.dumps(card, ensure_ascii=False)
        self.assertNotIn("第1段", blob)          # 未展开逐段
        self.assertNotIn("⏱ 中转", blob)
        self.assertIn("🧳 行李", blob)            # 单行摘要仍有行李行
        self.assertIn("Air China CA 992", blob)  # _headline_lines 的航班行


class TestHiddenCitySegments(unittest.TestCase):
    def _hit(self, segments=True):
        hit = {"origin": "YUL", "onward_dest": "BKK", "depart_date": "2026-08-06",
               "layover_cn": "PVG", "layover_city_cn": "上海", "price_cny": 4050,
               "airline": "China Eastern", "flight_no": "MU 500", "depart_time": "20:55",
               "direct_price_cny": None, "saving_pct": None,
               "suspected": False, "source": "serpapi"}
        if segments:
            hit["segments"] = [
                {"leg": 1, "flight_no": "MU 500", "from": "YUL", "from_time": "2026-08-06 20:55",
                 "to": "PVG", "to_time": "2026-08-07 23:40", "duration_min": 780, "airplane": ""},
                {"leg": 2, "flight_no": "MU 501", "from": "PVG", "from_time": "2026-08-08 01:10",
                 "to": "BKK", "to_time": "2026-08-08 05:20", "duration_min": 250, "airplane": ""},
            ]
            hit["layovers"] = [{"airport": "PVG", "wait_min": 90}]
        return hit

    def test_hidden_card_expands_segments(self):
        card = build_hidden_city_card([self._hit()])
        blob = json.dumps(card, ensure_ascii=False)
        self.assertIn("第1段", blob)
        self.assertIn("⏱ 中转 上海浦东(PVG) 等待 1h30m", blob)
        # 行李/风险提示仍在
        self.assertIn("隐藏城市票行李会直挂票面终点", blob)
        self.assertIn("风险自负", blob)

    def test_hidden_card_off_falls_back(self):
        card = build_hidden_city_card([self._hit()], show_segments=False)
        blob = json.dumps(card, ensure_ascii=False)
        self.assertNotIn("第1段", blob)
        self.assertIn("China Eastern MU 500", blob)  # 单行摘要

    def test_hidden_card_no_segments_single_line(self):
        card = build_hidden_city_card([self._hit(segments=False)])
        blob = json.dumps(card, ensure_ascii=False)
        self.assertNotIn("第1段", blob)
        self.assertIn("China Eastern MU 500", blob)

    def test_detail_expand_limit(self):
        # 第 6 条及以后即使有 segments 也不展开逐段（只前 5 条详细展开）。
        hits = []
        for i in range(7):
            h = self._hit()
            h["onward_dest"] = ["BKK", "SGN", "MNL", "DPS", "KUL", "HAN", "TPE"][i]
            hits.append(h)
        card = build_hidden_city_card(hits, limit=8)
        # 前 5 条展开逐段 -> "第1段" 应出现 5 次；第 6/7 条回退单行。
        blob = json.dumps(card, ensure_ascii=False)
        self.assertEqual(blob.count("第1段"), 5)


if __name__ == "__main__":
    unittest.main()
