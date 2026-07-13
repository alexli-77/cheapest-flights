"""Feishu (Lark) custom-bot webhook notifier (milestone M2).

The webhook URL is read from the environment variable named by the config
(``notifiers.feishu.secret_env``, default ``FEISHU_WEBHOOK``) so it never enters
the repo. Optional request signing uses ``FEISHU_SECRET`` (HmacSHA256 over the
key ``"<timestamp>\\n<secret>"`` with an empty message body, Base64 encoded).

Messages are Feishu *interactive* cards (redesigned 2026-07 to be compact):
  * digest card  -> title (date + run status) + one compact block per route
    (航线 + 最低价摘要：价格/出发日/航司航班/起飞时间/环比) + a single 异动统计
    line + a button to the dashboard. No more per-alert long table.
  * urgent card  -> red header + single-route detail + 航班信息行 + button.

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
import urllib.parse
from typing import Callable, Optional

from .base import Notifier, register_notifier

try:  # airport 中文名表；解析失败/缺失时退回三字码，绝不影响发送。
    from ..airports import lookup as _airport_lookup
except Exception:  # pragma: no cover - defensive
    _airport_lookup = None

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


def _airport_label(iata: str) -> str:
    """"YUL" -> "蒙特利尔(YUL)"; falls back to the bare code when unknown."""
    code = str(iata or "").upper().strip()
    if not code:
        return code
    city = ""
    if _airport_lookup is not None:
        try:
            city = (_airport_lookup(code) or {}).get("city_cn") or ""
        except Exception:
            city = ""
    return f"{city}({code})" if city else code


def _od_from_route_id(route_id: str) -> tuple:
    """"yul-pek" -> ("YUL", "PEK"). Non-``a-b`` ids yield ("", "")."""
    parts = str(route_id or "").split("-")
    if len(parts) == 2 and parts[0] and parts[1]:
        return parts[0].upper(), parts[1].upper()
    return "", ""


def gflights_url(origin: str, dest: str, depart_date: str = "", one_way: bool = True) -> str:
    """Google Flights deep link for a one-way search (购票渠道).

    ``https://www.google.com/travel/flights?q=<url-encoded query>`` where the
    query is e.g. ``"Flights from YUL to PEK on 2026-07-15 one way"``. Returns ""
    when origin/dest are missing.
    """
    o = str(origin or "").upper().strip()
    d = str(dest or "").upper().strip()
    if not o or not d:
        return ""
    q = f"Flights from {o} to {d}"
    if depart_date:
        q += f" on {depart_date}"
    if one_way:
        q += " one way"
    return "https://www.google.com/travel/flights?q=" + urllib.parse.quote(q)


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


_WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _mmdd(date_str: str) -> str:
    """"2026-07-19" -> "07-19" (leave non-standard strings untouched)."""
    parts = str(date_str or "").split("-")
    return "-".join(parts[1:]) if len(parts) == 3 else str(date_str or "")


def _weekday_cn(date_str: str) -> str:
    try:
        from datetime import date as _date
        y, m, d = (int(x) for x in str(date_str).split("-"))
        return _WEEKDAY_CN[_date(y, m, d).weekday()]
    except Exception:
        return ""


def _price_str(low: dict) -> str:
    """"¥3110" for CNY, else "<code> 3110"."""
    if not low:
        return "-"
    price = low.get("price")
    cur = (low.get("currency") or "CNY")
    return f"¥{_fmt(price)}" if cur == "CNY" else f"{cur} {_fmt(price)}"


def _flight_info_str(low: dict) -> str:
    """"Air China CA880 · 20:55" from a summary low record (fields optional)."""
    if not low:
        return ""
    airline = str(low.get("airline") or "").strip()
    flight_no = str(low.get("flight_no") or "").strip()
    dt = str(low.get("depart_time") or "").strip()
    parts = []
    carrier = " ".join(x for x in (airline, flight_no) if x)
    if carrier:
        parts.append(carrier)
    if dt:
        parts.append(dt)
    return " · ".join(parts)


def _digest_pct(node: dict) -> str:
    """"环比 -8%" comparing the latest fetch low to the previous fetch low for a
    depart_date, or "" when there is no prior point or no meaningful change."""
    series = (node or {}).get("series") or []
    if len(series) < 2:
        return ""
    prev = series[-2].get("price")
    cur = series[-1].get("price")
    if not prev or cur is None or prev == 0:
        return ""
    chg = (float(cur) - float(prev)) / float(prev) * 100.0
    if round(chg) == 0:
        return ""
    sign = "+" if chg > 0 else "-"
    return f"环比 {sign}{abs(round(chg))}%"


def _route_window_label(route) -> str:
    """"未来90天" / "固定日期" / "滚动+固定" from a route's dates block."""
    dates = getattr(route, "dates", {}) or {}
    mode = (dates.get("mode") or "fixed").lower()
    did = dates.get("depart_in_days")
    if isinstance(did, (list, tuple)):
        n = max((int(x) for x in did if str(x).lstrip("-").isdigit()), default=0)
    else:
        try:
            n = int(did or 0)
        except (TypeError, ValueError):
            n = 0
    if mode == "rolling":
        return f"未来{n}天"
    if mode == "fixed":
        return "固定日期"
    if mode == "both":
        return f"未来{n}天+固定"
    return mode


def _route_block(route, node_map: dict) -> dict:
    """Build one compact digest block (a lark_md div) for a single route.

    ``node_map`` = summary["routes"][id]["depart_dates"] (may be empty).
    """
    origin = str(getattr(route, "origin", "") or "").upper()
    dest = str(getattr(route, "dest", "") or "").upper()
    header = f"✈️ {_airport_label(origin)} → {_airport_label(dest)}（{_route_window_label(route)}）"

    dates = getattr(route, "dates", {}) or {}
    mode = (dates.get("mode") or "fixed").lower()

    # depart_dates that actually have a latest low.
    usable = {dd: n for dd, n in (node_map or {}).items()
              if n and n.get("latest") and n["latest"].get("price") is not None}

    link_dd = ""  # depart_date the 购票渠道 link should point at (最低价对应日期)
    if not usable:
        body = "暂无数据"
    elif mode == "fixed":
        # One entry per depart_date: "07-19: ¥3110 · 08-01: ¥2980".
        segs = []
        for dd in sorted(usable):
            segs.append(f"{_mmdd(dd)}: {_price_str(usable[dd]['latest'])}")
        body = " · ".join(segs)
        link_dd = min(usable, key=lambda dd: usable[dd]["latest"]["price"])
    else:
        # Rolling / both: single cheapest across all depart_dates.
        best_dd = min(usable, key=lambda dd: usable[dd]["latest"]["price"])
        node = usable[best_dd]
        low = node["latest"]
        parts = [f"最低 {_price_str(low)}"]
        wd = _weekday_cn(best_dd)
        parts.append(f"{_mmdd(best_dd)} {wd}".strip())
        fi = _flight_info_str(low)
        if fi:
            parts.append(fi)
        pct = _digest_pct(node)
        if pct:
            parts.append(pct)
        body = " · ".join(parts)
        link_dd = best_dd

    content = f"**{header}**\n{body}"
    if link_dd:
        url = gflights_url(origin, dest, link_dd)
        if url:
            content += f"\n[→ 查看购票渠道]({url})"

    return {"tag": "div", "text": {"tag": "lark_md", "content": content}}


def build_digest_card(alerts: list, stats: dict, summary: dict = None,
                      routes: list = None, dashboard_url: str = "",
                      run_date: str = "", run_status: str = "运行正常") -> dict:
    """Build the compact daily digest interactive card (heartbeat-safe).

    One block per route showing that route's cheapest摘要 (from the enhanced
    summary), then a single 异动统计 line and a dashboard button. The old
    per-alert long table is gone.
    """
    stats = stats or {}
    summary = summary or {}
    routes = routes or []
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

    # One compact block per route (only enabled routes).
    routes_summary = (summary.get("routes") or {})
    shown = 0
    for route in routes:
        if getattr(route, "enabled", True) is False:
            continue
        node_map = (routes_summary.get(getattr(route, "id", "")) or {}).get("depart_dates", {})
        elements.append(_route_block(route, node_map))
        shown += 1
    if shown == 0:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "暂无航线数据。"}})

    elements.append({"tag": "hr"})

    # 异动统计一行（取代旧的逐条异动长表格）。
    n_total = len(alerts or [])
    n_urgent = sum(1 for a in (alerts or []) if getattr(a, "level", "normal") == "urgent")
    if n_total:
        change_line = f"今日 {n_total} 条价格异动，紧急 {n_urgent} 条"
    else:
        change_line = "今日无价格异动。"
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": change_line}})

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


def build_urgent_card(alert, dashboard_url: str = "", flight: dict = None) -> dict:
    """Build a red-header urgent card for a single alert.

    ``flight`` (optional) is the summary low record for this route×depart_date;
    when present a compact 航班信息 line (航司/航班号/起飞时间) is appended.
    """
    price = getattr(alert, "price", None)
    prev = getattr(alert, "prev_price", None)
    target = getattr(alert, "target_price", None)

    origin, dest = _od_from_route_id(getattr(alert, "route_id", ""))
    route_label = (f"{_airport_label(origin)} → {_airport_label(dest)}"
                   if origin and dest else alert.route_id)

    detail = (
        f"**{alert.message}**\n\n"
        f"航线：{route_label}　出发日：{alert.depart_date or '-'}\n"
        f"今日最低：{_fmt(price)}　环比：{_pct_change(price, prev)}"
        f"　距目标价：{_gap_to_target(price, target)}"
    )
    fi = _flight_info_str(flight or {})
    if fi:
        detail += f"\n航班：{fi}"

    # Button area: 立即查看 (dashboard) + 购票渠道 (Google Flights deep link).
    buttons = [{
        "tag": "button",
        "text": {"tag": "plain_text", "content": "立即查看"},
        "type": "primary",
        "url": _dashboard_url(dashboard_url, alert.route_id) or "",
    }]
    buy_url = gflights_url(origin, dest, getattr(alert, "depart_date", ""))
    if buy_url:
        buttons.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": "购票渠道"},
            "type": "default",
            "url": buy_url,
        })

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
                {"tag": "action", "actions": buttons},
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
    def send_digest(self, alerts: list, stats: dict,
                    summary: dict = None, routes: list = None) -> bool:
        stats = stats or {}
        card = build_digest_card(
            alerts or [], stats, summary=summary, routes=routes,
            dashboard_url=self._dashboard_url(),
            run_date=stats.get("run_date", ""),
            run_status=stats.get("run_status", "运行正常"),
        )
        return self._send(card)

    def send_urgent(self, alert, summary: dict = None) -> bool:
        flight = _lookup_low(summary, getattr(alert, "route_id", ""),
                             getattr(alert, "depart_date", ""))
        card = build_urgent_card(alert, dashboard_url=self._dashboard_url(), flight=flight)
        return self._send(card)


def _lookup_low(summary: dict, route_id: str, depart_date: str) -> dict:
    """Fetch the summary latest-low record for a route×depart_date (or {})."""
    try:
        node = summary["routes"][route_id]["depart_dates"][depart_date]
        return node.get("latest") or {}
    except (KeyError, TypeError):
        return {}
