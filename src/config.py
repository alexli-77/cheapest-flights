"""Configuration loading and date resolution for flight-watch.

Source-of-truth policy (2026-07 fix): ``config.json`` is the single canonical
config. The web settings panel writes ``config.json`` reliably (structured JSON)
and only *mirrors* a human-readable ``config.yaml`` for eyeballing. Because the
panel's YAML mirror generation had indentation/leftover-line bugs that could
break ``yaml.safe_load`` and thus the scheduled run, loading now **prefers
``config.json`` whenever it exists** (next to, or as the sibling of, the given
path) and treats ``config.yaml`` as a fallback only. This makes the pipeline
immune to any YAML-mirror formatting glitch.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Optional

log = logging.getLogger("flight_watch.config")

#: hard upper bound on the number of concrete depart_dates generated per route
#: (protects the daily抓取 runtime — each date ≈ 17s of fetching).
MAX_DATES_PER_ROUTE = 60
#: rolling scalar窗口：前 DAILY_WINDOW 天逐日采样，之后每 SPARSE_STEP 天采样一次。
ROLLING_DAILY_WINDOW = 30
ROLLING_SPARSE_STEP = 3

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


#: default per-onward sampling cap for隐藏城市抓取 (protects daily runtime —
#: onward_routes × dates can explode; each fast-flights query ≈ 17s).
HIDDEN_CITY_MAX_DATES = 15


@dataclass
class HiddenCityConfig:
    """隐藏城市（中转中国）特价票监控配置（config.json 顶层 ``hidden_city`` 段）。

    监控「从 ``origin`` 出发、飞往 ``onward_routes`` 里某个延伸目的地、但中途在
    ``chinese_hubs`` 里某个中国城市中转」的跳程/隐藏城市票——真实目的地其实是那个
    中转的中国城市，用户只飞第一段。
    """

    enabled: bool = False
    origin: str = ""
    onward_routes: list = field(default_factory=list)
    chinese_hubs: list = field(default_factory=list)
    dates: dict = field(default_factory=lambda: {"mode": "rolling", "depart_in_days": 45})
    max_serpapi_per_run: int = 10
    min_saving_pct: float = 0.0
    #: 每条 onward_route 最多采样多少个日期（成本护栏）。
    max_dates_per_onward: int = HIDDEN_CITY_MAX_DATES
    #: fast-flights 直飞基线最多查几次（成本护栏；0 = 不查直飞基线）。
    max_direct_lookups: int = 6

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "HiddenCityConfig":
        d = d or {}
        return cls(
            enabled=bool(d.get("enabled", False)),
            origin=str(d.get("origin", "") or "").upper(),
            onward_routes=[str(x).upper() for x in (d.get("onward_routes") or [])],
            chinese_hubs=[str(x).upper() for x in (d.get("chinese_hubs") or [])],
            dates=d.get("dates") or {"mode": "rolling", "depart_in_days": 45},
            max_serpapi_per_run=int(d.get("max_serpapi_per_run", 10) or 0),
            min_saving_pct=float(d.get("min_saving_pct", 0) or 0),
            max_dates_per_onward=int(d.get("max_dates_per_onward", HIDDEN_CITY_MAX_DATES)
                                     or HIDDEN_CITY_MAX_DATES),
            max_direct_lookups=int(d.get("max_direct_lookups", 6) or 0),
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
    hidden_city: Optional[HiddenCityConfig] = None
    #: 日报航班详情增强段（config.json 顶层 ``enrich``）：
    #: {"enabled": bool, "max_per_run": int, "which": str}
    enrich: dict = field(default_factory=lambda: {
        "enabled": False, "max_per_run": 3, "which": "cheapest_per_route"})

    def route_by_id(self, route_id: str) -> Optional[Route]:
        for r in self.routes:
            if r.id == route_id:
                return r
        return None


def _load_raw(path: str) -> dict:
    """Load the raw config dict.

    ``config.json`` is the single source of truth (see module docstring): it is
    preferred whenever present, regardless of whether ``path`` points at the
    JSON or the YAML mirror. YAML is only parsed when no JSON is available.
    """
    # 1) Explicit .json path -> load it directly.
    if path.endswith(".json") and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    # 2) Any path whose .json sibling exists -> JSON wins (authoritative mirror,
    #    immune to YAML-panel formatting bugs). For a .json path this is itself;
    #    for config.yaml this is config.json next to it.
    sibling = os.path.splitext(path)[0] + ".json"
    if os.path.exists(sibling):
        with open(sibling, "r", encoding="utf-8") as f:
            return json.load(f)
    # 3) Fallback: parse the YAML mirror only when no JSON exists and PyYAML is
    #    installed.
    if path.endswith((".yaml", ".yml")) and _HAS_YAML and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    raise FileNotFoundError(
        f"No loadable config found for {path!r} (need a .json sibling or PyYAML for .yaml)"
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
        hidden_city=HiddenCityConfig.from_dict(raw.get("hidden_city")),
        enrich={
            "enabled": bool((raw.get("enrich") or {}).get("enabled", False)),
            "max_per_run": int((raw.get("enrich") or {}).get("max_per_run", 3) or 0),
            "which": str((raw.get("enrich") or {}).get("which", "cheapest_per_route")),
        },
    )


def _rolling_offsets(depart_in_days) -> list[int]:
    """Resolve the ``depart_in_days`` field into a list of day offsets (>=1).

    Two accepted forms (report section 6.3 + 2026-07 fix):

      * **list** ``[7, 14, 30]`` -> those exact future offsets (legacy form,
        kept working verbatim).
      * **scalar** ``N`` -> an auto-sampled future window: every day for the
        first ``ROLLING_DAILY_WINDOW`` days, then every ``ROLLING_SPARSE_STEP``
        days out to day ``N``. e.g. ``90`` -> 30 daily + 20 sparse = 50 offsets.
    """
    # List form: explicit offsets.
    if isinstance(depart_in_days, (list, tuple)):
        offsets = []
        for v in depart_in_days:
            try:
                iv = int(v)
            except (TypeError, ValueError):
                continue
            if iv >= 1:
                offsets.append(iv)
        return sorted(set(offsets))

    # Scalar form: sampled window.
    try:
        n = int(depart_in_days or 0)
    except (TypeError, ValueError):
        n = 0
    if n < 1:
        return []
    offsets = list(range(1, min(n, ROLLING_DAILY_WINDOW) + 1))
    if n > ROLLING_DAILY_WINDOW:
        offsets.extend(range(ROLLING_DAILY_WINDOW + ROLLING_SPARSE_STEP, n + 1,
                             ROLLING_SPARSE_STEP))
    return offsets


def resolve_dates(route: Route, today: date) -> list[str]:
    """Expand a route's ``dates`` block into concrete "YYYY-MM-DD" strings.

    Modes (report section 6.3):
      rolling  -> future offsets from ``depart_in_days`` (scalar sampled window
                  or explicit list; see :func:`_rolling_offsets`)
      fixed    -> the explicit fixed_dates list
      both     -> union of rolling and fixed, de-duplicated and sorted

    A hard cap of :data:`MAX_DATES_PER_ROUTE` concrete dates is enforced per
    route (excess truncated with a warning) to bound the daily抓取 runtime.
    """
    dates_cfg = route.dates or {}
    mode = (dates_cfg.get("mode") or "fixed").lower()
    result: set[str] = set()

    if mode in ("rolling", "both"):
        for i in _rolling_offsets(dates_cfg.get("depart_in_days")):
            result.add((today + timedelta(days=i)).isoformat())

    if mode in ("fixed", "both"):
        for d in dates_cfg.get("fixed_dates", []) or []:
            result.add(str(d))

    ordered = sorted(result)
    if len(ordered) > MAX_DATES_PER_ROUTE:
        log.warning(
            "route %s produced %d dates, truncating to cap %d",
            route.id, len(ordered), MAX_DATES_PER_ROUTE,
        )
        ordered = ordered[:MAX_DATES_PER_ROUTE]
    return ordered
