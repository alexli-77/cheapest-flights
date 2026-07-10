"""fast-flights adapter (report data-source #1, red-team fix #2).

Design constraints:
  * fast-flights is treated as a "may-die-any-day" dependency: pinned version
    (3.0.2), lazy import (missing lib -> available()=False, never a crash),
    and a >0-results assertion (empty -> retryable FetchError so the pipeline
    retries then degrades to the next source).
  * Prices are pinned to CNY where possible; currency is recorded explicitly.
    Non-CNY prices are converted via state/fx_rates.json (default table shipped;
    TODO: daily refresh — see refresh_fx_rates()).
  * Airline whitelist/blacklist filtering per route.
"""

from __future__ import annotations

import json
import os
import re
from typing import Optional

from ..models import FlightQuote, iso_now
from .base import FetcherAdapter, FetchError, register_fetcher

# Pinned params (red-team fix #3/#4): stop currency drifting with locale.
PINNED_CURRENCY = "CNY"
PINNED_HL = "zh-CN"

# Default FX table (units of foreign currency per 1 unit -> multiply to get CNY).
# TODO: refresh daily from a free FX source and persist to state/fx_rates.json.
DEFAULT_FX_RATES = {
    "CNY": 1.0,
    "USD": 7.2,
    "EUR": 7.8,
    "GBP": 9.1,
    "JPY": 0.048,
    "KRW": 0.0053,
    "HKD": 0.92,
}

_SYMBOL_TO_CODE = {
    "US$": "USD",
    "HK$": "HKD",
    "$": "USD",
    "¥": "CNY",   # pinned locale means ¥ denotes CNY, not JPY
    "￥": "CNY",
    "€": "EUR",
    "£": "GBP",
    "₩": "KRW",
}


def parse_price(raw: str, default_currency: str = PINNED_CURRENCY) -> tuple[int, str]:
    """Parse a price string into (amount:int, currency_code:str).

    Handles currency symbols and thousands separators, e.g.::

        "¥1,234"  -> (1234, "CNY")
        "US$530"  -> (530,  "USD")
        "$1,299"  -> (1299, "USD")
        "CNY 1,234" -> (1234, "CNY")
        "1234"    -> (1234, default_currency)
    """
    if raw is None:
        raise ValueError("price is None")
    s = str(raw).strip()

    currency: Optional[str] = None
    # 1) explicit 3-letter code prefix/suffix, e.g. "CNY 1,234" or "1234 USD"
    m = re.search(r"\b([A-Z]{3})\b", s)
    if m:
        currency = m.group(1)
    else:
        # 2) currency symbols (check multi-char symbols first)
        for sym in sorted(_SYMBOL_TO_CODE, key=len, reverse=True):
            if sym in s:
                currency = _SYMBOL_TO_CODE[sym]
                break

    # Extract the numeric portion, dropping thousands separators.
    num = re.sub(r"[^0-9.]", "", s.replace(",", ""))
    if not num or num == ".":
        raise ValueError(f"no numeric value in price {raw!r}")
    amount = int(round(float(num)))
    return amount, (currency or default_currency)


def load_fx_rates(state_dir: str) -> dict:
    """Load FX rates from state/fx_rates.json, creating defaults if absent."""
    path = os.path.join(state_dir, "fx_rates.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            rates = dict(DEFAULT_FX_RATES)
            rates.update(data.get("rates", data))
            return rates
        except Exception:
            pass
    os.makedirs(state_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"_comment": "TODO: refresh daily", "rates": DEFAULT_FX_RATES}, f,
                  ensure_ascii=False, indent=2)
    return dict(DEFAULT_FX_RATES)


def convert_to_cny(amount: float, currency: str, rates: dict) -> int:
    rate = rates.get(currency.upper())
    if rate is None:
        # Unknown currency: keep the raw number rather than guessing.
        return int(round(amount))
    return int(round(amount * rate))


def refresh_fx_rates(state_dir: str) -> None:  # pragma: no cover - placeholder
    """TODO: fetch live FX rates and persist to state/fx_rates.json (daily job)."""
    raise NotImplementedError("Daily FX refresh not yet implemented")


def _airline_allowed(airline: str, airlines_cfg: dict) -> bool:
    wl = airlines_cfg.get("whitelist") or []
    bl = airlines_cfg.get("blacklist") or []
    if wl and airline not in wl:
        return False
    if airline in bl:
        return False
    return True


@register_fetcher("fast_flights")
class FastFlightsFetcher(FetcherAdapter):
    name = "fast_flights"

    def __init__(self, state_dir: str = "state"):
        self.state_dir = state_dir

    def _import(self):
        """Lazy import of the fast_flights library. Returns module or None."""
        try:
            import fast_flights  # type: ignore
            return fast_flights
        except Exception:
            return None

    def available(self) -> bool:
        return self._import() is not None

    def fetch(self, route, depart_date: str) -> list:
        ff = self._import()
        if ff is None:
            raise FetchError("fast_flights library not installed", retryable=False)

        rates = load_fx_rates(self.state_dir)
        try:
            # fast-flights 3.x API. Kept defensive since the library is fragile.
            result = ff.get_flights(
                flight_data=[
                    ff.FlightData(date=depart_date, from_airport=route.origin, to_airport=route.dest)
                ],
                trip="one-way",
                seat="economy",
                passengers=ff.Passengers(adults=1),
                fetch_mode="fallback",
                hl=PINNED_HL,
                currency=PINNED_CURRENCY,
            )
            flights = getattr(result, "flights", result) or []
        except TypeError:
            # Older/newer signature without hl/currency kwargs.
            result = ff.get_flights(
                flight_data=[
                    ff.FlightData(date=depart_date, from_airport=route.origin, to_airport=route.dest)
                ],
                trip="one-way",
                seat="economy",
                passengers=ff.Passengers(adults=1),
            )
            flights = getattr(result, "flights", result) or []
        except Exception as e:  # network / parse failure -> retryable
            raise FetchError(f"fast_flights query failed: {e}", retryable=True)

        # >0-results assertion (report: empty/骤降为0 must alert).
        if not flights:
            raise FetchError(
                f"fast_flights returned 0 results for {route.id} {depart_date}", retryable=True
            )

        fetched_at = iso_now()
        quotes: list[FlightQuote] = []
        for fobj in flights:
            raw_price = _attr(fobj, "price")
            if raw_price in (None, "", "Price unavailable"):
                continue
            try:
                amount, cur = parse_price(raw_price)
            except ValueError:
                continue
            price_cny = convert_to_cny(amount, cur, rates)
            airline = str(_attr(fobj, "name") or _attr(fobj, "airline") or "").strip()
            if not _airline_allowed(airline, route.airlines):
                continue
            flight_no = str(_attr(fobj, "flight_no") or _attr(fobj, "flight_number") or "").strip()
            stops = _attr(fobj, "stops")
            quotes.append(FlightQuote(
                fetched_at=fetched_at,
                route_id=route.id,
                origin=route.origin,
                dest=route.dest,
                depart_date=depart_date,
                airline=airline,
                flight_no=flight_no,
                stops=int(stops) if isinstance(stops, (int, float)) else 0,
                price=price_cny,
                currency=PINNED_CURRENCY,
                raw_price=float(amount),
                raw_currency=cur,
                price_type="total_with_tax",
                source=self.name,
            ))

        if not quotes:
            raise FetchError(
                f"fast_flights: all {len(flights)} results filtered/unparsable "
                f"for {route.id} {depart_date}", retryable=True,
            )
        return quotes


def _attr(obj, name):
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)
