"""Alert engine — evaluate rules, add the failure watchdog, apply the merge /
throttle strategy (milestone M2).

Called from the pipeline via ``run_alerts(cfg, storage, summary)``.

Merge strategy (report + M2 brief):
  * ``normal`` alerts   -> all folded into the daily digest.
  * ``urgent`` alerts   -> single push, but at most once per (route_id,
    depart_date) within ``alerts.urgent_dedup_hours`` (default 24h), recorded in
    ``state/alert_sent.json``; and a global daily cap of
    ``alerts.max_urgent_per_day`` (default 5). Anything deduped or over the cap
    is downgraded to ``normal`` (still shown in the digest).

Failure watchdog: reads ``state/failures.json``; any route with
``consecutive_failures >= 2`` yields an urgent system alert.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

from ..models import now_shanghai, SHANGHAI
from .rules import REGISTRY, Alert, RuleContext, _fmt

try:  # airport 中文名表（卡片文案用）；解析失败/缺失时退回三字码，绝不影响告警。
    from ..airports import display_label as _airport_display_label
except Exception:  # pragma: no cover - defensive
    _airport_display_label = None

log = logging.getLogger("flight_watch.alerts")

# Repo root = parent of src/  -> default state dir.
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_STATE_DIR = os.path.join(_ROOT, "state")

FAILURE_THRESHOLD = 2  # consecutive failures before a system alert fires


# ------------------------------------------------------------ state helpers
def _load_json(path: str) -> dict:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception:
            return {}
    return {}


def _save_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _parse_ts(ts: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SHANGHAI)
    return dt


# ---------------------------------------------------------- failure watchdog
def _failure_alerts(state_dir: str) -> list[Alert]:
    failures = _load_json(os.path.join(state_dir, "failures.json"))
    out: list[Alert] = []
    for route_id, entry in failures.items():
        if not isinstance(entry, dict):
            continue
        n = int(entry.get("consecutive_failures", 0) or 0)
        if n >= FAILURE_THRESHOLD:
            out.append(Alert(
                rule_id="source_failure",
                level="urgent",
                route_id=route_id,
                depart_date="",
                price=None,
                prev_price=None,
                target_price=None,
                message=f"数据源连续 {n} 天无数据（route={route_id}）",
            ))
    return out


# ------------------------------------------------- route-level new-low alerts
def _route_label(route, route_id: str) -> str:
    """"蒙特利尔(YUL)→北京(PEK)" from a route (falls back to the route_id)."""
    origin = str(getattr(route, "origin", "") or "").upper() if route else ""
    dest = str(getattr(route, "dest", "") or "").upper() if route else ""
    if _airport_display_label is not None and origin and dest:
        try:
            return f"{_airport_display_label(origin)}→{_airport_display_label(dest)}"
        except Exception:
            pass
    return route_id


def _price_str(price, currency: str) -> str:
    cur = currency or "CNY"
    return f"¥{_fmt(price)}" if cur == "CNY" else f"{cur} {_fmt(price)}"


def _mmdd(dd: str) -> str:
    parts = str(dd or "").split("-")
    return "-".join(parts[1:]) if len(parts) == 3 else str(dd or "")


def _route_new_low_message(route, route_id, price, prev_price, dd,
                           drop_pct, target, currency) -> str:
    label = _route_label(route, route_id)
    msg = (
        f"{label} 全航线新低 {_price_str(price, currency)}（出发 {_mmdd(dd)}），"
        f"前低 {_price_str(prev_price, currency)}，↓{drop_pct:.1f}%"
    )
    if target is not None:
        try:
            gap = float(price) - float(target)
        except (TypeError, ValueError):
            gap = None
        if gap is not None:
            if gap <= 0:
                msg += f"，已低于目标价 {_fmt(target)}"
            else:
                msg += f"，距目标价 +{_fmt(gap)}"
    return msg


def _route_new_low_alerts(cfg, summary: dict, state_dir: str,
                          now: datetime) -> list[Alert]:
    """Emit at most one urgent ``route_new_low`` alert per route.

    For each route we compute ``route_min`` = the cheapest ``latest.price`` over
    all currently-monitored (future) depart_dates in the summary. A route-level
    best price is persisted in ``state/route_best.json``
    ``{route_id: {"price", "depart_date", "updated"}}``. An alert fires only when
    ``route_min`` breaks the recorded floor by BOTH ``alerts.new_low_min_pct``
    (default 2.0 %) AND ``alerts.new_low_min_abs`` (default 50, absolute money) —
    this kills the per-depart_date 0.1 % micro-new-low spam. Cold start (no
    record for the route) records the baseline silently and never alerts.
    """
    alerts_cfg = getattr(cfg, "alerts", {}) or {}
    try:
        min_pct = float(alerts_cfg.get("new_low_min_pct", 2.0))
    except (TypeError, ValueError):
        min_pct = 2.0
    try:
        min_abs = float(alerts_cfg.get("new_low_min_abs", 50))
    except (TypeError, ValueError):
        min_abs = 50.0

    best_path = os.path.join(state_dir, "route_best.json")
    best = _load_json(best_path)

    out: list[Alert] = []
    dirty = False
    routes_summary = (summary or {}).get("routes", {}) or {}
    for route_id, rdata in routes_summary.items():
        dd_map = (rdata.get("depart_dates", {}) or {}) if isinstance(rdata, dict) else {}
        # route_min across all future depart_dates that have a latest price.
        candidates = []
        for dd, node in dd_map.items():
            latest = (node or {}).get("latest") or {}
            price = latest.get("price")
            if price is None:
                continue
            candidates.append((price, dd, latest))
        if not candidates:
            continue
        price, dd, latest = min(candidates, key=lambda t: t[0])
        currency = latest.get("currency", "CNY")

        rec = best.get(route_id)
        prev_price = rec.get("price") if isinstance(rec, dict) else None

        # Cold start: record the baseline silently, never alert this run.
        if prev_price is None:
            best[route_id] = {"price": price, "depart_date": dd,
                              "updated": now.isoformat()}
            dirty = True
            continue

        # Only a genuine new low that clears BOTH the pct and abs thresholds.
        if price >= prev_price or prev_price <= 0:
            continue
        drop_abs = prev_price - price
        drop_pct = drop_abs / prev_price * 100.0
        if drop_pct < min_pct or drop_abs < min_abs:
            continue

        route = cfg.route_by_id(route_id)
        target = getattr(route, "target_price", None) if route else None
        msg = _route_new_low_message(route, route_id, price, prev_price, dd,
                                     drop_pct, target, currency)
        out.append(Alert(
            rule_id="route_new_low", level="urgent", route_id=route_id,
            depart_date=dd, price=price, prev_price=prev_price,
            target_price=target, message=msg,
        ))
        best[route_id] = {"price": price, "depart_date": dd,
                          "updated": now.isoformat()}
        dirty = True

    if dirty:
        _save_json(best_path, best)
    return out


# ------------------------------------------------------------- merge / cap
def _apply_merge(raw: list[Alert], cfg, state_dir: str, now: datetime) -> list[Alert]:
    alerts_cfg = getattr(cfg, "alerts", {}) or {}
    max_urgent = int(alerts_cfg.get("max_urgent_per_day", 5))
    dedup_hours = float(alerts_cfg.get("urgent_dedup_hours", 24))

    sent_path = os.path.join(state_dir, "alert_sent.json")
    sent = _load_json(sent_path)
    today = now.date().isoformat()

    # How many urgent single-pushes already went out today.
    urgent_today = sum(1 for v in sent.values()
                       if isinstance(v, dict) and v.get("date") == today)

    result: list[Alert] = []
    for a in raw:
        if a.level != "urgent":
            result.append(a)
            continue

        key = a.key()
        last = sent.get(key)
        last_ts = _parse_ts(last["ts"]) if isinstance(last, dict) and last.get("ts") else None

        # 24h dedup: same route x depart_date pushed recently -> digest only.
        if last_ts is not None and (now - last_ts) < timedelta(hours=dedup_hours):
            a.level = "normal"
            result.append(a)
            continue

        # Global daily cap (circuit breaker) -> downgrade to digest.
        if urgent_today >= max_urgent:
            a.level = "normal"
            result.append(a)
            log.info("urgent cap reached (%d), downgrading %s %s to digest",
                     max_urgent, a.route_id, a.depart_date)
            continue

        # Keep as urgent single-push; record for future dedup.
        sent[key] = {"ts": now.isoformat(), "date": today}
        urgent_today += 1
        result.append(a)

    _save_json(sent_path, sent)
    return result


# ----------------------------------------------------------------- public
def run_alerts(cfg, storage, summary: dict,
               state_dir: str = DEFAULT_STATE_DIR,
               now: Optional[datetime] = None) -> list[Alert]:
    """Evaluate all rules over the summary, add failure alerts, apply the
    merge/throttle strategy, and return the resulting :class:`Alert` list.

    The returned list is what the notifier layer consumes: entries with
    ``level == "urgent"`` are single-pushed; every entry (urgent + normal) is
    available for the daily digest.
    """
    now = now or now_shanghai()
    raw: list[Alert] = []

    routes_summary = (summary or {}).get("routes", {})
    for route_id, rdata in routes_summary.items():
        route = cfg.route_by_id(route_id)
        if route is None:
            continue
        for depart_date, node in (rdata.get("depart_dates", {}) or {}).items():
            ctx = RuleContext(
                route=route, route_id=route_id, depart_date=depart_date,
                node=node, storage=storage, cfg=cfg,
            )
            for rule_cls in REGISTRY.values():
                try:
                    alert = rule_cls().evaluate(ctx)
                except Exception as e:  # a broken rule must not sink the run
                    log.warning("rule %s crashed on %s %s: %s",
                                getattr(rule_cls, "rule_id", "?"),
                                route_id, depart_date, e)
                    continue
                if alert is not None:
                    raw.append(alert)

    # Route-level new-low alerts (one urgent per route; replaces the old
    # per-depart_date historical_low spam). Participate in dedup / cap too.
    raw.extend(_route_new_low_alerts(cfg, summary, state_dir, now))

    # Failure watchdog (system alerts) participate in dedup / cap too.
    raw.extend(_failure_alerts(state_dir))

    merged = _apply_merge(raw, cfg, state_dir, now)
    n_urgent = sum(1 for a in merged if a.level == "urgent")
    log.info("alerts: %d raw -> %d total (%d urgent single-push)",
             len(raw), len(merged), n_urgent)
    return merged
