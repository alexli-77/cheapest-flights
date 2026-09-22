#!/usr/bin/env python3
"""Sky Scrapper (RapidAPI) 报价 spike —— 验证「携程/Trip.com 是否真的更便宜」。

用途(一次性调研,不接入主流水线):对一条航线×日期,用 Sky Scrapper 拉
Skyscanner 的公开报价,并**按出票渠道(agent)拆开**,把 Trip.com / Ctrip 的价格
单独标出来。你再拿这个价去和:
  ① 你现在 Google Flights(fast_flights)看到的价
  ② 你手动在携程 App 里查到的价
三方对比,判断值不值得正式接一个国内源。见 README / 调研结论。

关键设计:
  * 默认用 **CN 市场 / CNY / zh-CN** 上下文发请求(currency=CNY, market=zh-CN,
    countryCode=CN)。API 在服务端跑,所以你人在海外/无国内 IP 不影响——这正是
    走托管 API 而非自己爬的意义。
  * 免费档约 100 请求/月。一次完整查询 = 2 次 searchAirport(可缓存)+ 1 次
    searchFlights + 1 次 getFlightDetails ≈ 4 次调用。spike 省着点用。
  * Schema 可能随 API 变动:脚本尽量容错,并用 --dump 打印原始 JSON 方便核对。
    它会把**所有**解析到的 agent 列出来,即使字段猜得不完全对你也能肉眼看到
    Trip.com/Ctrip 在不在、价格多少。

用法:
    export RAPIDAPI_KEY=<你的 key>            # 别硬编码,脚本只从环境读
    python3 scripts/skyscanner_spike.py YUL PEK 2026-10-03
    python3 scripts/skyscanner_spike.py YUL PEK 2026-10-03 --dump   # 附原始 JSON

RapidAPI key:去 https://rapidapi.com/apiheya/api/sky-scrapper 订阅免费档后,
在 dashboard 的 "X-RapidAPI-Key" 里拿。脚本不经手、不存储你的 key。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import requests

HOST = "sky-scrapper.p.rapidapi.com"
BASE = f"https://{HOST}"
TIMEOUT = 30

# 命中这些子串(大小写不敏感)的 agent 视为携程系,单独高亮。
CTRIP_MARKERS = ("trip.com", "ctrip", "trip com", "携程", "qunar", "去哪儿")


def _headers() -> dict:
    key = os.environ.get("RAPIDAPI_KEY")
    if not key:
        sys.exit("ERROR: 环境变量 RAPIDAPI_KEY 未设置。先 export RAPIDAPI_KEY=<key> 再跑。")
    return {"x-rapidapi-key": key, "x-rapidapi-host": HOST}


def _get(path: str, params: dict) -> dict:
    """GET 一个 endpoint,返回 JSON。对常见的额度/鉴权错误给人话提示。"""
    try:
        resp = requests.get(f"{BASE}{path}", headers=_headers(), params=params, timeout=TIMEOUT)
    except requests.RequestException as e:
        sys.exit(f"ERROR: 请求 {path} 失败:{e}")
    if resp.status_code == 401:
        sys.exit("ERROR: 401 —— RAPIDAPI_KEY 无效或未订阅该 API。")
    if resp.status_code == 403:
        sys.exit("ERROR: 403 —— 未订阅 sky-scrapper,或超出计划权限。")
    if resp.status_code == 429:
        sys.exit("ERROR: 429 —— 免费档额度用尽(约 100/月),等下月或升级付费档。")
    if resp.status_code != 200:
        sys.exit(f"ERROR: {path} -> HTTP {resp.status_code}: {resp.text[:300]}")
    try:
        return resp.json()
    except ValueError:
        sys.exit(f"ERROR: {path} 返回的不是 JSON:{resp.text[:300]}")


def resolve_airport(iata: str) -> dict:
    """IATA/城市名 -> {skyId, entityId, name}。取第一个匹配项(通常就是机场本身)。

    免费档限流较紧,searchAirport 常返回 {"status": false, "message": "Something
    went wrong"} —— 这是限流/瞬时错误,不是真的查无此机场。遇到就退避重试。
    """
    data = {}
    for attempt in range(1, 5):
        data = _get("/api/v1/flights/searchAirport", {"query": iata, "locale": "en-US"})
        items = data.get("data") or []
        if items:
            break
        # status:false 或空 -> 多半是限流,退避后重试
        wait = 3 * attempt
        print(f"  [retry] searchAirport('{iata}') 第 {attempt} 次未拿到结果"
              f"(status={data.get('status')}),等 {wait}s 重试 ...")
        time.sleep(wait)
    items = data.get("data") or []
    if not items:
        print(f"  [debug] searchAirport('{iata}') 多次重试仍失败。最后一次原始响应(截断 1500 字):")
        print("  " + json.dumps(data, ensure_ascii=False)[:1500])
        sys.exit(
            f"ERROR: searchAirport 对 {iata} 拿不到结果。\n"
            f"       若 message 是 'Something went wrong' → 免费档限流,过一会再跑,\n"
            f"       或在 RapidAPI 面板看该 API 的 rate limit(常见 free 档很低)。"
        )
    # 优先精确匹配 skyId == IATA 的机场;否则退回第一个。
    best = None
    for it in items:
        sky = str(it.get("skyId") or "").upper()
        if sky == iata.upper():
            best = it
            break
    it = best or items[0]
    nav = (it.get("navigation") or {})
    name = (nav.get("localizedName") or it.get("presentation", {}).get("title") or iata)
    sky_id = it.get("skyId") or nav.get("relevantFlightParams", {}).get("skyId")
    entity_id = it.get("entityId") or nav.get("relevantFlightParams", {}).get("entityId")
    if not sky_id or not entity_id:
        sys.exit(f"ERROR: 无法从 searchAirport 结果解析 {iata} 的 skyId/entityId。用 --dump 看原始返回。")
    return {"skyId": sky_id, "entityId": entity_id, "name": name}


def _ctx(res: dict) -> dict:
    return ((res.get("data") or {}).get("context") or {})


def _itins(res: dict) -> list:
    return ((res.get("data") or {}).get("itineraries")) or []


def search_flights(o: dict, d: dict, date: str, args) -> dict:
    """searchFlights 是轮询式:首个响应常是 status=incomplete + sessionId,
    需拿 sessionId 去 searchIncomplete 轮询,直到 complete 或结果稳定。"""
    params = {
        "originSkyId": o["skyId"],
        "destinationSkyId": d["skyId"],
        "originEntityId": o["entityId"],
        "destinationEntityId": d["entityId"],
        "date": date,
        "cabinClass": args.cabin,
        "adults": str(args.adults),
        "sortBy": "price_high",  # 我们自己挑最低价;这里只求覆盖全
        "currency": args.currency,
        "market": args.market,
        "countryCode": args.country,
    }
    res = _get("/api/v2/flights/searchFlights", params)
    status = _ctx(res).get("status")
    session = _ctx(res).get("sessionId")
    tries = 0
    while status == "incomplete" and session and tries < 6:
        tries += 1
        print(f"  [poll] 结果聚合中(incomplete),第 {tries} 次轮询 searchIncomplete ...")
        time.sleep(args.pace)
        try:
            more = _get("/api/v1/flights/searchIncomplete", {
                "sessionId": session,
                "currency": args.currency,
                "market": args.market,
                "countryCode": args.country,
            })
        except SystemExit:
            break
        # 有结果就采用最新一版;sessionId 可能刷新
        if _itins(more):
            res = more
        status = _ctx(more).get("status") or status
        session = _ctx(more).get("sessionId") or session
        if status == "complete":
            break
    return res


def _price_raw(itin: dict):
    p = (itin.get("price") or {})
    v = p.get("raw")
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _leg_summary(itin: dict) -> str:
    legs = itin.get("legs") or []
    if not legs:
        return "?"
    parts = []
    for lg in legs:
        carriers = ((lg.get("carriers") or {}).get("marketing") or [])
        names = ",".join(str(c.get("name") or "").strip() for c in carriers if c.get("name"))
        stops = lg.get("stopCount")
        dep = (lg.get("origin") or {}).get("displayCode") or "?"
        arr = (lg.get("destination") or {}).get("displayCode") or "?"
        dt = (lg.get("departure") or "")[11:16]
        parts.append(f"{dep}->{arr} {dt} {names} 中转{stops}")
    return " | ".join(parts)


def get_agents(itin_id: str, legs_ids: list, args, session: str = "") -> list:
    """对某条 itinerary 拉 agent 级报价。返回 [(agent_name, price_float_or_None), ...]。

    getFlightDetails 需要 itineraryId + legs + sessionId(缺 sessionId 常返回空)。
    不同版本参数略有差异,失败/空时打原始返回方便核对,再返回 []。
    """
    params = {
        "itineraryId": itin_id,
        "legs": json.dumps(legs_ids),
        "sessionId": session,
        "adults": str(args.adults),
        "currency": args.currency,
        "locale": "en-US",
        "market": args.market,
        "countryCode": args.country,
    }
    try:
        data = _get("/api/v1/flights/getFlightDetails", params)
    except SystemExit as e:
        print(f"    [debug] getFlightDetails 调用失败:{e}")
        return []
    itin = (data.get("data") or {}).get("itinerary") or {}
    if not itin.get("pricingOptions"):
        print("    [debug] getFlightDetails 未返回 pricingOptions。原始返回(截断 1200 字):")
        print("    " + json.dumps(data, ensure_ascii=False)[:1200])
    out = []
    for opt in itin.get("pricingOptions") or []:
        # 每个 pricingOption 通常含 agents[]:{name, price}。
        for ag in opt.get("agents") or []:
            name = str(ag.get("name") or "").strip()
            price = ag.get("price")
            try:
                price = float(price)
            except (TypeError, ValueError):
                price = None
            if name:
                out.append((name, price))
        # 有的版本把价格挂在 pricingOption.price.amount + agentIds
        if not opt.get("agents") and opt.get("price"):
            amt = (opt.get("price") or {}).get("amount")
            try:
                amt = float(amt)
            except (TypeError, ValueError):
                amt = None
            for aid in opt.get("agentIds") or []:
                out.append((str(aid), amt))
    return out


def _legs_ids_for_details(itin: dict) -> list:
    """把 searchFlights 的 legs 压成 getFlightDetails 需要的 legs 参数。"""
    ids = []
    for lg in itin.get("legs") or []:
        carriers = ((lg.get("carriers") or {}).get("marketing") or [])
        ids.append({
            "origin": (lg.get("origin") or {}).get("displayCode"),
            "destination": (lg.get("destination") or {}).get("displayCode"),
            "date": (lg.get("departure") or "")[:10],
        })
    return ids


def main() -> None:
    ap = argparse.ArgumentParser(description="Sky Scrapper 报价 spike(携程价对比)")
    ap.add_argument("origin", help="出发机场 IATA,如 YUL")
    ap.add_argument("dest", help="到达机场 IATA,如 PEK")
    ap.add_argument("date", help="出发日期 YYYY-MM-DD")
    ap.add_argument("--currency", default="CNY")
    ap.add_argument("--market", default="zh-CN", help="市场/语言,默认 zh-CN 模拟国内视角")
    ap.add_argument("--country", default="CN", help="国家码,默认 CN")
    ap.add_argument("--cabin", default="economy")
    ap.add_argument("--adults", type=int, default=1)
    ap.add_argument("--top", type=int, default=3, help="取最便宜的前 N 条 itinerary 拆 agent")
    ap.add_argument("--pace", type=float, default=2.0, help="每次调用之间的间隔秒数(免费档限流用)")
    ap.add_argument("--dump", action="store_true", help="附带打印原始 JSON")
    args = ap.parse_args()

    print(f"# 解析机场 {args.origin} / {args.dest} ...")
    o = resolve_airport(args.origin)
    time.sleep(args.pace)  # 免费档限流:两次 searchAirport 之间隔开
    d = resolve_airport(args.dest)
    print(f"  {args.origin} -> skyId={o['skyId']} entityId={o['entityId']} ({o['name']})")
    print(f"  {args.dest} -> skyId={d['skyId']} entityId={d['entityId']} ({d['name']})")

    print(f"\n# 查询报价 {args.origin}->{args.dest} {args.date} "
          f"[{args.currency}/{args.market}/{args.country}] ...")
    time.sleep(args.pace)
    res = search_flights(o, d, args.date, args)
    if args.dump:
        print("----- searchFlights 原始 JSON(截断 4000 字) -----")
        print(json.dumps(res, ensure_ascii=False)[:4000])
        print("----- END -----\n")

    itins = ((res.get("data") or {}).get("itineraries")) or []
    if not itins:
        status = (res.get("data") or {}).get("context", {}).get("status") or res.get("status")
        sys.exit(f"没有返回 itinerary(status={status})。用 --dump 看原始返回排查。")

    priced = [(it, _price_raw(it)) for it in itins]
    priced = [(it, p) for it, p in priced if p is not None]
    priced.sort(key=lambda x: x[1])

    if not priced:
        sys.exit("返回了 itinerary 但都没有可解析价格。用 --dump 排查字段。")

    session = _ctx(res).get("sessionId") or ""
    cheapest_price = priced[0][1]
    print(f"\n== 全渠道最低价:{args.currency} {cheapest_price:.0f} ==")
    print(f"   航段:{_leg_summary(priced[0][0])}")

    print(f"\n== 前 {args.top} 条最便宜 itinerary 的出票渠道(agent)拆解 ==")
    ctrip_prices = []
    for i, (it, p) in enumerate(priced[: args.top], 1):
        print(f"\n[{i}] {args.currency} {p:.0f} —— {_leg_summary(it)}")
        time.sleep(args.pace)  # 限速,别触发限流/429
        agents = get_agents(it.get("id"), _legs_ids_for_details(it), args, session=session)
        if not agents:
            print("    (未取到 agent 明细;可用 --dump 看,或 searchFlights 总价已够初判)")
            continue
        for name, ap_price in sorted(agents, key=lambda x: (x[1] is None, x[1] or 0)):
            tag = ""
            low = name.lower()
            if any(m in low for m in CTRIP_MARKERS):
                tag = "  <== 携程系"
                if ap_price is not None:
                    ctrip_prices.append(ap_price)
            price_s = f"{ap_price:.0f}" if ap_price is not None else "?"
            print(f"    {name:<28} {args.currency} {price_s}{tag}")

    print("\n== 初判 ==")
    if ctrip_prices:
        best_ctrip = min(ctrip_prices)
        gap = best_ctrip - cheapest_price
        print(f"携程系最低:{args.currency} {best_ctrip:.0f}  |  全渠道最低:{args.currency} {cheapest_price:.0f}"
              f"  |  差 {gap:+.0f}")
        print("下一步:拿「携程系最低」去比 ① 你 Google Flights 的价 ② 携程 App 手动查的价。")
        print("  若 携程系 明显低于 Google Flights 且 ≈ App 价 → 值得正式接。")
        print("  若 携程系 ≈ Google Flights 而 App 更低 → 便宜的是券后价,API 拿不到,别投入。")
    else:
        print("这条航线没解析到携程系 agent。可能该航线携程未出票,或字段变了(--dump 核对)。")
        print("多试几条你关心的航线再下结论。")


if __name__ == "__main__":
    main()
