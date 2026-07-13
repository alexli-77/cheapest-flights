"""Telegram notifier — skeleton implementation (milestone M2).

Disabled by default (config ``notifiers.telegram.enabled: false``). It exists to
validate the REGISTRY mechanism and to give a working Bot API path when the user
sets ``TG_BOT_TOKEN`` / ``TG_CHAT_ID``. Messages are plain text (Telegram has no
Feishu-style interactive cards).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Callable, Optional

from .base import Notifier, register_notifier

log = logging.getLogger("flight_watch.notifiers.telegram")

TOKEN_ENV = "TG_BOT_TOKEN"
CHAT_ENV = "TG_CHAT_ID"


def _fmt(v) -> str:
    if v is None:
        return "-"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return str(int(f)) if f == int(f) else f"{f:.2f}"


def build_digest_text(alerts: list, stats: dict) -> str:
    stats = stats or {}
    lines = [
        f"✈️ 机票监控日报 {stats.get('run_date', '')} · {stats.get('run_status', '运行正常')}".strip(),
        (f"成功 {stats.get('routes_ok', 0)} / 失败 {stats.get('routes_failed', 0)} 条航线"
         f" · 抓取 {stats.get('fetched_count', 0)} 条"),
    ]
    if alerts:
        lines.append(f"今日价格异动 {len(alerts)} 条：")
        for a in alerts:
            lines.append(f"· [{a.rule_id}] {a.route_id} {a.depart_date} 最低 {_fmt(a.price)}")
    else:
        lines.append("今日无价格异动。")
    return "\n".join(lines)


def build_urgent_text(alert) -> str:
    return f"🔥 紧急降价\n{alert.message}"


def default_transport(token: str, chat_id: str, text: str) -> bool:
    """Send a Telegram message via Bot API sendMessage. Honors NOTIFY_DRY_RUN."""
    if os.environ.get("NOTIFY_DRY_RUN") == "1":
        print("[NOTIFY_DRY_RUN telegram] " + json.dumps(
            {"chat_id": chat_id, "text": text}, ensure_ascii=False))
        return True
    if not token or not chat_id:
        log.warning("telegram TG_BOT_TOKEN / TG_CHAT_ID missing, skip send")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    try:
        import requests  # type: ignore
        resp = requests.post(url, data=body, headers=headers, timeout=15)
        return resp.status_code == 200
    except ImportError:
        import urllib.request
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=15) as r:
                return getattr(r, "status", 200) == 200
        except Exception as e:
            log.warning("telegram urllib send failed: %s", e)
            return False
    except Exception as e:
        log.warning("telegram send failed: %s", e)
        return False


@register_notifier("telegram")
class TelegramNotifier(Notifier):
    name = "telegram"

    def __init__(self, cfg: Optional[dict] = None,
                 transport: Optional[Callable[[str, str, str], bool]] = None):
        super().__init__(cfg)
        self.transport = transport or default_transport

    def _creds(self):
        return os.environ.get(TOKEN_ENV, ""), os.environ.get(CHAT_ENV, "")

    def _send(self, text: str) -> bool:
        token, chat_id = self._creds()
        try:
            return bool(self.transport(token, chat_id, text))
        except Exception as e:
            log.warning("telegram transport raised: %s", e)
            return False

    def send_digest(self, alerts: list, stats: dict,
                    summary: Optional[dict] = None,
                    routes: Optional[list] = None) -> bool:
        # Telegram digest stays plain text; summary/routes accepted for
        # interface parity but not rendered here.
        return self._send(build_digest_text(alerts or [], stats or {}))

    def send_urgent(self, alert, summary: Optional[dict] = None) -> bool:
        return self._send(build_urgent_text(alert))
