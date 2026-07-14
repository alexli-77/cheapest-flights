"""Daily-digest 航班详情增强（SerpAPI google_flights → summary headline）。

思路
----
fast-flights 2.2 只给「航司名(不稳)+时刻串(不稳)+价格+stops」，拿不到航班号 / 精确
时刻 / 机型 / 中转机场 / 行李标记。本模块在 pipeline 生成 summary 之后、发通知之前
运行：对每条 **enabled 常规航线**，取它当前最便宜的 route×depart_date，用一次
SerpAPI ``fetch_flight_detail`` 拉该 route×date 的候选航班，挑价格最接近 summary 那条
最低价的航班，把结构化明细回填进 ``summary["routes"][rid]["headline"]``（新增字段，
不动既有 latest/historical_low/series，侵入最小）。飞书日报卡片 + dashboard 读这个
headline 展示航班号/时刻/中转/行李。

成本护栏
--------
* ``enrich.max_per_run``（默认 3 = 每航线 1 条）硬上限；
* 与 SerpAPI 月额度守卫（state/serpapi_usage.json，MONTHLY_CAP=240）**双限制**——
  实际预算 = min(max_per_run, 月剩余额度)；
* 无 ``SERPAPI_KEY`` / serp fetcher 不可用时整段跳过（no-op），日报回退纯
  fast-flights 展示，绝不报错、绝不拖垮管线。

headline 结构（summary["routes"][rid]["headline"]，可空）::

    {"depart_date": "YYYY-MM-DD", "price": int,   # 匹配到的 summary 最低价
     "airline": str, "flight_no": str, "airplane": str,
     "depart_time": "HH:MM", "arrive_time": "HH:MM", "stops": int,
     "layover_airports": [str, ...], "baggage_note": str,
     "overnight": bool, "source": "serpapi"}
"""

from __future__ import annotations

import logging
import time as _time
from typing import Callable, Optional

log = logging.getLogger("flight_watch.enrich")


def _cheapest_depart_date(node_map: dict) -> Optional[tuple]:
    """Return (depart_date, latest_low_price) for the cheapest usable date, or None."""
    best = None
    for dd, node in (node_map or {}).items():
        low = (node or {}).get("latest") or {}
        price = low.get("price")
        if price is None:
            continue
        if best is None or price < best[1]:
            best = (dd, price)
    return best


def _pick_nearest(candidates: list, target_price) -> Optional[dict]:
    """Pick the candidate whose price_cny is closest to ``target_price``.

    Candidates without a usable price_cny are ignored; if none have a price the
    first candidate (if any) is returned so we still surface航班号/时刻.
    """
    priced = [c for c in candidates if c.get("price_cny") is not None]
    if priced and target_price is not None:
        return min(priced, key=lambda c: abs(c["price_cny"] - target_price))
    if priced:
        return min(priced, key=lambda c: c["price_cny"])
    return candidates[0] if candidates else None


def enrich_summary(
    cfg,
    summary: dict,
    serp_fetcher=None,
    max_per_run: int = 3,
    which: str = "cheapest_per_route",
    sleep_fn: Callable[[float], None] = None,
    request_interval: float = 0.0,
) -> dict:
    """Backfill flight detail into ``summary`` for the cheapest date of each route.

    Mutates ``summary`` in place (sets per-route ``headline``) and returns a stats
    dict. Never raises. No-op (returns ``{"enriched": 0, "skipped": ...}``) when
    SerpAPI is unavailable / no key / no budget.
    """
    sleep_fn = sleep_fn or _time.sleep
    stats = {"enriched": 0, "attempts": 0, "serpapi_used": 0, "budget": 0}

    if serp_fetcher is None or not hasattr(serp_fetcher, "fetch_flight_detail"):
        stats["skipped"] = "no_serpapi_fetcher"
        return stats
    try:
        if not serp_fetcher.available():
            stats["skipped"] = "serpapi_unavailable"  # no SERPAPI_KEY
            return stats
    except Exception:
        stats["skipped"] = "serpapi_unavailable"
        return stats

    month_left = 0
    if hasattr(serp_fetcher, "remaining_quota"):
        try:
            month_left = int(serp_fetcher.remaining_quota())
        except Exception:
            month_left = 0
    budget = max(0, min(int(max_per_run), month_left))
    stats["budget"] = budget
    if budget <= 0:
        stats["skipped"] = "no_budget"
        return stats

    routes_summary = (summary or {}).get("routes") or {}
    used = 0
    for route in getattr(cfg, "routes", []) or []:
        if getattr(route, "enabled", True) is False:
            continue
        if used >= budget:
            break
        rid = getattr(route, "id", "")
        node_map = (routes_summary.get(rid) or {}).get("depart_dates") or {}
        picked = _cheapest_depart_date(node_map)
        if not picked:
            continue
        dd, target_price = picked

        stats["attempts"] += 1
        try:
            candidates = serp_fetcher.fetch_flight_detail(route.origin, route.dest, dd)
        except Exception as e:  # fetch_flight_detail is meant to swallow, be doubly safe
            log.warning("enrich %s %s raised: %s", rid, dd, e)
            candidates = None
        used += 1  # a call was attempted (fetch_flight_detail counts usage itself)
        if candidates is None:
            # None => no key / quota exhausted / network error: stop spending.
            stats["skipped"] = "detail_unavailable"
            break
        stats["serpapi_used"] += 1

        best = _pick_nearest(candidates, target_price)
        if best:
            headline = {
                "depart_date": dd,
                "price": target_price,
                "airline": best.get("airline", ""),
                "flight_no": best.get("flight_no", ""),
                "airplane": best.get("airplane", ""),
                "depart_time": best.get("depart_time", ""),
                "arrive_time": best.get("arrive_time", ""),
                "stops": int(best.get("stops") or 0),
                "layover_airports": list(best.get("layover_airports") or []),
                "baggage_note": best.get("baggage_note", ""),
                "overnight": bool(best.get("overnight")),
                "source": "serpapi",
            }
            routes_summary.setdefault(rid, {})["headline"] = headline
            stats["enriched"] += 1
            log.info("enrich %s %s -> %s %s %s", rid, dd,
                     headline["airline"], headline["flight_no"], headline["depart_time"])
        if request_interval:
            sleep_fn(request_interval)

    return stats
