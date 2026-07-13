#!/usr/bin/env python3
"""Aggregate per-airline detail from data/*/*.jsonl into docs/data/detail/{route}.json.

WHY THIS EXISTS (red-team / Pages-structure fix):
GitHub Pages serves the repo's ``docs/`` folder as the SITE ROOT. The raw JSONL
"database" lives in the repo's ``data/`` folder, which is OUTSIDE the site root
and therefore NOT fetchable by the dashboard at runtime. So instead of letting
the frontend lazy-load ``../data/{route}/{YYYY-MM}.jsonl`` (a 404 on Pages), we
pre-aggregate the airline breakdown the charts need into the site tree here.

Output per route (docs/data/detail/{route}.json):
    {
      "route": "sha-nrt",
      "generated_at": "<ISO8601 Shanghai>",
      "latest_fetch_date": "2026-07-10" | null,
      "sources": ["fast_flights", ...],        # distinct sources seen
      "airlines_by_depart": {
        "<depart_date>": {
          "<airline>": {"price": int, "flight_no": str, "stops": int,
                         "fetch_date": str, "currency": str, "source": str}
        }, ...
      }
    }

The per-airline "source" is the source that produced that airline's lowest
observed fare, so the dashboard's data-source filter can hide/keep bars.

Only the ALL-TIME lowest price per (depart_date, airline) is kept, so the bar
chart shows each carrier's best observed fare. Reads JSONL directly (no src
import, no PyYAML) so it can never crash the pipeline.

Run from repo root:  python scripts/gen_detail.py
Called by .github/workflows/daily.yml after `python -m src.main`.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
OUT_DIR = os.path.join(ROOT, "docs", "data", "detail")

SHANGHAI = timezone(timedelta(hours=8))


def _fetch_date_of(row: dict) -> str:
    fd = row.get("fetch_date")
    if fd:
        return fd
    fetched = row.get("fetched_at", "")
    return fetched[:10] if fetched else ""


def _iter_rows(route_dir: str):
    for name in sorted(os.listdir(route_dir)):
        if not name.endswith(".jsonl"):
            continue
        path = os.path.join(route_dir, name)
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue  # tolerate a partial/corrupt trailing line


def _config_route_ids():
    """Route ids present in config.json (single source of truth), or None.

    None means "config unreadable" -> fall back to emitting every data/ folder
    (safe default). A readable config restricts output to its routes so deleted
    routes' historical folders don't produce stale detail json.
    """
    path = os.path.join(ROOT, "config.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        ids = [r.get("id") for r in (cfg.get("routes") or []) if r.get("id")]
        return set(ids) if ids else None
    except Exception:
        return None


def build_route_detail(route_id: str, route_dir: str) -> dict:
    airlines_by_depart: dict[str, dict[str, dict]] = {}
    latest_fetch_date = ""
    sources: set[str] = set()
    for r in _iter_rows(route_dir):
        dd = r.get("depart_date")
        airline = r.get("airline") or "?"
        price = r.get("price")
        if dd is None or price is None:
            continue
        fd = _fetch_date_of(r)
        if fd > latest_fetch_date:
            latest_fetch_date = fd
        src = r.get("source", "")
        if src:
            sources.add(src)
        bucket = airlines_by_depart.setdefault(dd, {})
        prev = bucket.get(airline)
        if prev is None or price < prev["price"]:
            bucket[airline] = {
                "price": price,
                "flight_no": r.get("flight_no", ""),
                "stops": r.get("stops", 0),
                "fetch_date": fd,
                "currency": r.get("currency", "CNY"),
                "source": src,
            }
    return {
        "route": route_id,
        "generated_at": datetime.now(SHANGHAI).replace(microsecond=0).isoformat(),
        "latest_fetch_date": latest_fetch_date or None,
        "sources": sorted(sources),
        "airlines_by_depart": airlines_by_depart,
    }


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    allowed = _config_route_ids()
    count = 0
    if os.path.isdir(DATA_DIR):
        for route_id in sorted(os.listdir(DATA_DIR)):
            route_dir = os.path.join(DATA_DIR, route_id)
            if not os.path.isdir(route_dir):
                continue
            if allowed is not None and route_id not in allowed:
                continue  # deleted route: keep data/ but don't emit detail json
            detail = build_route_detail(route_id, route_dir)
            out_path = os.path.join(OUT_DIR, f"{route_id}.json")
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(detail, f, ensure_ascii=False, indent=2, sort_keys=True)
            count += 1
    print(f"detail json written: {count} routes -> {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
