"""Airport IATA -> Chinese name lookup, sourced from ``docs/airports.js``.

The dashboard ships a hand-curated airport table in ``docs/airports.js`` (used by
the settings page for search). Rather than duplicate that data for the Python
side (Feishu cards want 中文城市名), we parse the same file here so there is a
single source of truth.

``docs/airports.js`` is a JavaScript IIFE, not JSON::

    var AIRPORTS = [
      { iata: "PEK", name_cn: "北京首都国际机场", city_cn: "北京", ... },
      ...
    ];

Object keys are unquoted and the file has ``//`` comments, so a plain
``json.loads`` cannot read it. We first *try* a JSON parse (in case the file is
ever migrated to ``window.AIRPORTS = [ ...valid JSON... ];``) and otherwise fall
back to a tolerant per-object regex scan. Any failure returns an empty table
rather than crashing the pipeline.
"""

from __future__ import annotations

import json
import logging
import os
import re

log = logging.getLogger("flight_watch.airports")

_TABLE: dict | None = None

# One airport object literal that contains an ``iata: "XXX"`` field. Non-greedy
# and brace-bounded so comments / commas between objects don't bleed across.
_OBJ_RE = re.compile(r"\{[^{}]*?iata\s*:\s*[\"']([A-Za-z]{3})[\"'][^{}]*?\}")


def _airports_js_path() -> str:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "docs", "airports.js")


def _field(block: str, name: str) -> str:
    m = re.search(name + r"\s*:\s*[\"']([^\"']*)[\"']", block)
    return m.group(1).strip() if m else ""


def _parse_regex(text: str) -> dict:
    table: dict = {}
    for m in _OBJ_RE.finditer(text):
        block = m.group(0)
        code = m.group(1).upper()
        table[code] = {
            "city_cn": _field(block, "city_cn"),
            "name_cn": _field(block, "name_cn"),
        }
    return table


def _parse_json(text: str) -> dict:
    """Try to read a plain-JSON ``AIRPORTS = [ ... ];`` form. Returns {} on any
    problem (the real file is JS, so this normally fails and we fall back)."""
    m = re.search(r"AIRPORTS\s*=\s*(\[.*?\])\s*;", text, re.S)
    if not m:
        return {}
    data = json.loads(m.group(1))  # raises on JS-style unquoted keys
    table: dict = {}
    for item in data:
        code = str(item.get("iata", "") or "").upper()
        if code:
            table[code] = {
                "city_cn": item.get("city_cn", "") or "",
                "name_cn": item.get("name_cn", "") or "",
            }
    return table


def _load_table() -> dict:
    try:
        with open(_airports_js_path(), "r", encoding="utf-8") as f:
            text = f.read()
    except Exception as e:  # missing file / unreadable -> empty, never crash
        log.warning("airports.js unreadable: %s", e)
        return {}
    try:
        table = _parse_json(text)
        if table:
            return table
    except Exception:
        pass  # expected for the JS-format file; fall back to regex
    try:
        return _parse_regex(text)
    except Exception as e:  # pragma: no cover - defensive
        log.warning("airports.js parse failed: %s", e)
        return {}


def _ensure_loaded() -> dict:
    global _TABLE
    if _TABLE is None:
        _TABLE = _load_table()
    return _TABLE


def lookup(iata: str) -> dict:
    """Return ``{"city_cn": str, "name_cn": str}`` for an IATA code.

    Unknown / empty codes yield empty strings (never raises).
    """
    code = str(iata or "").upper().strip()
    info = _ensure_loaded().get(code)
    if not info:
        return {"city_cn": "", "name_cn": ""}
    return {"city_cn": info.get("city_cn", ""), "name_cn": info.get("name_cn", "")}


def display_label(iata: str) -> str:
    """Human label for a route endpoint, e.g. ``"蒙特利尔(YUL)"``.

    Falls back to the bare IATA code when the city name is unknown.
    """
    code = str(iata or "").upper().strip()
    if not code:
        return code
    city = lookup(code).get("city_cn") or ""
    return f"{city}({code})" if city else code


def _reset_cache() -> None:  # test helper
    global _TABLE
    _TABLE = None
