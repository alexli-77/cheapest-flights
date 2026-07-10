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
)


def _alert(level="urgent", rule="below_target", price=1400, prev=1600, target=1500):
    return Alert(rule_id=rule, level=level, route_id="sha-nrt",
                 depart_date="2026-10-01", price=price, prev_price=prev,
                 target_price=target, message="SHA->NRT 2026-10-01 今日最低 1400")


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

    def test_digest_card_structure_and_fields(self):
        stats = {"routes_ok": 2, "routes_failed": 0, "fetched_count": 42,
                 "serpapi_remaining_quota": 87, "run_date": "2026-07-10",
                 "run_status": "运行正常"}
        card = build_digest_card([_alert(level="normal")], stats,
                                 dashboard_url="https://d/",
                                 run_date="2026-07-10", run_status="运行正常")
        self.assertEqual(card["msg_type"], "interactive")
        self.assertEqual(card["card"]["header"]["template"], "blue")
        self.assertIn("2026-07-10", card["card"]["header"]["title"]["content"])
        # a div with the 5+ table-style fields must be present
        field_divs = [e for e in card["card"]["elements"] if e.get("fields")]
        self.assertTrue(field_divs)
        labels = [f["text"]["content"].split("\n")[0] for f in field_divs[0]["fields"]]
        for expected in ("**航线**", "**出发日**", "**今日最低**", "**环比**", "**距目标价**"):
            self.assertIn(expected, labels)
        # dashboard button present
        self.assertTrue(any(e.get("tag") == "action" for e in card["card"]["elements"]))

    def test_digest_heartbeat_no_alerts(self):
        # heartbeat: digest still built with run stats when no alerts
        stats = {"routes_ok": 3, "routes_failed": 1, "fetched_count": 10,
                 "run_status": "1 条航线异常"}
        card = build_digest_card([], stats, run_date="2026-07-10",
                                 run_status="1 条航线异常")
        # failed routes -> orange header
        self.assertEqual(card["card"]["header"]["template"], "orange")
        text_blob = str(card["card"]["elements"])
        self.assertIn("今日无价格异动", text_blob)
        self.assertIn("失败 1", text_blob)

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
