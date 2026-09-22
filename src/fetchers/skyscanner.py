"""Sky Scrapper (RapidAPI) adapter —— 抓 Skyscanner 公开报价的正式数据源。

背景:主源 fast_flights 抓的是 Google Flights,对某些航线(实测 YUL→NRT)看不到
携程/Skyscanner 上更便宜的多段票——Google 报 963 USD(¥6934),而 Skyscanner/携程
只要 ¥4557–4592。本 fetcher 直接查 Skyscanner(经 RapidAPI 的 sky-scrapper 封装),
把那档真实低价接进流水线。逻辑与 scripts/skyscanner_spike.py 一致(spike 已验证通)。

免费档约束(report:用户选择「免费档 + 降频/限航线」):
  * 免费档约 100 请求/月。每次查询 = searchFlights + 若干次 searchIncomplete 轮询
    ≈ 2 次调用(机场 skyId/entityId 缓存到 state,命中后不再消耗 searchAirport)。
  * 月额度守卫(state/skyscanner_usage.json,默认 cap 95)—— 超了 fetch 直接抛
    retryable=False,流水线自动降级回 fast_flights,绝不超支。
  * 隔天守卫 SKYSCANNER_EVERY_N_DAYS(默认 2)—— 非当值日直接降级,把 100/月摊开。

全部经环境变量调,workflow 里配:
    RAPIDAPI_KEY(必需)、SKYSCANNER_MONTHLY_CAP、SKYSCANNER_EVERY_N_DAYS、
    SKYSCANNER_MAX_POLLS、SKYSCANNER_MARKET / _COUNTRY / _CURRENCY。

价格口径:searchFlights 传 currency=CNY,返回价已是 CNY,无需 FX 换算(比
fast_flights 的 USD→CNY 更干净)。
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime

from ..models import FlightQuote, iso_now, SHANGHAI, today_shanghai
from .base import FetcherAdapter, FetchError, register_fetcher

log = logging.getLogger("flight_watch.fetchers.skyscanner")

HOST = "sky-scrapper.p.rapidapi.com"
BASE = f"https://{HOST}"
TIMEOUT = 30
DEFAULT_MONTHLY_CAP = 95      # < 免费档 100,留余量
DEFAULT_EVERY_N_DAYS = 2      # 隔天跑,把额度摊到全月
DEFAULT_MAX_POLLS = 2         # searchFlights 后最多轮询几次


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


@register_fetcher("skyscanner")
class SkyScannerFetcher(FetcherAdapter):
    name = "skyscanner"

    def __init__(self, state_dir: str = "state"):
        self.state_dir = state_dir
        self.usage_path = os.path.join(state_dir, "skyscanner_usage.json")
        self.airports_path = os.path.join(state_dir, "skyscanner_airports.json")
        self.market = os.environ.get("SKYSCANNER_MARKET", "zh-CN")
        self.country = os.environ.get("SKYSCANNER_COUNTRY", "CN")
        self.currency = os.environ.get("SKYSCANNER_CURRENCY", "CNY")

    # ------------------------------------------------------------- config
    @property
    def monthly_cap(self) -> int:
        return _env_int("SKYSCANNER_MONTHLY_CAP", DEFAULT_MONTHLY_CAP)

    @property
    def every_n_days(self) -> int:
        return max(1, _env_int("SKYSCANNER_EVERY_N_DAYS", DEFAULT_EVERY_N_DAYS))

    @property
    def max_polls(self) -> int:
        return max(0, _env_int("SKYSCANNER_MAX_POLLS", DEFAULT_MAX_POLLS))

    # ------------------------------------------------------------- quota
    def _month_key(self) -> str:
        return datetime.now(SHANGHAI).strftime("%Y-%m")

    def _read_json(self, path: str) -> dict:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def _used_this_month(self) -> int:
        return int(self._read_json(self.usage_path).get(self._month_key(), 0))

    def _increment_usage(self, n: int = 1) -> None:
        os.makedirs(self.state_dir, exist_ok=True)
        usage = self._read_json(self.usage_path)
        mk = self._month_key()
        usage[mk] = int(usage.get(mk, 0)) + n
        with open(self.usage_path, "w", encoding="utf-8") as f:
            json.dump(usage, f, ensure_ascii=False, indent=2)

    def remaining_quota(self) -> int:
        return max(0, self.monthly_cap - self._used_this_month())

    # ------------------------------------------------------------- http
    def _headers(self) -> dict:
        return {"x-rapidapi-key": os.environ.get("RAPIDAPI_KEY", ""),
                "x-rapidapi-host": HOST}

    def _get(self, path: str, params: dict, count: bool = True) -> dict:
        import requests  # type: ignore
        if count:
            self._increment_usage()  # 计数:调用即消耗,无论成败
        resp = requests.get(f"{BASE}{path}", headers=self._headers(),
                            params=params, timeout=TIMEOUT)
        if resp.status_code in (401, 403):
            raise FetchError(f"Sky Scrapper auth/plan error {resp.status_code}", retryable=False)
        if resp.status_code == 429:
            raise FetchError("Sky Scrapper rate/quota limited (429)", retryable=False)
        if resp.status_code != 200:
            raise FetchError(f"Sky Scrapper {path} HTTP {resp.status_code}", retryable=True)
        try:
            return resp.json()
        except ValueError:
            raise FetchError(f"Sky Scrapper {path} non-JSON response", retryable=True)

    # ------------------------------------------------- airport id cache
    def _resolve_airport(self, iata: str) -> dict:
        """IATA -> {skyId, entityId},带 state 缓存(命中不消耗额度)。"""
        code = iata.upper().strip()
        cache = self._read_json(self.airports_path)
        if code in cache and cache[code].get("skyId") and cache[code].get("entityId"):
            return cache[code]
        # 缓存未命中:查 searchAirport(会退避重试,免费档常瞬时报错)
        data = {}
        for attempt in range(1, 4):
            data = self._get("/api/v1/flights/searchAirport",
                             {"query": code, "locale": "en-US"})
            if data.get("data"):
                break
            time.sleep(2 * attempt)
        items = data.get("data") or []
        if not items:
            raise FetchError(f"searchAirport({code}) 无结果/限流", retryable=True)
        best = next((it for it in items
                     if str(it.get("skyId") or "").upper() == code), items[0])
        nav = best.get("navigation") or {}
        rel = nav.get("relevantFlightParams") or {}
        sky_id = best.get("skyId") or rel.get("skyId")
        entity_id = best.get("entityId") or rel.get("entityId")
        if not sky_id or not entity_id:
            raise FetchError(f"searchAirport({code}) 缺 skyId/entityId", retryable=True)
        cache[code] = {"skyId": sky_id, "entityId": entity_id}
        os.makedirs(self.state_dir, exist_ok=True)
        with open(self.airports_path, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        return cache[code]

    # ------------------------------------------------- search (+ poll)
    def _search(self, o: dict, d: dict, depart_date: str) -> list:
        params = {
            "originSkyId": o["skyId"], "destinationSkyId": d["skyId"],
            "originEntityId": o["entityId"], "destinationEntityId": d["entityId"],
            "date": depart_date, "cabinClass": "economy", "adults": "1",
            "sortBy": "price_high", "currency": self.currency,
            "market": self.market, "countryCode": self.country,
        }
        res = self._get("/api/v2/flights/searchFlights", params)
        ctx = (res.get("data") or {}).get("context") or {}
        status, session = ctx.get("status"), ctx.get("sessionId")
        itins = (res.get("data") or {}).get("itineraries") or []
        polls = 0
        while status == "incomplete" and session and polls < self.max_polls:
            polls += 1
            time.sleep(1.5)
            more = self._get("/api/v1/flights/searchIncomplete", {
                "sessionId": session, "currency": self.currency,
                "market": self.market, "countryCode": self.country,
            })
            mctx = (more.get("data") or {}).get("context") or {}
            mitins = (more.get("data") or {}).get("itineraries") or []
            if mitins:
                itins = mitins
            status = mctx.get("status") or status
            session = mctx.get("sessionId") or session
            if status == "complete":
                break
        return itins

    # ------------------------------------------------------------- adapter
    def available(self) -> bool:
        if not os.environ.get("RAPIDAPI_KEY"):
            return False
        try:
            import requests  # noqa: F401
        except Exception:
            return False
        return True

    def fetch(self, route, depart_date: str) -> list:
        if not os.environ.get("RAPIDAPI_KEY"):
            raise FetchError("RAPIDAPI_KEY not set", retryable=False)
        # 隔天守卫:非当值日直接降级(省额度),交给 fast_flights 兜底。
        if today_shanghai().toordinal() % self.every_n_days != 0:
            raise FetchError(
                f"skyscanner off-day (every_n_days={self.every_n_days})", retryable=False)
        if self._used_this_month() >= self.monthly_cap:
            raise FetchError(
                f"skyscanner monthly quota exhausted ({self.monthly_cap})", retryable=False)

        o = self._resolve_airport(route.origin)
        d = self._resolve_airport(route.dest)
        itins = self._search(o, d, depart_date)
        quotes = _parse_itineraries(itins, route, depart_date, self.currency)
        if not quotes:
            raise FetchError(
                f"skyscanner: 0 usable quotes for {route.id} {depart_date}", retryable=True)
        return quotes


def _hhmm(raw: str) -> str:
    """"2026-10-03T09:45:00" -> "09:45";无法解析 -> ""。"""
    s = str(raw or "")
    if "T" in s and len(s) >= 16:
        return s[11:16]
    return ""


def _parse_itineraries(itins: list, route, depart_date: str, currency: str) -> list:
    """把 Sky Scrapper 的 itineraries 解析成 FlightQuote 列表(价已是 CNY)。

    只取有价的;airline/flight_no 取第一段承运人。返回全部(存储层只落当日最低)。
    """
    fetched_at = iso_now()
    out: list = []
    for it in itins or []:
        if not isinstance(it, dict):
            continue
        price_raw = ((it.get("price") or {}).get("raw"))
        try:
            price = float(price_raw)
        except (TypeError, ValueError):
            continue
        legs = it.get("legs") or []
        first = legs[0] if legs else {}
        carriers = ((first.get("carriers") or {}).get("marketing") or [])
        airline = str((carriers[0].get("name") if carriers else "") or "").strip()
        # 航班号:leg.segments[0].flightNumber(+承运人码);缺失置 ""
        segs = first.get("segments") or []
        flight_no = ""
        if segs and isinstance(segs[0], dict):
            fn = str(segs[0].get("flightNumber") or "").strip()
            mc = ((segs[0].get("marketingCarrier") or {}).get("alternateId")
                  or (segs[0].get("marketingCarrier") or {}).get("displayCode") or "")
            flight_no = (f"{mc}{fn}".strip() if fn else "")
        stops = int(first.get("stopCount") or 0)
        depart_time = _hhmm(first.get("departure"))
        out.append(FlightQuote(
            fetched_at=fetched_at,
            route_id=route.id,
            origin=route.origin,
            dest=route.dest,
            depart_date=depart_date,
            airline=airline,
            flight_no=flight_no,
            depart_time=depart_time,
            stops=stops,
            price=int(round(price)),
            currency=currency,
            raw_price=price,
            raw_currency=currency,
            price_type="total_with_tax",
            source="skyscanner",
        ))
    return out
