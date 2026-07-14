"""隐藏城市（中转中国）特价票监控（延伸航线抓取 + SerpAPI 中转确认 + 启发式降级）。

思路
----
监控「从 ``origin`` (蒙特利尔 YUL) 出发、飞往一个延伸目的地 (曼谷/马尼拉/悉尼…)、
但中途在某个 **中国城市** 中转」的机票。这类 origin→onward 的联程票经常经北京/上海/
广州等地中转，而单飞 origin→中国城市 反而更贵——用户其实只想去那个中转的中国城市，
买延伸票、只飞第一段（跳程 / hidden-city）即可省钱。

数据源分工（关键技术约束）
    * fast-flights 2.2 只返回 stops 次数 + 航司名，**不返回中转机场码**。
      所以先用它对每条 onward_route×date 抓最低价的联程候选（stops≥1）。
    * 中转机场码必须靠 SerpAPI 的 google_flights engine 确认：它给每个航班挂了
      ``layovers`` 数组，元素形如 ``{"duration":135,"name":"...","id":"PVG"}``，
      ``id`` 就是中转机场三字码（已对官方文档核实）。一次 SerpAPI 调用即返回该
      route×date 的所有航班，所以按 route×date 计一次共享月额度。
    * 额度不足 / 无 SerpAPI key 时**降级为「疑似」**：只按 fast-flights 的航司名
      启发式猜中转中国枢纽（Air China→PEK、China Eastern→PVG…），标记 suspected。

成本护栏
    onward_routes × dates 可能很多：每条 onward_route 采样上限
    ``max_dates_per_onward``（默认 15）；SerpAPI 严格受 ``max_serpapi_per_run`` +
    月 90 双限制，优先确认「价格最低」的候选；直飞基线查询上限
    ``max_direct_lookups``。
"""

from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from datetime import date
from typing import Callable, Optional

from .config import Config, Route, resolve_dates
from .fetchers.base import FetchError, get_fetcher
from .models import iso_now, today_shanghai, month_of, fetch_date_of

log = logging.getLogger("flight_watch.hidden_city")

try:  # 中转城市中文名（仅用于展示，失败退回三字码，绝不影响主流程）。
    from .airports import lookup as _airport_lookup
except Exception:  # pragma: no cover - defensive
    _airport_lookup = None


# 航司名 -> 疑似中转中国枢纽（无 SerpAPI 确认时的启发式降级）。子串匹配，
# 大小写不敏感。只在命中的枢纽同时属于 config 的 chinese_hubs 时才算疑似命中。
# 这些 needle 与 config.DEFAULT_CN_CARRIERS 一致复用（都是「中国承运人」的常见写法）。
AIRLINE_HUB_HEURISTICS = [
    ("air china", "PEK"),
    ("china eastern", "PVG"),
    ("shanghai airlines", "PVG"),
    ("juneyao", "PVG"),
    ("spring airlines", "PVG"),
    ("china southern", "CAN"),
    ("xiamen", "XMN"),
    ("sichuan", "CTU"),
    ("shenzhen airlines", "SZX"),
    ("hainan", "PEK"),
    ("beijing capital", "PEK"),
]


def is_cn_carrier(airline: str, cn_carriers: list) -> bool:
    """航司名是否包含任一「中国承运人」（子串匹配、大小写不敏感）。

    fast-flights 的 ``name`` 可能是「WestJet, China Southern」这种多航司串，只要
    **包含**任一中国承运人写法即视为「疑似经中国大陆中转」，据此优先消耗 SerpAPI
    确认额度。注意：Cathay(国泰)经 HKG 中转，HKG 不在 chinese_hubs 内，默认名单不含
    Cathay，以免把额度浪费在永不命中的候选上（与 chinese_hubs 口径一致）。
    """
    a = str(airline or "").lower()
    if not a:
        return False
    for c in (cn_carriers or []):
        c = str(c or "").strip().lower()
        if c and c in a:
            return True
    return False


def heuristic_hub(airline: str, chinese_hubs: list) -> Optional[str]:
    """按航司名猜中转中国枢纽；仅返回同时在 ``chinese_hubs`` 内的枢纽码。"""
    a = str(airline or "").strip().lower()
    if not a:
        return None
    hubs = {str(h).upper() for h in (chinese_hubs or [])}
    for needle, hub in AIRLINE_HUB_HEURISTICS:
        if needle in a and hub in hubs:
            return hub
    return None


def _layover_city_cn(code: str) -> str:
    """三字码 -> 中文城市名（用于卡片展示）；查不到返回空串。"""
    code = str(code or "").upper().strip()
    if not code or _airport_lookup is None:
        return ""
    try:
        info = _airport_lookup(code) or {}
        return (info.get("city_cn") or "").strip()
    except Exception:
        return ""


def _onward_route(origin: str, dest: str, dates: dict) -> Route:
    return Route(
        id=f"hc-{origin.lower()}-{dest.lower()}",
        origin=origin,
        dest=dest,
        dates=dates,
        sources=["fast_flights"],
    )


# ------------------------------------------------------------------ storage
def _hidden_dir(data_dir: str) -> str:
    d = os.path.join(data_dir, "hidden_city")
    os.makedirs(d, exist_ok=True)
    return d


def _dedup_key(row: dict) -> tuple:
    fd = row.get("fetch_date") or fetch_date_of(row["fetched_at"])
    return (row.get("origin"), row.get("onward_dest"), row.get("depart_date"),
            row.get("layover_cn"), fd, row.get("source"))


def append_hidden_hits(data_dir: str, rows: list) -> int:
    """把命中/疑似记录按 fetch 月分片追加到 ``data/hidden_city/{YYYY-MM}.jsonl``。

    去重键：(origin, onward_dest, depart_date, layover_cn, fetch_date, source)。
    """
    if not rows:
        return 0
    by_file: dict[str, list] = defaultdict(list)
    for r in rows:
        by_file[os.path.join(_hidden_dir(data_dir), f"{month_of(r['fetched_at'])}.jsonl")].append(r)

    written = 0
    for path, items in by_file.items():
        seen: set = set()
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            seen.add(_dedup_key(json.loads(line)))
                        except Exception:
                            continue
        with open(path, "a", encoding="utf-8") as f:
            for r in items:
                k = _dedup_key(r)
                if k in seen:
                    continue
                seen.add(k)
                f.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
                written += 1
    return written


def read_recent_hits(data_dir: str, limit: int = 60) -> list:
    """读取最近的隐藏城市命中（跨所有月分片），按 fetched_at 倒序取前 ``limit`` 条。"""
    d = os.path.join(data_dir, "hidden_city")
    rows: list = []
    if not os.path.isdir(d):
        return rows
    for name in sorted(os.listdir(d)):
        if not name.endswith(".jsonl"):
            continue
        with open(os.path.join(d, name), "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        continue
    rows.sort(key=lambda r: r.get("fetched_at", ""), reverse=True)
    return rows[:limit]


def write_dashboard_json(docs_dir: str, hits: list, stats: dict) -> str:
    """生成 ``docs/data/hidden_city.json`` 供 dashboard 读取。"""
    out_dir = os.path.join(docs_dir, "data")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "hidden_city.json")
    payload = {
        "generated_at": iso_now(),
        "hits": hits,
        "stats": stats,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
    return path


# ------------------------------------------------------------------ pipeline
def _gather_candidates(hc, today: date, fast_fetcher, sleep_fn, request_interval) -> list:
    """对每条 onward_route×date 抓最低价的联程候选（stops≥1）。"""
    candidates: list = []
    for dest in hc.onward_routes:
        if not dest or dest == hc.origin:
            continue
        route = _onward_route(hc.origin, dest, hc.dates)
        dates = resolve_dates(route, today)[: hc.max_dates_per_onward]
        for dd in dates:
            try:
                quotes = fast_fetcher.fetch(route, dd)
            except FetchError as e:
                log.info("  hidden_city %s %s fast-flights failed: %s", dest, dd, e)
                if request_interval:
                    sleep_fn(request_interval)
                continue
            except Exception as e:  # never crash the pipeline
                log.warning("  hidden_city %s %s unexpected error: %s", dest, dd, e)
                continue
            stopping = [q for q in quotes if getattr(q, "stops", 0) >= 1
                        and getattr(q, "price", None) is not None]
            if stopping:
                cheapest = min(stopping, key=lambda q: q.price)
                candidates.append({
                    "onward_dest": dest,
                    "depart_date": dd,
                    "price_cny": int(cheapest.price),
                    "airline": cheapest.airline,
                    "flight_no": cheapest.flight_no,
                    "depart_time": getattr(cheapest, "depart_time", ""),
                    "stops": int(cheapest.stops),
                    "is_cn_carrier": is_cn_carrier(cheapest.airline, getattr(hc, "cn_carriers", None)),
                })
            if request_interval:
                sleep_fn(request_interval)
    # 候选优先级：第一优先「航司含中国承运人」的候选（只有它们才可能经中国大陆中转，
    # 值得优先花 SerpAPI 确认额度），第二优先其余候选；两组各按价格升序。
    candidates.sort(key=lambda c: (0 if c.get("is_cn_carrier") else 1, c["price_cny"]))
    return candidates


def _match_confirmed_hub(parsed_flights: list, chinese_hubs: list) -> Optional[dict]:
    """在 SerpAPI 解析结果里找中转落在 chinese_hubs 的最便宜航班。

    返回 {"layover_cn","price_cny","airline","flight_no","depart_time",
          "segments","layovers"} 或 None。segments/layovers 为逐段行程展示用
    （layovers 为 {"airport","wait_min"} 形式，从 SerpAPI itin_layovers 取）。
    """
    hubs = {str(h).upper() for h in (chinese_hubs or [])}
    best = None
    for fl in parsed_flights or []:
        hub = None
        for lo in fl.get("layovers") or []:
            if lo.get("id") in hubs:
                hub = lo["id"]
                break
        if not hub:
            continue
        price = fl.get("price_cny")
        if best is None or (price is not None and (best["price_cny"] is None
                                                   or price < best["price_cny"])):
            best = {
                "layover_cn": hub,
                "price_cny": price,
                "airline": fl.get("airline", ""),
                "flight_no": fl.get("flight_no", ""),
                "depart_time": fl.get("depart_time", ""),  # SerpAPI 解析已带精确起飞时刻
                "segments": list(fl.get("segments") or []),
                "layovers": list(fl.get("itin_layovers") or []),
            }
    return best


def _direct_price(fast_fetcher, origin: str, hub: str, dd: str,
                  cache: dict, budget: dict, sleep_fn, request_interval) -> Optional[int]:
    """直飞 origin→hub 最低价（用于算 saving）。带缓存 + 全局查询预算。"""
    key = (hub, dd)
    if key in cache:
        return cache[key]
    if budget["left"] <= 0:
        return None
    route = _onward_route(origin, hub, {"mode": "fixed", "fixed_dates": [dd]})
    price = None
    try:
        quotes = fast_fetcher.fetch(route, dd)
        budget["left"] -= 1
        direct = [q for q in quotes if getattr(q, "stops", 0) == 0
                  and getattr(q, "price", None) is not None]
        pool = direct or [q for q in quotes if getattr(q, "price", None) is not None]
        if pool:
            price = int(min(pool, key=lambda q: q.price).price)
    except Exception as e:
        log.info("  hidden_city direct %s->%s %s failed: %s", origin, hub, dd, e)
        budget["left"] -= 1  # a failed lookup still consumed a network attempt
    if request_interval:
        sleep_fn(request_interval)
    cache[key] = price
    return price


def run_hidden_city(
    cfg: Config,
    data_dir: str,
    docs_dir: str,
    today: Optional[date] = None,
    fast_fetcher=None,
    serp_fetcher=None,
    sleep_fn: Callable[[float], None] = None,
    request_interval: float = 0.0,
    dry_run: bool = False,
) -> dict:
    """跑一次隐藏城市监控。返回 {"hits": [...], "stats": {...}}。

    ``fast_fetcher`` / ``serp_fetcher`` 可注入（测试 / dry-run 用 mock）；缺省从
    fetcher 注册表取。任何异常都不会抛出到调用方（数据管线不能被它拖垮）。
    """
    import time as _time
    sleep_fn = sleep_fn or _time.sleep
    hc = cfg.hidden_city
    result = {"hits": [], "stats": {"enabled": bool(hc and hc.enabled)}}
    if not hc or not hc.enabled:
        log.info("hidden_city disabled, skipping")
        return result
    if not hc.origin or not hc.onward_routes or not hc.chinese_hubs:
        log.info("hidden_city misconfigured (origin/onward_routes/chinese_hubs empty), skipping")
        return result

    today = today or today_shanghai()
    fast_fetcher = fast_fetcher or get_fetcher("fast_flights")
    serp_fetcher = serp_fetcher or get_fetcher("serpapi")

    if fast_fetcher is None or not fast_fetcher.available():
        log.warning("hidden_city: fast-flights unavailable, cannot gather candidates")
        result["stats"]["error"] = "fast_flights_unavailable"
        return result

    candidates = _gather_candidates(hc, today, fast_fetcher, sleep_fn, request_interval)
    n_cn = sum(1 for c in candidates if c.get("is_cn_carrier"))
    log.info("hidden_city: %d candidates (stops>=1) gathered, %d with CN carrier "
             "(prioritized for SerpAPI confirm)", len(candidates), n_cn)

    # SerpAPI 确认预算：受 max_serpapi_per_run 与月剩余额度双限制。
    serp_ok = False
    month_left = 0
    if serp_fetcher is not None:
        try:
            serp_ok = bool(serp_fetcher.available()) and hasattr(serp_fetcher, "fetch_layovers")
            if serp_ok and hasattr(serp_fetcher, "remaining_quota"):
                month_left = int(serp_fetcher.remaining_quota())
        except Exception:
            serp_ok = False
    serp_budget = min(int(hc.max_serpapi_per_run), month_left) if serp_ok else 0

    direct_cache: dict = {}
    direct_budget = {"left": int(hc.max_direct_lookups)}
    confirmed_cache: dict = {}  # (dest,dd) -> parsed flights (or None)
    serp_used = 0
    hits: list = []
    fetched_at = iso_now()

    for cand in candidates:
        dest = cand["onward_dest"]
        dd = cand["depart_date"]
        key = (dest, dd)

        # 1) 尝试 SerpAPI 确认（一次调用覆盖整条 route×date）。候选已按「中国承运人优先、
        #    价格升序」排好序，所以额度天然先给疑似经中国大陆中转的候选。当
        #    confirm_only_suspected=True 时，进一步只对中国承运人候选花额度（没有疑似
        #    候选则本次一次都不花，把 SerpAPI 留给日报增强/其他运行，避免浪费）。
        want_confirm = serp_ok and key not in confirmed_cache and serp_used < serp_budget
        if want_confirm and getattr(hc, "confirm_only_suspected", True) \
                and not cand.get("is_cn_carrier"):
            want_confirm = False
        if want_confirm:
            try:
                parsed = serp_fetcher.fetch_layovers(hc.origin, dest, dd)
                serp_used += 1
                confirmed_cache[key] = parsed
                # 诊断日志：打印被确认候选的航司 + onward + date + 确认结果。
                seen_hubs = sorted({str(lo.get("id") or "") for fl in (parsed or [])
                                    for lo in (fl.get("layovers") or []) if lo.get("id")})
                _m = _match_confirmed_hub(parsed, hc.chinese_hubs)
                if _m:
                    log.info("  hidden_city confirm: %s %s→%s %s -> layovers=%s HIT %s",
                             cand["airline"], hc.origin, dest, dd, seen_hubs, _m["layover_cn"])
                else:
                    log.info("  hidden_city confirm: %s %s→%s %s -> layovers=%s miss",
                             cand["airline"], hc.origin, dest, dd, seen_hubs)
            except FetchError as e:
                log.info("  hidden_city SerpAPI %s %s failed: %s", dest, dd, e)
                confirmed_cache[key] = None
            except Exception as e:
                log.warning("  hidden_city SerpAPI %s %s error: %s", dest, dd, e)
                confirmed_cache[key] = None

        parsed = confirmed_cache.get(key)
        hit = None
        if parsed:  # ---- 已确认路径 ----
            match = _match_confirmed_hub(parsed, hc.chinese_hubs)
            if match:
                price = match["price_cny"] if match["price_cny"] is not None else cand["price_cny"]
                hit = {
                    "layover_cn": match["layover_cn"],
                    "price_cny": int(price),
                    "airline": match["airline"] or cand["airline"],
                    "flight_no": match["flight_no"] or cand["flight_no"],
                    "depart_time": match.get("depart_time", ""),  # SerpAPI 精确时刻优先
                    "segments": list(match.get("segments") or []),   # 逐段行程（SerpAPI）
                    "layovers": list(match.get("layovers") or []),   # 段间中转等待
                    "suspected": False,
                    "source": "serpapi",
                }
        else:  # ---- 启发式降级（疑似）----
            hub = heuristic_hub(cand["airline"], hc.chinese_hubs)
            if hub:
                hit = {
                    "layover_cn": hub,
                    "price_cny": cand["price_cny"],
                    "airline": cand["airline"],
                    "flight_no": cand["flight_no"],
                    "suspected": True,
                    "source": "fast_flights_heuristic",
                }
        if not hit:
            continue

        # 2) 直飞基线 & saving（可空）。
        direct_price = None
        saving_pct = None
        if hc.max_direct_lookups > 0 and not dry_run:
            direct_price = _direct_price(fast_fetcher, hc.origin, hit["layover_cn"], dd,
                                         direct_cache, direct_budget, sleep_fn, request_interval)
            if direct_price and direct_price > 0 and hit["price_cny"] < direct_price:
                saving_pct = round((direct_price - hit["price_cny"]) / direct_price * 100.0, 1)

        # 3) min_saving_pct 过滤：仅当已知 saving 且低于阈值时丢弃（未知则保留）。
        if hc.min_saving_pct and saving_pct is not None and saving_pct < hc.min_saving_pct:
            continue

        hit.update({
            "fetched_at": fetched_at,
            "origin": hc.origin,
            "onward_dest": dest,
            "depart_date": dd,
            "depart_time": hit.get("depart_time") or cand.get("depart_time", ""),
            "layover_city_cn": _layover_city_cn(hit["layover_cn"]),
            "direct_price_cny": direct_price,
            "saving_pct": saving_pct,
        })
        hits.append(hit)

    hits.sort(key=lambda h: (h.get("saving_pct") is None, -(h.get("saving_pct") or 0),
                             h.get("price_cny", 0)))

    written = append_hidden_hits(data_dir, hits)
    stats = {
        "enabled": True,
        "candidates": len(candidates),
        "cn_carrier_candidates": sum(1 for c in candidates if c.get("is_cn_carrier")),
        "hits": len(hits),
        "confirmed": sum(1 for h in hits if not h.get("suspected")),
        "suspected": sum(1 for h in hits if h.get("suspected")),
        "serpapi_used": serp_used,
        "serpapi_budget": serp_budget,
        "serpapi_month_remaining": max(0, month_left - serp_used),
        "written": written,
        "run_date": today.isoformat(),
    }
    log.info("hidden_city: %d hits (%d confirmed, %d suspected), SerpAPI used %d/%d",
             stats["hits"], stats["confirmed"], stats["suspected"], serp_used, serp_budget)

    # dashboard JSON 用最近命中（含本次），供 docs/index.html 读取。
    recent = read_recent_hits(data_dir, limit=60)
    if not recent:
        recent = hits
    write_dashboard_json(docs_dir, recent, stats)

    result["hits"] = hits
    result["stats"] = stats
    return result
