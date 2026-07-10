"""Configuration loading and date resolution for flight-watch.

The canonical config is ``config.yaml`` (report section 6.3). Parsing prefers
PyYAML; if PyYAML is unavailable (graceful degradation, hard constraint) it
falls back to a sibling ``config.json`` with identical structure. Keep the two
files in sync — ``config.json`` is the no-dependency mirror of ``config.yaml``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Optional

try:  # PyYAML is preferred but optional (sandbox / offline degradation).
    import yaml  # type: ignore
    _HAS_YAML = True
except Exception:  # pragma: no cover - only hit when PyYAML missing
    yaml = None  # type: ignore
    _HAS_YAML = False


@dataclass
class Route:
    id: str
    origin: str
    dest: str
    dates: dict
    airlines: dict = field(default_factory=lambda: {"whitelist": [], "blacklist": []})
    target_price: Optional[float] = None
    drop_alert_pct: Optional[float] = None
    sources: list = field(default_factory=lambda: ["fast_flights"])
    enabled: bool = True

    @classmethod
    def from_dict(cls, d: dict) -> "Route":
        airlines = d.get("airlines") or {}
        return cls(
            id=d["id"],
            origin=d.get("from") or d.get("origin"),
            dest=d.get("to") or d.get("dest"),
            dates=d.get("dates") or {},
            airlines={
                "whitelist": list(airlines.get("whitelist", []) or []),
                "blacklist": list(airlines.get("blacklist", []) or []),
            },
            target_price=d.get("target_price"),
            drop_alert_pct=d.get("drop_alert_pct"),
            sources=list(d.get("sources", ["fast_flights"]) or ["fast_flights"]),
            enabled=bool(d.get("enabled", True)),
        )


@dataclass
class Config:
    timezone: str
    defaults: dict
    routes: list
    cross_check: dict
    alerts: dict
    notifiers: dict
    dashboard: dict
    raw: dict

    def route_by_id(self, route_id: str) -> Optional[Route]:
        for r in self.routes:
            if r.id == route_id:
                return r
        return None


def _load_raw(path: str) -> dict:
    """Load the raw config dict, preferring YAML then JSON fallback."""
    if path.endswith((".yaml", ".yml")) and _HAS_YAML and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    # JSON fallback: either an explicit .json path, or the .json sibling of a
    # .yaml path when PyYAML is not installed.
    if path.endswith(".json") and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    sibling = os.path.splitext(path)[0] + ".json"
    if os.path.exists(sibling):
        with open(sibling, "r", encoding="utf-8") as f:
            return json.load(f)
    if _HAS_YAML and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    raise FileNotFoundError(
        f"No loadable config found for {path!r} (need PyYAML for .yaml or a .json sibling)"
    )


def load_config(path: str) -> Config:
    raw = _load_raw(path)
    routes = [Route.from_dict(r) for r in raw.get("routes", [])]
    return Config(
        timezone=raw.get("timezone", "Asia/Shanghai"),
        defaults=raw.get("defaults", {}) or {},
        routes=routes,
        cross_check=raw.get("cross_check", {}) or {},
        alerts=raw.get("alerts", {}) or {},
        notifiers=raw.get("notifiers", {}) or {},
        dashboard=raw.get("dashboard", {}) or {},
        raw=raw,
    )


def resolve_dates(route: Route, today: date) -> list[str]:
    """Expand a route's ``dates`` block into concrete "YYYY-MM-DD" strings.

    Modes (report section 6.3):
      rolling  -> today+1 .. today+depart_in_days (inclusive, future days)
      fixed    -> the explicit fixed_dates list
      both     -> union of rolling and fixed, de-duplicated and sorted
    """
    dates_cfg = route.dates or {}
    mode = (dates_cfg.get("mode") or "fixed").lower()
    result: set[str] = set()

    if mode in ("rolling", "both"):
        n = int(dates_cfg.get("depart_in_days", 0) or 0)
        for i in range(1, n + 1):
            result.add((today + timedelta(days=i)).isoformat())

    if mode in ("fixed", "both"):
        for d in dates_cfg.get("fixed_dates", []) or []:
            result.add(str(d))

    return sorted(result)
