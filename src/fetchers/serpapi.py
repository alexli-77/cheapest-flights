"""SerpAPI google_flights adapter — limited cross-check source (report 4.2).

Not a primary source: the free tier is only ~100-250 queries/month, so this is
used a few times a day to validate fast-flights prices. A quota guard enforces a
hard monthly ceiling (default 90) persisted in state/serpapi_usage.json.
"""

from __future__ import annotations

import json
import os
from datetime import datetime

from ..models import FlightQuote, iso_now, SHANGHAI
from .fast_flights import parse_price, load_fx_rates, convert_to_cny, PINNED_CURRENCY, PINNED_HL
from .base import FetcherAdapter, FetchError, register_fetcher

MONTHLY_CAP = 90  # <= free-tier low estimate (100/month) with headroom


@register_fetcher("serpapi")
class SerpApiFetcher(FetcherAdapter):
    name = "serpapi"

    def __init__(self, state_dir: str = "state", monthly_cap: int = MONTHLY_CAP):
        self.state_dir = state_dir
        self.monthly_cap = monthly_cap
        self.usage_path = os.path.join(state_dir, "serpapi_usage.json")

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

    def _increment_usage(self) -> None:
        os.makedirs(self.state_dir, exist_ok=True)
        usage = self._read_usage()
        mk = self._month_key()
        usage[mk] = int(usage.get(mk, 0)) + 1
        with open(self.usage_path, "w", encoding="utf-8") as f:
            json.dump(usage, f, ensure_ascii=False, indent=2)

    def remaining_quota(self) -> int:
        return max(0, self.monthly_cap - self._used_this_month())

    # ------------------------------------------------------------- adapter
    def available(self) -> bool:
        return bool(os.environ.get("SERPAPI_KEY"))

    def fetch(self, route, depart_date: str) -> list:
        api_key = os.environ.get("SERPAPI_KEY")
        if not api_key:
            raise FetchError("SERPAPI_KEY not set", retryable=False)
        if self._used_this_month() >= self.monthly_cap:
            raise FetchError(
                f"SerpAPI monthly quota exhausted ({self.monthly_cap})", retryable=False
            )
        try:
            import requests  # type: ignore
        except Exception:
            raise FetchError("requests library not installed", retryable=False)

        params = {
            "engine": "google_flights",
            "type": "2",  # one-way
            "departure_id": route.origin,
            "arrival_id": route.dest,
            "outbound_date": depart_date,
            "currency": PINNED_CURRENCY,
            "hl": PINNED_HL,
            "api_key": api_key,
        }
        try:
            resp = requests.get("https://serpapi.com/search", params=params, timeout=30)
            self._increment_usage()  # count the call regardless of parse outcome
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            raise FetchError(f"SerpAPI request failed: {e}", retryable=True)

        rates = load_fx_rates(self.state_dir)
        raw_flights = (data.get("best_flights") or []) + (data.get("other_flights") or [])
        if not raw_flights:
            raise FetchError(
                f"SerpAPI returned 0 flights for {route.id} {depart_date}", retryable=True
            )

        fetched_at = iso_now()
        quotes: list[FlightQuote] = []
        for item in raw_flights:
            price = item.get("price")
            if price in (None, ""):
                continue
            try:
                amount, cur = parse_price(price)
            except (ValueError, TypeError):
                continue
            legs = item.get("flights") or [{}]
            first = legs[0]
            airline = str(first.get("airline") or "").strip()
            flight_no = str(first.get("flight_number") or "").strip()
            stops = max(0, len(legs) - 1)
            quotes.append(FlightQuote(
                fetched_at=fetched_at,
                route_id=route.id,
                origin=route.origin,
                dest=route.dest,
                depart_date=depart_date,
                airline=airline,
                flight_no=flight_no,
                stops=stops,
                price=convert_to_cny(amount, cur, rates),
                currency=PINNED_CURRENCY,
                raw_price=float(amount),
                raw_currency=cur,
                price_type="total_with_tax",
                source=self.name,
            ))
        if not quotes:
            raise FetchError("SerpAPI: no usable quotes parsed", retryable=True)
        return quotes

    # ------------------------------------------------- hidden-city layovers
    def fetch_layovers(self, origin: str, dest: str, depart_date: str) -> list:
        """Query one origin→dest×date and return every flight's中转信息.

        Used by the隐藏城市 monitor to CONFIRM which airport a stop lands on:
        fast-flights only exposes stop *count* + airline names, never the中转
        机场码, whereas SerpAPI's google_flights engine attaches a ``layovers``
        array to每个航班 (verified against the official docs
        https://serpapi.com/google-flights-api — each element is
        ``{"duration":90,"name":"Shanghai Pudong International Airport","id":"PVG"}``
        under both ``best_flights[]`` and ``other_flights[]``; ``id`` is the IATA
        code of the中转机场).

        One call returns the whole route×date, so it counts as a SINGLE unit of
        the shared monthly SerpAPI budget (``state/serpapi_usage.json``), not one
        per candidate.

        Returns a list of dicts::

            {"price_cny": int, "raw_price": float, "raw_currency": str,
             "airline": str, "flight_no": str, "stops": int,
             "layovers": [{"id": "PVG", "name": "...", "duration": 135}, ...]}
        """
        api_key = os.environ.get("SERPAPI_KEY")
        if not api_key:
            raise FetchError("SERPAPI_KEY not set", retryable=False)
        if self._used_this_month() >= self.monthly_cap:
            raise FetchError(
                f"SerpAPI monthly quota exhausted ({self.monthly_cap})", retryable=False
            )
        try:
            import requests  # type: ignore
        except Exception:
            raise FetchError("requests library not installed", retryable=False)

        params = {
            "engine": "google_flights",
            "type": "2",  # one-way
            "departure_id": origin,
            "arrival_id": dest,
            "outbound_date": depart_date,
            "currency": PINNED_CURRENCY,
            "hl": PINNED_HL,
            "api_key": api_key,
        }
        try:
            resp = requests.get("https://serpapi.com/search", params=params, timeout=30)
            self._increment_usage()  # count the call regardless of parse outcome
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            raise FetchError(f"SerpAPI request failed: {e}", retryable=True)

        rates = load_fx_rates(self.state_dir)
        raw_flights = (data.get("best_flights") or []) + (data.get("other_flights") or [])
        return parse_layover_flights(raw_flights, rates)


def parse_layover_flights(raw_flights: list, rates: dict) -> list:
    """Parse SerpAPI ``best_flights``/``other_flights`` items into中转记录.

    Pure/side-effect-free so tests can feed it the documented sample JSON. Each
    output row carries the ``layovers`` array (with IATA ``id`` per stop) plus a
    CNY-normalized price and the first leg's carrier.
    """
    out: list = []
    for item in raw_flights or []:
        if not isinstance(item, dict):
            continue
        legs = item.get("flights") or [{}]
        first = legs[0] if legs else {}
        airline = str(first.get("airline") or "").strip()
        flight_no = str(first.get("flight_number") or "").strip()
        stops = max(0, len(legs) - 1)
        layovers = []
        for lo in item.get("layovers") or []:
            if not isinstance(lo, dict):
                continue
            code = str(lo.get("id") or "").upper().strip()
            layovers.append({
                "id": code,
                "name": str(lo.get("name") or "").strip(),
                "duration": lo.get("duration"),
            })
        price = item.get("price")
        raw_price = 0.0
        raw_currency = PINNED_CURRENCY
        price_cny = None
        if price not in (None, ""):
            try:
                amount, cur = parse_price(price)
                raw_price = float(amount)
                raw_currency = cur
                price_cny = convert_to_cny(amount, cur, rates)
            except (ValueError, TypeError):
                price_cny = None
        out.append({
            "price_cny": price_cny,
            "raw_price": raw_price,
            "raw_currency": raw_currency,
            "airline": airline,
            "flight_no": flight_no,
            "stops": stops,
            "layovers": layovers,
        })
    return out
