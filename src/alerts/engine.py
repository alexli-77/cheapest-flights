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
from .rules import REGISTRY, Alert, RuleContext

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

    # Failure watchdog (system alerts) participate in dedup / cap too.
    raw.extend(_failure_alerts(state_dir))

    merged = _apply_merge(raw, cfg, state_dir, now)
    n_urgent = sum(1 for a in merged if a.level == "urgent")
    log.info("alerts: %d raw -> %d total (%d urgent single-push)",
             len(raw), len(merged), n_urgent)
    return merged
