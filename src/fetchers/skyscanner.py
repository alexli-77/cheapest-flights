"""Skyscanner 数据源 —— 经 omkarcloud 的 Apify Actor 抓 Skyscanner 公开报价。

背景:主源 fast_flights 抓的 Google Flights 看不到 Trip/Skyscanner 上更便宜的多段票
(实测 YUL→NRT Google ¥6934,而 Skyscanner/Trip ~¥4880)。本 fetcher 查 Skyscanner,
把那档真实低价接进流水线。

为什么用 omkarcloud/Apify(不是之前的 apiheya/RapidAPI):
  * apiheya 免费档仅 100 请求/月,撑不住每日自动。
  * omkarcloud 是 Apify Actor,计费 $0.001/条,免费 $5/月 ≈ 5000 条/月。按 2 航线×
    每天×每次取 ~30 条 ≈ 1800 条/月,**免费额度内即可每日自动跑**。

接口(Apify run-sync-get-dataset-items):
    POST https://api.apify.com/v2/acts/omkar-cloud~skyscanner-scraper/
         run-sync-get-dataset-items?token=<APIFY_TOKEN>
    body: {origin,destination,departDate,adults,cabinClass,sort,maxResults,
           market,locale,currency}
    resp: JSON 数组,每条 itinerary 形如
          {"price":{"amount":4880,"currency":"CNY"},"stop_count":2,
           "legs":[{"carriers":[{"name":"Xiamen Air","code":"MF"}],
                    "departure_at":"2026-12-11T17:55:00"}]}

环境变量(workflow 配):
    APIFY_TOKEN(必需)、SKYSCANNER_MONTHLY_CAP(按条数,默认 4500)、
    SKYSCANNER_MAX_RESULTS(默认 30)、SKYSCANNER_EVERY_N_DAYS(默认 1)、
    SKYSCANNER_MARKET / _LOCALE / _CURRENCY。

价格口径:currency=CNY,返回价已是 CNY,无需 FX 换算。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime

from ..models import FlightQuote, iso_now, SHANGHAI, today_shanghai
from .base import FetcherAdapter, FetchError, register_fetcher

log = logging.getLogger("flight_watch.fetchers.skyscanner")

ACTOR = "omkar-cloud~skyscanner-scraper"
RUN_SYNC = f"https://api.apify.com/v2/acts/{ACTOR}/run-sync-get-dataset-items"
TIMEOUT = 150                   # Actor 同步跑可能 30-90s,留足
DEFAULT_MONTHLY_CAP = 4500      # 按「条数」计,< 免费 5000/月
DEFAULT_MAX_RESULTS = 30        # 每条航线×日期取多少 itinerary
DEFAULT_EVERY_N_DAYS = 1        # 免费额度够,默认每天


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
        self.market = os.environ.get("SKYSCANNER_MARKET", "CN")
        self.locale = os.environ.get("SKYSCANNER_LOCALE", "en-US")
        self.currency = os.environ.get("SKYSCANNER_CURRENCY", "CNY")

    # ------------------------------------------------------------- config
    @property
    def monthly_cap(self) -> int:
        return _env_int("SKYSCANNER_MONTHLY_CAP", DEFAULT_MONTHLY_CAP)

    @property
    def every_n_days(self) -> int:
        return max(1, _env_int("SKYSCANNER_EVERY_N_DAYS", DEFAULT_EVERY_N_DAYS))

    @property
    def max_results(self) -> int:
        return max(1, _env_int("SKYSCANNER_MAX_RESULTS", DEFAULT_MAX_RESULTS))

    # ------------------------------------------------------------- quota
    def _month_key(self) -> str:
        return datetime.now(SHANGHAI).strftime("%Y-%m")

    def _read_usage(self) -> dict:
        if os.path.exists(self.usage_path):
            try:
                with open(self.usage_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def _used_this_month(self) -> int:
        return int(self._read_usage().get(self._month_key(), 0))

    def _increment_usage(self, n: int) -> None:
        if n <= 0:
            return
        os.makedirs(self.state_dir, exist_ok=True)
        usage = self._read_usage()
        mk = self._month_key()
        usage[mk] = int(usage.get(mk, 0)) + int(n)
        with open(self.usage_path, "w", encoding="utf-8") as f:
            json.dump(usage, f, ensure_ascii=False, indent=2)

    def remaining_quota(self) -> int:
        return max(0, self.monthly_cap - self._used_this_month())

    # ------------------------------------------------------------- adapter
    def available(self) -> bool:
        if not os.environ.get("APIFY_TOKEN"):
            return False
        try:
            import requests  # noqa: F401
        except Exception:
            return False
        return True

    def fetch(self, route, depart_date: str) -> list:
        token = os.environ.get("APIFY_TOKEN")
        if not token:
            raise FetchError("APIFY_TOKEN not set", retryable=False)
        # 隔天守卫:非当值日降级,交给 fast_flights 兜底(默认每天,通常不触发)。
        if today_shanghai().toordinal() % self.every_n_days != 0:
            raise FetchError(
                f"skyscanner off-day (every_n_days={self.every_n_days})", retryable=False)
        if self._used_this_month() >= self.monthly_cap:
            raise FetchError(
                f"skyscanner monthly cap reached ({self.monthly_cap} 条)", retryable=False)

        import requests  # type: ignore
        body = {
            "origin": route.origin, "destination": route.dest,
            "departDate": depart_date, "adults": 1, "cabinClass": "economy",
            "sort": "cheapest", "maxResults": self.max_results,
            "market": self.market, "locale": self.locale, "currency": self.currency,
        }
        try:
            resp = requests.post(RUN_SYNC, params={"token": token}, json=body, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise FetchError(f"Apify request failed: {e}", retryable=True)
        if resp.status_code in (401, 403):
            raise FetchError(f"Apify auth error {resp.status_code}", retryable=False)
        if resp.status_code == 402:  # 额度/付费相关
            raise FetchError("Apify payment/quota required (402)", retryable=False)
        if resp.status_code != 201 and resp.status_code != 200:
            raise FetchError(f"Apify run HTTP {resp.status_code}", retryable=True)
        try:
            items = resp.json()
        except ValueError:
            raise FetchError("Apify non-JSON response", retryable=True)
        if not isinstance(items, list):
            raise FetchError("Apify response not a list", retryable=True)

        self._increment_usage(len(items))  # 按返回条数计费计数
        quotes = _parse_items(items, route, depart_date, self.currency)
        if not quotes:
            raise FetchError(
                f"skyscanner: 0 usable quotes for {route.id} {depart_date}", retryable=True)
        return quotes


def _hhmm(raw: str) -> str:
    """"2026-12-11T17:55:00" -> "17:55";无法解析 -> ""。"""
    s = str(raw or "")
    if "T" in s and len(s) >= 16:
        return s[11:16]
    return ""


def _parse_items(items: list, route, depart_date: str, currency: str) -> list:
    """把 omkarcloud 的 itinerary 数组解析成 FlightQuote(价已是 CNY)。

    容错:缺价/结构异常的条目跳过;返回全部(存储层只落当日最低)。
    """
    fetched_at = iso_now()
    out: list = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        price_obj = it.get("price") or {}
        amount = price_obj.get("amount")
        try:
            price = float(amount)
        except (TypeError, ValueError):
            continue
        legs = it.get("legs") or []
        first = legs[0] if legs and isinstance(legs[0], dict) else {}
        carriers = first.get("carriers") or []
        c0 = carriers[0] if carriers and isinstance(carriers[0], dict) else {}
        airline = str(c0.get("name") or "").strip()
        flight_no = str(c0.get("code") or "").strip()  # omkarcloud 仅给承运人码,无航班号
        stops = int(it.get("stop_count") or 0)
        depart_time = _hhmm(first.get("departure_at"))
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
            raw_currency=str(price_obj.get("currency") or currency),
            price_type="total_with_tax",
            source="skyscanner",
        ))
    return out
