#!/usr/bin/env python3
"""Generate docs/data/routes_meta.json from config.yaml (fallback config.json).

Display metadata for the static dashboard. The frontend reads this file to know
each route's human labels, target price and currency WITHOUT ever touching the
Python config loader. Kept dependency-optional: uses PyYAML if present, else the
config.json mirror shipped alongside config.yaml.

Run from repo root:  python scripts/gen_routes_meta.py
Called by .github/workflows/daily.yml after `python -m src.main`.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "data", "routes_meta.json")
AIRPORTS_JS = os.path.join(ROOT, "docs", "airports.js")

SHANGHAI = timezone(timedelta(hours=8))

# Tolerant per-object scan of docs/airports.js (a JS IIFE, not JSON). Mirrors
# src/airports.py but kept dependency-free/standalone so this script never
# imports the src package (same policy as gen_detail.py).
_AIRPORT_OBJ_RE = re.compile(r"\{[^{}]*?iata\s*:\s*[\"']([A-Za-z]{3})[\"'][^{}]*?\}")


def _airport_field(block: str, name: str) -> str:
    m = re.search(name + r"\s*:\s*[\"']([^\"']*)[\"']", block)
    return m.group(1).strip() if m else ""


def _load_airports() -> dict:
    table: dict = {}
    try:
        with open(AIRPORTS_JS, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return table
    for m in _AIRPORT_OBJ_RE.finditer(text):
        block = m.group(0)
        code = m.group(1).upper()
        table[code] = {
            "city_cn": _airport_field(block, "city_cn"),
            "name_cn": _airport_field(block, "name_cn"),
        }
    return table


def _load_config() -> dict:
    """Load config.yaml if PyYAML is available, else the config.json mirror."""
    yaml_path = os.path.join(ROOT, "config.yaml")
    json_path = os.path.join(ROOT, "config.json")
    try:
        import yaml  # type: ignore

        if os.path.exists(yaml_path):
            with open(yaml_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f)
    except Exception:
        pass
    with open(json_path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_meta(cfg: dict) -> dict:
    defaults = cfg.get("defaults", {}) or {}
    default_currency = defaults.get("currency", "CNY")
    airports = _load_airports()
    routes_out = []
    for r in cfg.get("routes", []) or []:
        frm = str(r.get("from") or "").upper()
        to = str(r.get("to") or "").upper()
        fa = airports.get(frm, {})
        ta = airports.get(to, {})
        routes_out.append(
            {
                "route_id": r.get("id"),
                "from": r.get("from"),
                "to": r.get("to"),
                # 中文名（供 dashboard 展示；查不到留空字符串，前端回退三字码）。
                "from_city_cn": fa.get("city_cn", ""),
                "from_name_cn": fa.get("name_cn", ""),
                "to_city_cn": ta.get("city_cn", ""),
                "to_name_cn": ta.get("name_cn", ""),
                "target_price": r.get("target_price"),
                "currency": r.get("currency", default_currency),
                "drop_alert_pct": r.get("drop_alert_pct"),
                "enabled": bool(r.get("enabled", True)),
                "sources": r.get("sources", []),
            }
        )
    return {
        "generated_at": datetime.now(SHANGHAI).replace(microsecond=0).isoformat(),
        "timezone": cfg.get("timezone", "Asia/Shanghai"),
        "routes": routes_out,
    }


def main() -> int:
    cfg = _load_config()
    meta = build_meta(cfg)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2, sort_keys=True)
    print(f"routes_meta.json written: {len(meta['routes'])} routes -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
