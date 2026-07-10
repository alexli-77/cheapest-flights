"""Feishu (Lark) custom-bot webhook notifier (milestone M2).

The webhook URL is read from the environment variable named by the config
(``notifiers.feishu.secret_env``, default ``FEISHU_WEBHOOK``) so it never enters
the repo. Optional request signing uses ``FEISHU_SECRET`` (HmacSHA256 over the
key ``"<timestamp>\\n<secret>"`` with an empty message body, Base64 encoded).

Messages are Feishu *interactive* cards:
  * digest card  -> title (date + run status) + table-style fields
    (航线/出发日/今日最低/环比/距目标价) + a button to the dashboard.
  * urgent card  -> red header + single-route detail + button.

Network transport degrades gracefully: ``requests`` if present else ``urllib``.
Setting ``NOTIFY_DRY_RUN=1`` prints the card JSON instead of sending. Webhook
URLs are masked in logs.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Callable, Optional

from .base import Notifier, register_notifier

log = logging.getLogger("flight_watch.notifiers.feishu")

DEFAULT_WEBHOOK_ENV = "FEISHU_WEBHOOK"
SECRET_ENV = "FEISHU_SECRET"


# --------------------------------------------------------------- formatting
def _fmt(v) -> str:
    if v is None:
        return "-"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return str(int(f)) if f == int(f) else f"{f:.2f}"


def _pct_change(price, prev_price) -> str:
    """Period-over-period change string, e.g. ``-12.3%`` (down) / ``+4.0%``."""
    if price is None or prev_price in (None, 0):
        return "-"
    try:
        chg = (float(price) - float(prev_price)) / float(prev_price) * 100.0
    except (TypeError, ValueError, ZeroDivisionError):
        return "-"
    sign = "+" if chg >= 0 else ""
    return f"{sign}{chg:.1f}%"


def _gap_to_target(price, target_price) -> str:
    """Distance to target, e.g. ``-120``（低于目标） / ``+300``（高于目标）。"""
    if price is None or target_price is None:
        return "-"
    try:
        gap = float(price) - float(target_price)
    except (TypeError, ValueError):
        return "-"
    sign = "+" if gap >= 0 else ""
    return f"{sign}{_fmt(gap)}"


def _mask_url(url: str) -> str:
    """Mask a webhook URL for logs: keep scheme/host + last 4 chars of token."""
    if not url:
        return "<empty>"
    try:
        head, _, tail = url.partition("://")
        host = tail.split("/", 1)[0]
        return f"{head}://{host}/***{url[-4:]}"
    except Exception:
        return "***"


# ------------------------------------------------------------------ signing
def sign(timestamp, secret: str) -> str:
    """Feishu custom-bot signature: HmacSHA256(key=f"{ts}\\n{secret}", msg="")."""
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        string_to_sign.encode("utf-8"), b"", digestmod=hashlib.sha256
    ).digest()
    return base64.b64encode(hmac_code).decode("utf-8")


# ------------------------------------------------------------- card builders
def _button(text: str, url: str) -> dict:
    return {
        "tag": "action",
        "actions": [{
            "tag": "button",
            "text": {"tag": "plain_text", "content": text},
            "type": "primary",
            "url": url or "",
        }],
    }


def _dashboard_url(base_url: str, route_id: str = "") -> str:
    base_url = base_url or ""
    if not base_url:
        return ""
    if route_id:
        return f"{base_url}?route={route_id}"
    return base_url


def _alert_fields(alert) -> list:
    """Table-style fields for one alert: 航线/出发日/今日最低/环比/距目标价."""
    price = getattr(alert, "price", None)
    prev = getattr(alert, "prev_price", None)
    target = getattr(alert, "target_price", None)
    return [
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**航线**\n{alert.route_id}"}},
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**出发日**\n{alert.depart_date or '-'}"}},
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**今日最低**\n{_fmt(price)}"}},
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**环比**\n{_pct_change(price, prev)}"}},
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**距目标价**\n{_gap_to_target(price, target)}"}},
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**规则**\n{alert.rule_id}"}},
    ]


def build_digest_card(alerts: list, stats: dict, dashboard_url: str = "",
                      run_date: str = "", run_status: str = "运行正常") -> dict:
    """Build the daily digest interactive card (heartbeat-safe)."""
    stats = stats or {}
    title = f"✈️ 机票监控日报 {run_date} · {run_status}".strip()

    elements: list = []

    # Run statistics block (heartbeat content, always present).
    stat_line = (
        f"成功 {stats.get('routes_ok', 0)} / 失败 {stats.get('routes_failed', 0)} 条航线"
        f" · 抓取 {stats.get('fetched_count', 0)} 条"
    )
    if stats.get("serpapi_remaining_quota") is not None:
        stat_line += f" · SerpAPI 余额 {stats.get('serpapi_remaining_quota')}"
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": stat_line}})
    elements.append({"tag": "hr"})

    if alerts:
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"**今日价格异动（{len(alerts)} 条）**"},
        })
        for a in alerts:
            elements.append({"tag": "div", "fields": _alert_fields(a)})
            elements.append({"tag": "hr"})
    else:
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "今日无价格异动，一切正常。"},
        })

    elements.append(_button("查看趋势图 Dashboard", _dashboard_url(dashboard_url)))

    template = "blue" if stats.get("routes_failed", 0) == 0 else "orange"
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": template,
            },
            "elements": elements,
        },
    }


def build_urgent_card(alert, dashboard_url: str = "") -> dict:
    """Build a red-header urgent card for a single alert."""
    price = getattr(alert, "price", None)
    prev = getattr(alert, "prev_price", None)
    target = getattr(alert, "target_price", None)

    detail = (
        f"**{alert.message}**\n\n"
        f"航线：{alert.route_id}　出发日：{alert.depart_date or '-'}\n"
        f"今日最低：{_fmt(price)}　环比：{_pct_change(price, prev)}"
        f"　距目标价：{_gap_to_target(price, target)}"
    )
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "🔥 紧急降价提醒"},
                "template": "red",
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": detail}},
                _button("立即查看", _dashboard_url(dashboard_url, alert.route_id)),
            ],
        },
    }


# -------------------------------------------------------------- transport
def default_transport(url: str, payload: dict) -> bool:
    """POST ``payload`` as JSON to ``url``. Honors NOTIFY_DRY_RUN.

    Returns True on success. Never raises — logs and returns False on error.
    """
    if os.environ.get("NOTIFY_DRY_RUN") == "1":
        print("[NOTIFY_DRY_RUN feishu] " + json.dumps(payload, ensure_ascii=False))
        return True
    if not url:
        log.warning("feishu webhook URL empty, skip send")
        return False

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}

    try:  # prefer requests, degrade to urllib
        import requests  # type: ignore
        resp = requests.post(url, data=body, headers=headers, timeout=15)
        ok = resp.status_code == 200
        if not ok:
            log.warning("feishu send to %s -> HTTP %s", _mask_url(url), resp.status_code)
        return ok
    except ImportError:
        import urllib.request
        import urllib.error
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=15) as r:
                return getattr(r, "status", 200) == 200
        except Exception as e:
            log.warning("feishu urllib send to %s failed: %s", _mask_url(url), e)
            return False
    except Exception as e:
        log.warning("feishu send to %s failed: %s", _mask_url(url), e)
        return False


# -------------------------------------------------------------- notifier
@register_notifier("feishu")
class FeishuNotifier(Notifier):
    name = "feishu"

    def __init__(self, cfg: Optional[dict] = None,
                 transport: Optional[Callable[[str, dict], bool]] = None):
        super().__init__(cfg)
        self.transport = transport or default_transport
        self.webhook_env = (self.cfg.get("secret_env") or DEFAULT_WEBHOOK_ENV)

    # --------------------------------------------------------- helpers
    def _webhook_url(self) -> str:
        return os.environ.get(self.webhook_env, "")

    def _dashboard_url(self) -> str:
        dash = (self.cfg.get("dashboard") or {})
        return dash.get("url", "") if isinstance(dash, dict) else ""

    def _maybe_sign(self, payload: dict) -> dict:
        """Add timestamp + sign fields if FEISHU_SECRET is set."""
        secret = os.environ.get(SECRET_ENV)
        if not secret:
            return payload
        ts = str(int(time.time()))
        signed = dict(payload)
        signed["timestamp"] = ts
        signed["sign"] = sign(ts, secret)
        return signed

    def _send(self, card: dict) -> bool:
        payload = self._maybe_sign(card)
        url = self._webhook_url()
        log.info("feishu -> %s", _mask_url(url))
        try:
            return bool(self.transport(url, payload))
        except Exception as e:  # transport must never sink the pipeline
            log.warning("feishu transport raised: %s", e)
            return False

    # --------------------------------------------------------- interface
    def send_digest(self, alerts: list, stats: dict) -> bool:
        stats = stats or {}
        card = build_digest_card(
            alerts or [], stats,
            dashboard_url=self._dashboard_url(),
            run_date=stats.get("run_date", ""),
            run_status=stats.get("run_status", "运行正常"),
        )
        return self._send(card)

    def send_urgent(self, alert) -> bool:
        card = build_urgent_card(alert, dashboard_url=self._dashboard_url())
        return self._send(card)
