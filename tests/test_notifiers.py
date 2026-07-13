import os
import sys
import base64
import hashlib
import hmac
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import Config, Route  # noqa: E402
from src.alerts.rules import Alert  # noqa: E402
from src.notifiers import dispatch, build_stats, REGISTRY  # noqa: E402
from src.notifiers import feishu  # noqa: E402
from src.notifiers.feishu import (  # noqa: E402
    build_digest_card, build_urgent_card, sign, FeishuNotifier, _mask_url,
    gflights_url, _airport_label,
)


def _alert(level="urgent", rule="below_target", price=1400, prev=1600, target=1500):
    return Alert(rule_id=rule, level=level, route_id="sha-nrt",
                 depart_date="2026-10-01", price=price, prev_price=prev,
                 target_price=target, message="SHA->NRT 2026-10-01 今日最低 1400")


def _low(price, airline="Air China", flight_no="CA880", depart_time="20:55",
         fetch_date="2026-07-18", currency="CNY"):
    return {"fetch_date": fetch_date, "price": price, "currency": currency,
            "airline": airline, "flight_no": flight_no, "depart_time": depart_time}


def _rolling_route():
    return Route(id="sha-nrt", origin="SHA", dest="NRT",
                 dates={"mode": "rolling", "depart_in_days": 90})


def _rolling_summary():
    return {"routes": {"sha-nrt": {"depart_dates": {
        "2026-07-19": {
            "latest": _low(3110),
            "historical_low": _low(3110),
            "series": [{"price": 3380}, {"price": 3110}],
        },
        "2026-08-05": {
            "latest": _low(3600, flight_no="CA982", depart_time="11:20"),
            "historical_low": _low(3600),
            "series": [{"price": 3600}],
        },
    }}}}


def _cfg(notifiers):
    return Config(
        timezone="Asia/Shanghai", defaults={}, routes=[], cross_check={},
        alerts={"daily_digest": True}, notifiers=notifiers,
        dashboard={"url": "https://example.github.io/flight-watch/"}, raw={},
    )


class TestRegistry(unittest.TestCase):
    def test_registry_loads_feishu_and_telegram(self):
        self.assertIn("feishu", REGISTRY)
        self.assertIn("telegram", REGISTRY)


class TestFeishuCards(unittest.TestCase):
    def test_urgent_card_structure(self):
        card = build_urgent_card(_alert(), dashboard_url="https://d/")
        self.assertEqual(card["msg_type"], "interactive")
        self.assertEqual(card["card"]["header"]["template"], "red")
        self.assertEqual(card["card"]["header"]["title"]["content"], "🔥 紧急降价提醒")
        # last element is the action button linking to the route anchor
        action = card["card"]["elements"][-1]
        self.assertEqual(action["tag"], "action")
        btn = action["actions"][0]
        self.assertEqual(btn["tag"], "button")
        self.assertEqual(btn["url"], "https://d/?route=sha-nrt")

    def test_gflights_url(self):
        url = gflights_url("YUL", "PEK", "2026-07-15")
        self.assertTrue(url.startswith("https://www.google.com/travel/flights?q="))
        # one-way marker + endpoints present (url-encoded)
        self.assertIn("one%20way", url)
        self.assertIn("YUL", url)
        self.assertIn("PEK", url)
        self.assertIn("2026-07-15", url)
        # missing endpoints -> empty string, never a broken link
        self.assertEqual(gflights_url("", "PEK", "2026-07-15"), "")

    def test_airport_label_cn(self):
        self.assertEqual(_airport_label("YUL"), "蒙特利尔(YUL)")
        self.assertEqual(_airport_label("PEK"), "北京(PEK)")
        self.assertEqual(_airport_label("ZZZ"), "ZZZ")  # unknown -> bare code

    def test_digest_route_block_has_cn_name_and_buy_link(self):
        card = build_digest_card([], {"routes_failed": 0}, summary=_rolling_summary(),
                                 routes=[_rolling_route()], dashboard_url="https://d/",
                                 run_date="2026-07-10")
        blob = str(card["card"]["elements"])
        # 航线行显示中文城市名（SHA=上海, NRT=东京）
        self.assertIn("上海(SHA)", blob)
        self.assertIn("东京(NRT)", blob)
        # 最低价下方的购票渠道深链
        self.assertIn("查看购票渠道", blob)
        self.assertIn("google.com/travel/flights", blob)

    def test_urgent_card_cn_name_and_buy_button(self):
        card = build_urgent_card(_alert(), dashboard_url="https://d/")
        blob = str(card["card"]["elements"])
        # route_id sha-nrt -> 中文城市名航线
        self.assertIn("上海(SHA)", blob)
        self.assertIn("东京(NRT)", blob)
        # 按钮区有第二个「购票渠道」按钮
        action = card["card"]["elements"][-1]
        texts = [b["text"]["content"] for b in action["actions"]]
        self.assertIn("立即查看", texts)
        self.assertIn("购票渠道", texts)
        buy = [b for b in action["actions"] if b["text"]["content"] == "购票渠道"][0]
        self.assertIn("google.com/travel/flights", buy["url"])

    def test_urgent_card_flight_line(self):
        # When a flight record is supplied, a 航班 line is appended.
        card = build_urgent_card(_alert(), dashboard_url="https://d/",
                                 flight=_low(1400, airline="Air China",
                                             flight_no="CA880", depart_time="20:55"))
        blob = str(card["card"]["elements"])
        self.assertIn("航班", blob)
        self.assertIn("Air China CA880", blob)
        self.assertIn("20:55", blob)

    def test_digest_rolling_route_block(self):
        stats = {"routes_ok": 1, "routes_failed": 0, "fetched_count": 42,
                 "serpapi_remaining_quota": 87, "run_date": "2026-07-10"}
        card = build_digest_card([_alert(level="urgent")], stats,
                                 summary=_rolling_summary(), routes=[_rolling_route()],
                                 dashboard_url="https://d/", run_date="2026-07-10")
        self.assertEqual(card["msg_type"], "interactive")
        self.assertEqual(card["card"]["header"]["template"], "blue")
        self.assertIn("2026-07-10", card["card"]["header"]["title"]["content"])
        blob = str(card["card"]["elements"])
        # compact route block: cheapest across depart_dates (3110 < 3600)
        self.assertIn("✈️ 上海(SHA) → 东京(NRT)（未来90天）", blob)
        self.assertIn("最低 ¥3110", blob)
        self.assertIn("07-19", blob)
        self.assertIn("Air China CA880", blob)
        self.assertIn("20:55", blob)
        self.assertIn("环比 -8%", blob)  # 3110 vs 3380
        # single 异动统计 line, NOT a per-alert table
        self.assertIn("今日 1 条价格异动，紧急 1 条", blob)
        self.assertFalse(any(e.get("fields") for e in card["card"]["elements"]))
        # dashboard button present
        self.assertTrue(any(e.get("tag") == "action" for e in card["card"]["elements"]))

    def test_digest_fixed_route_block(self):
        route = Route(id="sha-nrt", origin="SHA", dest="NRT",
                      dates={"mode": "fixed", "fixed_dates": ["2026-07-19", "2026-08-01"]})
        summary = {"routes": {"sha-nrt": {"depart_dates": {
            "2026-07-19": {"latest": _low(3110), "historical_low": _low(3110),
                           "series": [{"price": 3110}]},
            "2026-08-01": {"latest": _low(2980), "historical_low": _low(2980),
                           "series": [{"price": 2980}]},
        }}}}
        card = build_digest_card([], {"routes_failed": 0}, summary=summary,
                                 routes=[route], run_date="2026-07-10")
        blob = str(card["card"]["elements"])
        self.assertIn("固定日期", blob)
        self.assertIn("07-19: ¥3110", blob)
        self.assertIn("08-01: ¥2980", blob)

    def test_digest_heartbeat_no_alerts(self):
        # heartbeat: digest still built with run stats when no alerts / no data
        stats = {"routes_ok": 3, "routes_failed": 1, "fetched_count": 10,
                 "run_status": "1 条航线异常"}
        card = build_digest_card([], stats, summary={"routes": {}},
                                 routes=[_rolling_route()], run_date="2026-07-10",
                                 run_status="1 条航线异常")
        # failed routes -> orange header
        self.assertEqual(card["card"]["header"]["template"], "orange")
        text_blob = str(card["card"]["elements"])
        self.assertIn("今日无价格异动", text_blob)
        self.assertIn("失败 1", text_blob)
        self.assertIn("暂无数据", text_blob)  # route present but no summary data

    def test_sign_matches_reference(self):
        ts = "1700000000"
        secret = "mysecret"
        expected_key = f"{ts}\n{secret}".encode("utf-8")
        expected = base64.b64encode(
            hmac.new(expected_key, b"", hashlib.sha256).digest()).decode()
        self.assertEqual(sign(ts, secret), expected)

    def test_mask_url_hides_token(self):
        masked = _mask_url("https://open.feishu.cn/open-apis/bot/v2/hook/abcd1234secret")
        self.assertNotIn("abcd1234secret", masked)
        self.assertIn("open.feishu.cn", masked)


class _Capture:
    """Mock transport capturing (url, payload) instead of hitting the network."""

    def __init__(self):
        self.calls = []

    def __call__(self, url, payload):
        self.calls.append((url, payload))
        return True


class TestFeishuNotifierTransport(unittest.TestCase):
    def setUp(self):
        os.environ["FEISHU_WEBHOOK"] = "https://open.feishu.cn/hook/testtoken"
        os.environ.pop("FEISHU_SECRET", None)

    def tearDown(self):
        os.environ.pop("FEISHU_WEBHOOK", None)
        os.environ.pop("FEISHU_SECRET", None)

    def test_send_urgent_uses_transport(self):
        cap = _Capture()
        n = FeishuNotifier({"dashboard": {"url": "https://d/"}}, transport=cap)
        self.assertTrue(n.send_urgent(_alert()))
        self.assertEqual(len(cap.calls), 1)
        url, payload = cap.calls[0]
        self.assertEqual(url, "https://open.feishu.cn/hook/testtoken")
        self.assertEqual(payload["card"]["header"]["template"], "red")
        self.assertNotIn("sign", payload)  # no FEISHU_SECRET set

    def test_signing_when_secret_present(self):
        os.environ["FEISHU_SECRET"] = "s3cr3t"
        cap = _Capture()
        n = FeishuNotifier({}, transport=cap)
        n.send_digest([], {"run_status": "运行正常"})
        _, payload = cap.calls[0]
        self.assertIn("sign", payload)
        self.assertIn("timestamp", payload)


class TestDispatch(unittest.TestCase):
    def test_dispatch_only_enabled_channels(self):
        # feishu enabled with mock transport; telegram disabled
        cap = _Capture()

        # monkeypatch feishu default transport so the real class uses our capture
        orig = feishu.default_transport
        feishu.default_transport = cap
        try:
            os.environ["FEISHU_WEBHOOK"] = "https://open.feishu.cn/hook/x"
            cfg = _cfg({"feishu": {"enabled": True, "secret_env": "FEISHU_WEBHOOK"},
                        "telegram": {"enabled": False}})
            alerts = [_alert(level="urgent"), _alert(level="normal", rule="drop_pct")]
            summary = {"meta": {"run_stats": {"routes_ok": 1, "routes_failed": 0,
                                              "fetched_count": 5, "run_date": "2026-07-10"}}}
            report = dispatch(cfg, summary, alerts=alerts, sleep_fn=lambda *_: None)
        finally:
            feishu.default_transport = orig
            os.environ.pop("FEISHU_WEBHOOK", None)

        self.assertIn("feishu", report)
        self.assertNotIn("telegram", report)
        # 1 urgent single-push + 1 digest = 2 transport calls
        self.assertEqual(len(cap.calls), 2)
        self.assertEqual(report["feishu"]["urgent"], 1)
        self.assertTrue(report["feishu"]["digest"])

    def test_build_stats_from_meta(self):
        cfg = _cfg({})
        summary = {"generated_at": "2026-07-10T09:00:00+08:00",
                   "meta": {"run_stats": {"routes_ok": 2, "routes_failed": 1,
                                          "fetched_count": 9},
                            "serpapi_remaining_quota": 50}}
        stats = build_stats(cfg, summary)
        self.assertEqual(stats["routes_ok"], 2)
        self.assertEqual(stats["routes_failed"], 1)
        self.assertEqual(stats["serpapi_remaining_quota"], 50)
        self.assertEqual(stats["run_status"], "1 条航线异常")
        self.assertEqual(stats["run_date"], "2026-07-10")


class TestDryRun(unittest.TestCase):
    def test_dry_run_prints_not_sends(self):
        os.environ["NOTIFY_DRY_RUN"] = "1"
        os.environ.pop("FEISHU_WEBHOOK", None)  # no URL, but dry-run should still "succeed"
        try:
            n = FeishuNotifier({})
            self.assertTrue(n.send_urgent(_alert()))  # printed, returns True
        finally:
            os.environ.pop("NOTIFY_DRY_RUN", None)


if __name__ == "__main__":
    unittest.main()
