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
import re
import time
import urllib.parse
from datetime import date as _date
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
    """"PEK" -> "北京首都(PEK)"; falls back to city then the bare code.

    Uses the airport's ``name_cn`` (机场全名) with the generic ``国际机场`` /
    ``机场`` suffix stripped, so cards show which airport it is (北京首都) rather
    than just the city (北京).
    """
    code = str(iata or "").upper().strip()
    if not code:
        return code
    name = ""
    if _airport_lookup is not None:
        try:
            info = _airport_lookup(code) or {}
            full = (info.get("name_cn") or "").strip()
            for suffix in ("国际机场", "机场"):
                if full.endswith(suffix):
                    full = full[: -len(suffix)]
                    break
            name = full or (info.get("city_cn") or "")
        except Exception:
            name = ""
    return f"{name}({code})" if name else code


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


def _headline_lines(hl: dict) -> list:
    """两行增强详情（来自 summary headline / SerpAPI）：航班行 + 行李/中转托运说明。

    行1: ``国泰 CX889 · 直飞 · 20:55 起飞``（有中转时 ``· 中转 北京首都(PEK)``）。
    行2: 直飞  -> ``🧳 行李：<baggage_note 或 见链接确认>``
         有中转 -> ``🧳 行李：<...> · 单一订单行李直挂终点，中转无需重新托运``
                  （固定政策文案，不依赖数据）。
    无 headline 时返回 []（保持原样，回退 fast-flights 展示）。
    """
    if not hl:
        return []
    carrier = " ".join(x for x in (str(hl.get("airline") or "").strip(),
                                   str(hl.get("flight_no") or "").strip()) if x).strip()
    stops = int(hl.get("stops") or 0)
    if stops <= 0:
        stop_label = "直飞"
    else:
        los = [_airport_label(a) for a in (hl.get("layover_airports") or []) if a]
        stop_label = ("中转 " + "、".join(los)) if los else f"中转{stops}次"
    dt = str(hl.get("depart_time") or "").strip()
    seg = [x for x in (carrier, stop_label) if x]
    if dt:
        seg.append(f"{dt} 起飞")
    lines = []
    line1 = " · ".join(seg)
    if line1:
        lines.append(line1)
    bag = str(hl.get("baggage_note") or "").strip() or "见链接确认"
    if stops > 0:
        lines.append(f"🧳 行李：{bag} · 单一订单行李直挂终点，中转无需重新托运")
    else:
        lines.append(f"🧳 行李：{bag}")
    return lines


# --------------------------------------------------- 逐段行程 (multi-segment)
#: 逐段行程一次最多展示的航段数（超长航程截断 + 「…」）。
MAX_ITINERARY_SEGMENTS = 4

_FULL_DT_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{2})")


def _fmt_dur(mins) -> str:
    """分钟 -> 友好「Xh Ym」显示：165->'2h45m'、60->'1h'、45->'45m'。

    None / 非数字 / <=0 -> ""（不显示）。
    """
    try:
        m = int(mins)
    except (TypeError, ValueError):
        return ""
    if m <= 0:
        return ""
    h, mm = divmod(m, 60)
    if h and mm:
        return f"{h}h{mm}m"
    if h:
        return f"{h}h"
    return f"{mm}m"


def _parse_full_dt(raw):
    """"2026-08-06 20:55" -> (date(2026,8,6), "20:55")。

    仅有 "HH:MM" 时 -> (None, "HH:MM")；无法解析 -> (None, "")。
    """
    s = str(raw or "").strip()
    if not s:
        return None, ""
    m = _FULL_DT_RE.search(s)
    if m:
        y, mo, d, hh, mm = (int(x) for x in m.groups())
        try:
            dobj = _date(y, mo, d)
        except ValueError:
            dobj = None
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return dobj, f"{hh:02d}:{mm:02d}"
        return dobj, ""
    hm = re.search(r"(\d{1,2}):(\d{2})", s)
    if hm:
        hh, mm = int(hm.group(1)), int(hm.group(2))
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return None, f"{hh:02d}:{mm:02d}"
    return None, ""


def _flight_no_compact(no) -> str:
    """"CA 880" -> "CA880"（去掉内部空格，卡片更紧凑）。"""
    return "".join(str(no or "").split())


def _layover_for(layovers: list, idx: int, airport: str):
    """段 ``idx`` 之后的中转等待记录：优先按位置，退回按机场码匹配。"""
    if 0 <= idx < len(layovers) and isinstance(layovers[idx], dict):
        return layovers[idx]
    code = str(airport or "").upper().strip()
    if code:
        for lo in layovers:
            if isinstance(lo, dict) and str(lo.get("airport") or "").upper() == code:
                return lo
    return None


#: 段号 keycap emoji（1..9）；第 10 段起退回文字「第N段」。
_SEG_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣"]

#: 起降行缩进。飞书 lark_md 会把行首的半角空格吞掉/折叠，导致层次消失；改用两个
#: 全角空格（U+3000 表意空格），它不会被裁剪，能在飞书里渲染出稳定的缩进层次。
_ITIN_INDENT = "　　"


def _seg_marker(i: int) -> str:
    """0-based 段序 -> 段号：0->「1️⃣」… 8->「9️⃣」，超过 9 段退回「第N段」文字。"""
    return _SEG_EMOJI[i] if i < len(_SEG_EMOJI) else f"第{i + 1}段"


def format_itinerary(segments, layovers=None, max_segs: int = MAX_ITINERARY_SEGMENTS) -> str:
    """把逐段航段 + 段间中转等待渲染成 lark_md 多行文本（按用户模板，可空）。

    每段 3 行（段号行 + 起飞行 + 到达行），段间插入中转等待行::

        1️⃣ WS817 · 飞行1h30m
        　　17:45 蒙特利尔特鲁多(YUL)
        　　19:15 多伦多皮尔逊(YYZ)
        ⏱ 中转 多伦多皮尔逊(YYZ) 等待 6h35m
        2️⃣ PR119 · 飞行16h15m
        　　07-16 01:50 多伦多皮尔逊(YYZ)
        　　06:05+1 马尼拉尼诺·阿基诺(MNL)

    规则要点：

    * 段号用 keycap emoji（超过 9 段用「第N段」）；``flight_no`` 去空格；
      飞行时长缺失则省略「· 飞行…」。
    * 起降行用全角空格缩进（:data:`_ITIN_INDENT`）。
    * 出发行日期前缀（``MM-DD``）：仅当该段出发日 != 「整个行程上一次打印过的日期」
      才显示（``last_printed_date`` 初始化为首段出发日 -> 首段出发行不带日期）。
      只有出发行会更新 ``last_printed_date``。
    * 到达行用 ``+N`` 跨日标记（``N = 到达日 - 本段出发日``，>0 才显示），不带 ``MM-DD``。

    机场用中文全名（airports.py）。超过 ``max_segs`` 段则截断并加「… 还有 N 段」。
    字段缺失容错，绝不抛错；无有效航段返回 ""。
    """
    segs = [s for s in (segments or []) if isinstance(s, dict)]
    if not segs:
        return ""
    layovers = [lo for lo in (layovers or []) if isinstance(lo, dict)]
    n = len(segs)
    shown = segs[:max_segs]
    lines: list = []

    # 「整个行程上一次打印过的日期」：初始化为首段出发日，使首段出发行不带日期前缀。
    first_dep_dt, _ = _parse_full_dt(shown[0].get("from_time"))
    last_printed_date = first_dep_dt

    for i, seg in enumerate(shown):
        fno = _flight_no_compact(seg.get("flight_no"))
        dep_dt, dep_hhmm = _parse_full_dt(seg.get("from_time"))
        arr_dt, arr_hhmm = _parse_full_dt(seg.get("to_time"))
        from_label = _airport_label(seg.get("from"))
        to_label = _airport_label(seg.get("to"))

        # 段号行：段号 + 航班号 + 飞行时长。
        head = _seg_marker(i)
        if fno:
            head += f" {fno}"
        dur = _fmt_dur(seg.get("duration_min"))
        if dur:
            head += f" · 飞行{dur}"
        lines.append(head)

        # 出发行：日期前缀仅当出发日 != 上一次打印过的日期。
        dep_prefix = ""
        if dep_dt is not None and (last_printed_date is None or dep_dt != last_printed_date):
            dep_prefix = f"{dep_dt.month:02d}-{dep_dt.day:02d} "
        if dep_dt is not None:
            last_printed_date = dep_dt
        dep_time = f"{dep_prefix}{dep_hhmm}".strip()
        dep_body = " ".join(x for x in (dep_time, from_label) if x)
        if dep_body:
            lines.append(f"{_ITIN_INDENT}{dep_body}")

        # 到达行：+N 跨日标记（相对本段出发日），不带 MM-DD 前缀。
        arr_disp = arr_hhmm
        if arr_dt is not None and dep_dt is not None and arr_hhmm:
            day_gap = (arr_dt - dep_dt).days
            if day_gap > 0:
                arr_disp = f"{arr_hhmm}+{day_gap}"
        arr_body = " ".join(x for x in (arr_disp, to_label) if x)
        if arr_body:
            lines.append(f"{_ITIN_INDENT}{arr_body}")

        if i < len(shown) - 1:  # 段间中转等待
            lo = _layover_for(layovers, i, seg.get("to"))
            if lo:
                lo_line = f"⏱ 中转 {_airport_label(lo.get('airport'))}"
                wait = _fmt_dur(lo.get("wait_min"))
                if wait:
                    lo_line += f" 等待 {wait}"
                lines.append(lo_line)

    if n > max_segs:
        lines.append(f"… 还有 {n - max_segs} 段")
    return "\n".join(lines)


def _baggage_line(hl: dict) -> str:
    """行李说明单行（与 _headline_lines 第2行一致；逐段展示时附在行程后）。"""
    bag = str((hl or {}).get("baggage_note") or "").strip() or "见链接确认"
    if int((hl or {}).get("stops") or 0) > 0:
        return f"🧳 行李：{bag} · 单一订单行李直挂终点，中转无需重新托运"
    return f"🧳 行李：{bag}"


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


def _route_block(route, node_map: dict, headline: dict = None,
                 show_segments: bool = True) -> dict:
    """Build one compact digest block (a lark_md div) for a single route.

    ``node_map`` = summary["routes"][id]["depart_dates"] (may be empty).
    ``headline`` = summary["routes"][id]["headline"] (SerpAPI 增强详情, optional):
    when it matches the depart_date shown as this route's最低价, the逐段行程 is
    expanded (航班号/各段时刻/起降机场 + 段间中转等待, via :func:`format_itinerary`)
    followed by a 行李/中转托运说明 line. When ``show_segments`` is False or the
    headline carries no ``segments``, it falls back to the single-line
    :func:`_headline_lines` summary.
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
        # 是否会在下方展开逐段行程（headline 命中本块最低价日期且带 segments）。
        itin_segs = (headline or {}).get("segments") or []
        will_expand = bool(
            show_segments and headline
            and headline.get("depart_date") == best_dd and itin_segs)
        if will_expand:
            # 展开逐段：第二行标注「各段为当地时间」+ 段数（直飞/共N段行程）。
            # 注：SerpAPI 各段 departure/arrival time 均为各自机场当地时间，笼统写
            # 某城市时间会误导（会让人以为后段也是出发地时区）。故用固定文案。
            parts.append("各段为当地时间")
            n_seg = len(itin_segs)
            parts.append("直飞" if n_seg == 1 else f"共{n_seg}段行程")
        else:
            # 不展开时（show_segments 关或无 segments）回退航司/时刻单行摘要。
            fi = _flight_info_str(low)
            if fi:
                parts.append(fi)
        pct = _digest_pct(node)
        if pct:
            parts.append(pct)
        body = " · ".join(parts)
        link_dd = best_dd

    content = f"**{header}**\n{body}"

    # 增强详情：仅当 headline 对应的 depart_date 正是本块展示的最低价日期时展示。
    if headline and link_dd and headline.get("depart_date") == link_dd:
        segs = headline.get("segments") or []
        if show_segments and segs:
            itin = format_itinerary(segs, headline.get("layovers") or [])
            if itin:
                content += f"\n{itin}"
            content += f"\n{_baggage_line(headline)}"
        else:
            for extra in _headline_lines(headline):
                content += f"\n{extra}"

    if link_dd:
        url = gflights_url(origin, dest, link_dd)
        if url:
            content += f"\n[→ 查看购票渠道]({url})"

    return {"tag": "div", "text": {"tag": "lark_md", "content": content}}


def build_digest_card(alerts: list, stats: dict, summary: dict = None,
                      routes: list = None, dashboard_url: str = "",
                      run_date: str = "", run_status: str = "运行正常",
                      show_segments: bool = True) -> dict:
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
        route_node = routes_summary.get(getattr(route, "id", "")) or {}
        node_map = route_node.get("depart_dates", {})
        headline = route_node.get("headline")
        elements.append(_route_block(route, node_map, headline=headline,
                                     show_segments=show_segments))
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


# ------------------------------------------------ hidden-city (隐藏城市) card
HIDDEN_CITY_RISK = (
    "隐藏城市票：仅飞第一段、勿托运行李、勿用于往返程、违反航司条款，风险自负"
)
# 行李政策说明（固定文案）：跳程票的行李会被直挂到票面终点，而非你实际下机的中转站，
# 所以只能带手提行李随身下机。
HIDDEN_CITY_BAGGAGE = (
    "🧳 行李：隐藏城市票行李会直挂票面终点（非你下机的中转站），只能带手提行李随身下机"
)


def _city_code_label(code: str) -> str:
    """"BKK" -> "曼谷BKK"（城市中文名 + 三字码）；查不到城市名时退回三字码。"""
    code = str(code or "").upper().strip()
    if not code:
        return code
    city = ""
    if _airport_lookup is not None:
        try:
            city = (_airport_lookup(code) or {}).get("city_cn") or ""
        except Exception:
            city = ""
    return f"{city}{code}" if city else code


def _hidden_hit_block(hit: dict, show_segments: bool = True, expand: bool = True) -> dict:
    """一条隐藏城市命中的卡片块。

    ``expand`` 且 ``show_segments`` 且命中带 ``segments`` 时，展开逐段行程
    （各段时刻/航班号/起降机场 + 段间中转等待，via :func:`format_itinerary`）；
    否则退回单行 航班摘要（航司/航班号/起飞时间）。行李/风险提示始终保留。
    """
    origin = str(hit.get("origin") or "").upper()
    onward = str(hit.get("onward_dest") or "").upper()
    layover = str(hit.get("layover_cn") or "").upper()
    layover_city = (hit.get("layover_city_cn") or "").strip()
    price = hit.get("price_cny")
    dd = hit.get("depart_date") or ""

    layover_label = f"{layover_city}({layover})" if layover_city else layover
    head = (f"🧳 {_city_code_label(origin)} → {_city_code_label(onward)}"
            f" 机票 {_price_str({'price': price, 'currency': 'CNY'})}"
            f"，中转 {layover_label}")
    if hit.get("suspected"):
        head += "  ⚠️未确认中转站"
    lines = [f"**{head}**"]

    saving = hit.get("saving_pct")
    direct = hit.get("direct_price_cny")
    if saving is not None and direct:
        city_only = layover_city or layover
        lines.append(f"比直飞{city_only} ¥{_fmt(direct)} 省 {_fmt(saving)}%")

    segs = hit.get("segments") or []
    itin = format_itinerary(segs, hit.get("layovers") or []) if (show_segments and expand) else ""
    if itin:
        if dd:
            lines.append(f"出发 {dd}")
        lines.append(itin)
    else:
        fi = _flight_info_str(hit)
        meta = []
        if dd:
            meta.append(f"出发 {dd}")
        if fi:
            meta.append(fi)
        if meta:
            lines.append(" · ".join(meta))

    lines.append(HIDDEN_CITY_BAGGAGE)

    url = gflights_url(origin, onward, dd)
    if url:
        lines.append(f"[→ Google Flights 全程]({url})")
    lines.append(f"<font color='grey'>{HIDDEN_CITY_RISK}</font>")

    return {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}


#: 隐藏城市卡片一次最多「详细展开逐段行程」的命中数，其余仅显示单行摘要。
HIDDEN_CITY_DETAIL_LIMIT = 5


def build_hidden_city_card(hits: list, dashboard_url: str = "", limit: int = 8,
                           show_segments: bool = True) -> dict:
    """隐藏城市特价专属卡片：每条命中一块，最多 ``limit`` 条，其余折叠成一行。

    前 ``HIDDEN_CITY_DETAIL_LIMIT`` 条详细展开逐段行程（有 segments 时），其余
    命中块仅显示单行航班摘要，避免卡片过长。``show_segments=False`` 时全部回退
    单行摘要。
    """
    hits = list(hits or [])
    elements: list = []
    if not hits:
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                        "content": "本次未发现经中国中转的隐藏城市特价。"}})
    else:
        n_conf = sum(1 for h in hits if not h.get("suspected"))
        n_susp = len(hits) - n_conf
        summary = f"共 {len(hits)} 条（确认 {n_conf} · 疑似 {n_susp}）"
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": summary}})
        elements.append({"tag": "hr"})
        for i, h in enumerate(hits[:limit]):
            elements.append(_hidden_hit_block(
                h, show_segments=show_segments, expand=(i < HIDDEN_CITY_DETAIL_LIMIT)))
        rest = len(hits) - limit
        if rest > 0:
            elements.append({"tag": "div", "text": {"tag": "lark_md",
                            "content": f"…还有 {rest} 条，见 dashboard"}})
    elements.append({"tag": "hr"})
    elements.append(_button("查看隐藏城市 Dashboard", _dashboard_url(dashboard_url)))
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "🧳 隐藏城市特价"},
                "template": "turquoise",
            },
            "elements": elements,
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

    def _show_segments(self) -> bool:
        """notifiers.feishu.show_segments 开关（默认 True = 展开逐段行程）。"""
        return bool(self.cfg.get("show_segments", True))

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
            show_segments=self._show_segments(),
        )
        return self._send(card)

    def send_urgent(self, alert, summary: dict = None) -> bool:
        flight = _lookup_low(summary, getattr(alert, "route_id", ""),
                             getattr(alert, "depart_date", ""))
        card = build_urgent_card(alert, dashboard_url=self._dashboard_url(), flight=flight)
        return self._send(card)

    def send_hidden_city(self, hits: list) -> bool:
        """隐藏城市特价单独一张卡片。无命中时不发（返回 False）。"""
        if not hits:
            return False
        card = build_hidden_city_card(hits, dashboard_url=self._dashboard_url(),
                                      show_segments=self._show_segments())
        return self._send(card)


def _lookup_low(summary: dict, route_id: str, depart_date: str) -> dict:
    """Fetch the summary latest-low record for a route×depart_date (or {})."""
    try:
        node = summary["routes"][route_id]["depart_dates"][depart_date]
        return node.get("latest") or {}
    except (KeyError, TypeError):
        return {}
