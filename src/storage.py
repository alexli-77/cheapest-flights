"""JSONL storage layer — "Git as database" (report section 4.4).

Quotes are appended to ``data/{route_id}/{YYYY-MM}.jsonl`` where the month is
the fetch month in Asia/Shanghai. Writes are de-duplicated on the primary key
``(route_id, depart_date, flight_no, fetch_date, source)``.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from typing import Iterable, Optional

from .models import FlightQuote, iso_now, month_of, fetch_date_of, today_shanghai


class Storage:
    def __init__(self, data_dir: str, docs_dir: Optional[str] = None):
        self.data_dir = data_dir
        # summary.json lives under docs/data/ so GitHub Pages can serve it.
        self.docs_dir = docs_dir or os.path.join(os.path.dirname(data_dir.rstrip("/")), "docs")
        os.makedirs(self.data_dir, exist_ok=True)

    # ------------------------------------------------------------------ paths
    def _file_for(self, route_id: str, month: str) -> str:
        d = os.path.join(self.data_dir, route_id)
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, f"{month}.jsonl")

    def _route_dir(self, route_id: str) -> str:
        return os.path.join(self.data_dir, route_id)

    # ------------------------------------------------------------------ read
    def read_route(self, route_id: str) -> list[dict]:
        d = self._route_dir(route_id)
        rows: list[dict] = []
        if not os.path.isdir(d):
            return rows
        for name in sorted(os.listdir(d)):
            if not name.endswith(".jsonl"):
                continue
            with open(os.path.join(d, name), "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    r = json.loads(line)
                    # fetch_date is derived (not stored, to match report schema).
                    if "fetch_date" not in r:
                        r["fetch_date"] = fetch_date_of(r["fetched_at"])
                    rows.append(r)
        return rows

    def _existing_keys(self, path: str) -> set:
        keys: set = set()
        if not os.path.exists(path):
            return keys
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                keys.add(_key_of(r))
        return keys

    # ------------------------------------------------------------------ write
    def append_quotes(self, quotes: Iterable[FlightQuote]) -> int:
        """Append quotes, skipping duplicates. Returns number actually written."""
        by_file: dict[str, list[FlightQuote]] = defaultdict(list)
        for q in quotes:
            by_file[self._file_for(q.route_id, q.month)].append(q)

        written = 0
        for path, items in by_file.items():
            seen = self._existing_keys(path)
            with open(path, "a", encoding="utf-8") as f:
                for q in items:
                    k = q.dedup_key
                    if k in seen:
                        continue
                    seen.add(k)
                    f.write(q.to_json() + "\n")
                    written += 1
        return written

    # -------------------------------------------------------------- queries
    def _rows_for(self, route_id: str, depart_date: str) -> list[dict]:
        return [r for r in self.read_route(route_id) if r.get("depart_date") == depart_date]

    def latest_low(self, route_id: str, depart_date: str) -> Optional[dict]:
        """Lowest price on the most recent fetch_date for a route+depart_date.

        Ties on price prefer the row carrying flight detail (航司/航班号/起飞时间)
        so the digest/dashboard never show a bare price when a richer row exists.
        """
        rows = self._rows_for(route_id, depart_date)
        if not rows:
            return None
        latest_fd = max(r["fetch_date"] for r in rows)
        same = [r for r in rows if r["fetch_date"] == latest_fd]
        return min(same, key=_low_sort_key)

    def historical_low(self, route_id: str, depart_date: str) -> Optional[dict]:
        """All-time lowest price row for a route+depart_date (detail-preferred on tie)."""
        rows = self._rows_for(route_id, depart_date)
        if not rows:
            return None
        return min(rows, key=_low_sort_key)

    def series(self, route_id: str, depart_date: str) -> list[dict]:
        """Per fetch_date lowest price, ascending by fetch_date."""
        rows = self._rows_for(route_id, depart_date)
        by_day: dict[str, dict] = {}
        for r in rows:
            fd = r["fetch_date"]
            if fd not in by_day or r["price"] < by_day[fd]["price"]:
                by_day[fd] = r
        out = []
        for fd in sorted(by_day):
            r = by_day[fd]
            out.append({"fetch_date": fd, "price": r["price"], "currency": r.get("currency", "CNY")})
        return out

    def depart_dates(self, route_id: str) -> list[str]:
        return sorted({r["depart_date"] for r in self.read_route(route_id)})

    def route_ids(self) -> list[str]:
        if not os.path.isdir(self.data_dir):
            return []
        return sorted(
            name for name in os.listdir(self.data_dir)
            if os.path.isdir(os.path.join(self.data_dir, name))
        )

    # -------------------------------------------------------------- summary
    def build_summary(self, route_ids: Optional[list[str]] = None, extra: Optional[dict] = None) -> dict:
        """Build and persist docs/data/summary.json for the dashboard.

        Structure (documented so the dashboard/agent M4 can rely on it):

            {
              "generated_at": "<ISO8601 Shanghai>",
              "routes": {
                "<route_id>": {
                  "depart_dates": {
                    "<YYYY-MM-DD>": {
                      "latest":         {"fetch_date","price","currency",
                                         "airline","flight_no","depart_time"} | null,
                      "historical_low": {"fetch_date","price","currency",
                                         "airline","flight_no","depart_time"} | null,
                      "series": [ {"fetch_date","price","currency"}, ... ]
                    }, ...
                  },
                  "headline": {           # OPTIONAL, added by src.enrich (SerpAPI
                                          # digest enrichment); absent when no key
                                          # / no budget. Describes the route's
                                          # cheapest depart_date in full detail.
                     "depart_date","price","airline","flight_no","airplane",
                     "depart_time","arrive_time","stops","layover_airports":[...],
                     "segments":[{leg,airline,flight_no,from,from_time,to,to_time,
                                  duration_min,airplane},...],   # 逐段行程
                     "layovers":[{"airport","wait_min"},...],    # 段间中转等待
                     "baggage_note","overnight","source"
                  }
                }, ...
              },
              "meta": { ...arbitrary extra (e.g. serpapi quota)... }
            }

        ``build_summary`` itself never fills ``headline`` (it only reads JSONL).
        The enrichment runs after this in the pipeline and re-persists via
        :meth:`persist_summary`.
        """
        ids = route_ids if route_ids is not None else self.route_ids()
        # Only surface departure dates that haven't already flown. Past dates
        # linger in the JSONL history (rolling window advances daily) and would
        # otherwise be picked as the "cheapest" — e.g. showing a 07-15 fare on
        # 07-18. History is preserved on disk; it's just excluded from the
        # live summary the cards/dashboard read.
        today = today_shanghai().isoformat()
        routes_out: dict = {}
        for rid in ids:
            dd_out: dict = {}
            for dd in self.depart_dates(rid):
                if dd < today:
                    continue
                latest = self.latest_low(rid, dd)
                hlow = self.historical_low(rid, dd)
                dd_out[dd] = {
                    "latest": _slim(latest),
                    "historical_low": _slim(hlow),
                    "series": self.series(rid, dd),
                }
            routes_out[rid] = {"depart_dates": dd_out}

        summary = {
            "generated_at": iso_now(),
            "routes": routes_out,
            "meta": extra or {},
        }
        self.persist_summary(summary)
        return summary

    def persist_summary(self, summary: dict) -> str:
        """(Re)write docs/data/summary.json from an in-memory summary dict.

        Used both by build_summary and by src.enrich (which mutates the summary
        in place with per-route ``headline`` details and needs to re-persist so
        the static dashboard sees the enriched fields).
        """
        out_dir = os.path.join(self.docs_dir, "data")
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, "summary.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2, sort_keys=True)
        return path


def _detail_score(r: dict) -> int:
    """Count of non-empty flight-detail fields (airline/flight_no/depart_time)."""
    return sum(1 for k in ("airline", "flight_no", "depart_time")
               if str(r.get(k) or "").strip())


def _low_sort_key(r: dict) -> tuple:
    """Cheapest first; among equal prices, the row with the most detail first."""
    return (r["price"], -_detail_score(r))


def _key_of(r: dict) -> tuple:
    fd = r.get("fetch_date") or fetch_date_of(r["fetched_at"])
    return (r["route_id"], r["depart_date"], r["flight_no"], fd, r["source"])


def _slim(r: Optional[dict]) -> Optional[dict]:
    if not r:
        return None
    # Carry the flight identity of the cheapest record (used by the Feishu
    # digest: 航司/航班号/起飞时间). Old JSONL rows may lack these -> "".
    return {
        "fetch_date": r["fetch_date"],
        "price": r["price"],
        "currency": r.get("currency", "CNY"),
        "airline": r.get("airline", "") or "",
        "flight_no": r.get("flight_no", "") or "",
        "depart_time": r.get("depart_time", "") or "",
    }
