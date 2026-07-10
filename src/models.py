"""Core data model for flight-watch.

All timestamps and date sharding use the Asia/Shanghai timezone (red-team
mandated fix #4). ``fetched_at`` is stored as a timezone-aware ISO-8601 string
in Shanghai local time (``+08:00``); ``fetch_date`` (the calendar day a quote
was collected) and the monthly JSONL shard are both derived in this timezone.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from datetime import datetime, date
from zoneinfo import ZoneInfo

# Single source of truth for the project timezone.
SHANGHAI = ZoneInfo("Asia/Shanghai")


def now_shanghai() -> datetime:
    """Current time as a tz-aware datetime in Asia/Shanghai."""
    return datetime.now(SHANGHAI)


def today_shanghai() -> date:
    """Today's calendar date in Asia/Shanghai."""
    return now_shanghai().date()


def iso_now() -> str:
    """Current Shanghai time as an ISO-8601 string with offset (seconds precision)."""
    return now_shanghai().replace(microsecond=0).isoformat()


def fetch_date_of(fetched_at: str) -> str:
    """Return the calendar day (YYYY-MM-DD, Shanghai tz) for an ISO timestamp.

    Naive timestamps are assumed to already be Shanghai local time.
    """
    dt = datetime.fromisoformat(fetched_at)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SHANGHAI)
    return dt.astimezone(SHANGHAI).date().isoformat()


def month_of(fetched_at: str) -> str:
    """Return the monthly shard key (YYYY-MM, Shanghai tz) for an ISO timestamp."""
    return fetch_date_of(fetched_at)[:7]


@dataclass
class FlightQuote:
    """A single flight price observation.

    Fields (schema per report section 4.4, with red-team fixes #3):
      fetched_at      ISO-8601 timestamp in Asia/Shanghai (with +08:00 offset)
      route_id        config route id, e.g. "sha-nrt"
      origin          IATA origin, e.g. "SHA"
      dest            IATA destination, e.g. "NRT"
      depart_date     departure calendar date "YYYY-MM-DD"
      airline         airline IATA code, e.g. "MU"
      flight_no       flight number, e.g. "MU523"
      stops           number of stops (0 = direct)
      price           normalized price as int in ``currency`` (default CNY)
      currency        normalized currency code, e.g. "CNY"
      raw_price       original numeric price as returned by the source
      raw_currency    original currency code as returned by the source
      price_type      price口径, e.g. "total_with_tax" | "base" | "unknown"
      source          fetcher name, e.g. "fast_flights" | "serpapi"
      is_lowest_of_day  True if this is the lowest price seen for the
                        (route_id, depart_date) on its fetch_date
    """

    fetched_at: str
    route_id: str
    origin: str
    dest: str
    depart_date: str
    airline: str
    flight_no: str
    stops: int
    price: int
    currency: str = "CNY"
    raw_price: float = 0.0
    raw_currency: str = "CNY"
    price_type: str = "total_with_tax"
    source: str = "fast_flights"
    is_lowest_of_day: bool = False

    @property
    def fetch_date(self) -> str:
        """The calendar day this quote was collected (Shanghai tz)."""
        return fetch_date_of(self.fetched_at)

    @property
    def month(self) -> str:
        """Monthly shard key (YYYY-MM, Shanghai tz) for JSONL storage."""
        return month_of(self.fetched_at)

    @property
    def dedup_key(self) -> tuple:
        """Deduplication primary key (report section 4.4)."""
        return (self.route_id, self.depart_date, self.flight_no, self.fetch_date, self.source)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_dict(cls, d: dict) -> "FlightQuote":
        allowed = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in allowed})
