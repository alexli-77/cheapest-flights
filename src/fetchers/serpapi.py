"""SerpAPI google_flights adapter — limited cross-check source (report 4.2).

Not a primary source: the free tier is only ~250 queries/month, so this is used
a few times a day to (a) validate fast-flights prices, (b) enrich the daily
digest with real flight numbers / times / baggage markers, and (c) confirm
hidden-city中转机场. A quota guard enforces a hard monthly ceiling (default 240)
persisted in state/serpapi_usage.json, shared across all three consumers.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime

from ..models import FlightQuote, iso_now, SHANGHAI
from .fast_flights import parse_price, load_fx_rates, convert_to_cny, PINNED_CURRENCY, PINNED_HL
from .base import FetcherAdapter, FetchError, register_fetcher

log = logging.getLogger("flight_watch.fetchers.serpapi")

# Free tier is 250 calls/month; keep headroom for both consumers of the shared
# monthly counter (state/serpapi_usage.json): daily-digest enrichment (~90/mo,
# ≤3/run × ~30 runs) + hidden-city confirmation (~90/mo) + buffer. Both draw
# down the SAME budget, so this ceiling caps the two combined.
MONTHLY_CAP = 240  # <= free-tier 250/month with headroom (enrich ~90 + hidden ~90 + buffer)


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

    # ---------------------------------------------- daily-digest enrichment
    def fetch_flight_detail(self, origin: str, dest: str, depart_date: str):
        """Query one origin→dest×date and return structured candidate flights.

        Used by the daily-digest 增强 (src.enrich) to attach real 航班号/精确时刻/
        机型/中转机场/行李标记 to the cheapest quote of each route. One call covers
        the whole route×date and counts as ONE unit of the shared monthly SerpAPI
        budget (``state/serpapi_usage.json``).

        Returns the list produced by :func:`parse_flight_details` (possibly empty)
        on success. Returns ``None`` — never raises — when the key is missing, the
        monthly quota is exhausted, ``requests`` is absent, or the request/parse
        fails, so the digest silently falls back to fast-flights display.
        """
        api_key = os.environ.get("SERPAPI_KEY")
        if not api_key:
            return None
        if self._used_this_month() >= self.monthly_cap:
            log.info("SerpAPI monthly quota exhausted (%d), skip enrichment", self.monthly_cap)
            return None
        try:
            import requests  # type: ignore
        except Exception:
            return None

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
            log.warning("SerpAPI detail request failed for %s->%s %s: %s",
                        origin, dest, depart_date, e)
            return None

        rates = load_fx_rates(self.state_dir)
        return parse_flight_details(data, rates)

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
             "layovers": [{"id": "PVG", "name": "...", "duration": 135}, ...],
             "segments": [ {leg,airline,flight_no,from,from_time,to,to_time,
                            duration_min,airplane}, ... ],   # 逐段行程
             "itin_layovers": [{"airport": "PVG", "wait_min": 135}, ...]}
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
        dep = first.get("departure_airport") if isinstance(first, dict) else None
        depart_time = _hhmm_from_serp_time((dep or {}).get("time"))
        segments, itin_layovers = parse_segments(item)
        out.append({
            "price_cny": price_cny,
            "raw_price": raw_price,
            "raw_currency": raw_currency,
            "airline": airline,
            "flight_no": flight_no,
            "stops": stops,
            "layovers": layovers,          # {id,name,duration} — 隐藏城市中转匹配用
            "segments": segments,          # 逐段结构化航段（展示逐段行程用）
            "itin_layovers": itin_layovers,  # {airport,wait_min} — 逐段中转等待
            "depart_time": depart_time,
            "baggage_note": _baggage_note(item.get("extensions")),
        })
    return out


# SerpAPI departure/arrival ``time`` looks like "2023-10-03 15:10" (space split)
# — normalize to 24-hour "HH:MM"; unparsable -> "".
_SERP_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")


def _hhmm_from_serp_time(raw) -> str:
    """"2023-10-03 15:10" -> "15:10"; "" / None / unparsable -> ""."""
    if not raw:
        return ""
    m = _SERP_TIME_RE.search(str(raw))
    if not m:
        return ""
    hh, mm = int(m.group(1)), int(m.group(2))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return ""
    return f"{hh:02d}:{mm:02d}"


_SEG_INT_RE = re.compile(r"-?\d+")


def _seg_int(v):
    """Coerce a duration-ish value to int minutes; None/unparsable -> None."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    m = _SEG_INT_RE.search(str(v))
    return int(m.group(0)) if m else None


def parse_segments(item: dict):
    """Extract the structured per-leg 航段 + 中转等待 from one itinerary item.

    Returns ``(segments, layovers)`` where::

        segments = [{"leg": 1, "airline": "Air China", "flight_no": "CA 880",
                     "from": "PVG", "from_time": "2026-08-06 20:55",
                     "to": "PEK", "to_time": "2026-08-06 23:40",
                     "duration_min": 165, "airplane": "Boeing 777"}, ...]
        layovers = [{"airport": "PEK", "wait_min": 200}, ...]

    时刻保留完整 ``"YYYY-MM-DD HH:MM"``（跨天行程需要日期）；``duration_min`` /
    ``wait_min`` 为整数分钟（缺失置 ``None``）。容错：``item`` 非法或某段字段缺失时
    该段/该字段置空，绝不抛错。``layovers[i]`` 对应 ``segments[i]`` 与
    ``segments[i+1]`` 之间的中转等待。
    """
    item = item if isinstance(item, dict) else {}
    segments: list = []
    for leg in item.get("flights") or []:
        if not isinstance(leg, dict):
            continue
        dep = leg.get("departure_airport")
        arr = leg.get("arrival_airport")
        dep = dep if isinstance(dep, dict) else {}
        arr = arr if isinstance(arr, dict) else {}
        segments.append({
            "leg": len(segments) + 1,
            "airline": str(leg.get("airline") or "").strip(),
            "flight_no": str(leg.get("flight_number") or "").strip(),
            "from": str(dep.get("id") or "").upper().strip(),
            "from_time": str(dep.get("time") or "").strip(),
            "to": str(arr.get("id") or "").upper().strip(),
            "to_time": str(arr.get("time") or "").strip(),
            "duration_min": _seg_int(leg.get("duration")),
            "airplane": str(leg.get("airplane") or "").strip(),
        })
    layovers: list = []
    for lo in item.get("layovers") or []:
        if not isinstance(lo, dict):
            continue
        layovers.append({
            "airport": str(lo.get("id") or "").upper().strip(),
            "wait_min": _seg_int(lo.get("duration")),
        })
    return segments, layovers


_BAG_NUM_RE = re.compile(r"(\d+)")


def _baggage_note(extensions) -> str:
    """Compress a flight's ``extensions`` list into a short 中文 baggage marker.

    SerpAPI only exposes coarse flight-level ``extensions`` strings such as
    ``"1 free carry-on"`` / ``"Checked baggage for a fee"`` / ``"1 free checked
    bag"`` — there is NO precise allowance (kg/pieces beyond what the phrase
    states). We extract only phrases mentioning bag/carry/checked and map the
    common ones to compact tags joined by ``;`` (e.g. "含1件随身;托运需另购").
    Unknown baggage phrases are kept verbatim. Returns "" when nothing matches.
    """
    tags: list = []
    for ext in extensions or []:
        s = str(ext or "").strip()
        low = s.lower()
        if not any(k in low for k in ("bag", "carry", "checked", "cabin")):
            continue
        num_m = _BAG_NUM_RE.search(s)
        num = num_m.group(1) if num_m else ""
        if "carry" in low or "cabin" in low:
            tags.append(f"含{num}件随身" if (num and "free" in low)
                        else ("含随身行李" if "free" in low else "随身行李"))
        elif "fee" in low:  # "Checked baggage for a fee"
            tags.append("托运需另购")
        elif "free" in low:  # "1 free checked bag"
            tags.append(f"含{num}件托运" if num else "含免费托运")
        else:
            tags.append(s)  # unknown -> keep the raw phrase, honest fallback
    seen: set = set()
    out: list = []
    for t in tags:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return ";".join(out)


def parse_flight_details(payload: dict, rates: dict = None) -> list:
    """Parse a full SerpAPI google_flights response into candidate flights.

    Pure/side-effect-free so tests can feed it the documented sample JSON.
    ``payload`` is the whole response (``best_flights`` + ``other_flights``).
    Each output row (one per candidate itinerary) carries::

        {"price_cny": int|None, "raw_price": float, "raw_currency": str,
         "airline": str,          # first leg's marketing carrier
         "flight_no": str,        # first leg's flight number, e.g. "NH 962"
         "airplane": str,         # first leg's aircraft type (may be "")
         "depart_time": "HH:MM",  # first leg departure (24h; "" if unknown)
         "arrive_time": "HH:MM",  # last leg arrival (24h; "" if unknown)
         "stops": int,            # len(flights) - 1
         "layover_airports": [str, ...],   # IATA ids of中转机场
         "segments": [ {leg,airline,flight_no,from,from_time,to,to_time,
                        duration_min,airplane}, ... ],  # 逐段结构化航段
         "layovers": [ {"airport": str, "wait_min": int}, ... ],  # 段间中转等待
         "baggage_note": str,     # coarse marker from flight-level extensions
         "overnight": bool}
    """
    if rates is None:
        from .fast_flights import DEFAULT_FX_RATES
        rates = DEFAULT_FX_RATES
    payload = payload or {}
    raw_flights = (payload.get("best_flights") or []) + (payload.get("other_flights") or [])
    out: list = []
    for item in raw_flights:
        if not isinstance(item, dict):
            continue
        legs = item.get("flights") or []
        first = legs[0] if legs else {}
        last = legs[-1] if legs else {}
        dep = (first.get("departure_airport") if isinstance(first, dict) else None) or {}
        arr = (last.get("arrival_airport") if isinstance(last, dict) else None) or {}
        airline = str(first.get("airline") or "").strip()
        flight_no = str(first.get("flight_number") or "").strip()
        airplane = str(first.get("airplane") or "").strip()
        depart_time = _hhmm_from_serp_time(dep.get("time"))
        arrive_time = _hhmm_from_serp_time(arr.get("time"))
        stops = max(0, len(legs) - 1)
        layover_airports = [
            str(lo.get("id") or "").upper().strip()
            for lo in (item.get("layovers") or [])
            if isinstance(lo, dict) and lo.get("id")
        ]
        segments, itin_layovers = parse_segments(item)
        price = item.get("price")
        price_cny = None
        raw_price = 0.0
        raw_currency = PINNED_CURRENCY
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
            "airplane": airplane,
            "depart_time": depart_time,
            "arrive_time": arrive_time,
            "stops": stops,
            "layover_airports": layover_airports,
            "segments": segments,
            "layovers": itin_layovers,
            "baggage_note": _baggage_note(item.get("extensions")),
            "overnight": bool(item.get("overnight")),
        })
    return out
