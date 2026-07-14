"""Notifier package (milestone M2).

Importing this package registers all built-in channels (Feishu, Telegram) via
module side effects and exposes ``dispatch`` — the pipeline entry point.

``dispatch(cfg, summary, alerts=None)``:
  * builds run statistics (成功/失败 route 数、抓取条数、SerpAPI 余额) from
    ``summary.meta`` for the digest heartbeat;
  * for each *enabled* channel in ``cfg.notifiers`` it single-pushes every
    urgent alert then sends the daily digest (when ``alerts.daily_digest``);
  * sleeps 1s between sends (anti-throttle); injectable for tests.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from .base import Notifier, REGISTRY, register_notifier  # noqa: F401

# Import channels for their @register_notifier side effects.
from . import feishu  # noqa: F401
from . import telegram  # noqa: F401

log = logging.getLogger("flight_watch.notifiers")

__all__ = ["Notifier", "REGISTRY", "register_notifier", "dispatch",
           "dispatch_hidden_city", "build_stats"]


def build_stats(cfg, summary: dict) -> dict:
    """Assemble the digest heartbeat statistics from the summary meta."""
    meta = (summary or {}).get("meta", {}) or {}
    run_stats = meta.get("run_stats", {}) or {}
    stats = {
        "routes_total": run_stats.get("routes_total", 0),
        "routes_ok": run_stats.get("routes_ok", 0),
        "routes_failed": run_stats.get("routes_failed", 0),
        "fetched_count": run_stats.get("fetched_count", 0),
        "serpapi_remaining_quota": meta.get("serpapi_remaining_quota"),
        "run_date": run_stats.get("run_date") or (summary or {}).get("generated_at", "")[:10],
        "run_status": "运行正常" if run_stats.get("routes_failed", 0) == 0
        else f"{run_stats.get('routes_failed', 0)} 条航线异常",
    }
    return stats


def dispatch(cfg, summary: dict, alerts: Optional[list] = None,
             sleep_fn: Callable[[float], None] = time.sleep,
             interval: float = 1.0) -> dict:
    """Fan out alerts to every enabled notifier. Returns a per-channel report.

    Never raises: individual channel/send failures are logged and skipped so the
    data pipeline is never taken down by a notification problem.
    """
    alerts = list(alerts or [])
    stats = build_stats(cfg, summary)
    urgent = [a for a in alerts if getattr(a, "level", "normal") == "urgent"]

    alerts_cfg = getattr(cfg, "alerts", {}) or {}
    digest_enabled = bool(alerts_cfg.get("daily_digest", True))
    notifiers_cfg = getattr(cfg, "notifiers", {}) or {}
    dashboard_cfg = getattr(cfg, "dashboard", {}) or {}
    routes = getattr(cfg, "routes", []) or []

    report: dict = {}
    for name, ncfg in notifiers_cfg.items():
        ncfg = ncfg or {}
        if not ncfg.get("enabled"):
            continue
        cls = REGISTRY.get(name)
        if cls is None:
            log.warning("notifier %s enabled but not registered, skipping", name)
            continue

        # Pass the dashboard config through so cards can link to the dashboard.
        merged_cfg = dict(ncfg)
        merged_cfg["dashboard"] = dashboard_cfg
        try:
            notifier = cls(merged_cfg)
        except Exception as e:
            log.warning("notifier %s init failed: %s", name, e)
            continue

        sent = {"urgent": 0, "digest": False}
        for a in urgent:
            try:
                if notifier.send_urgent(a, summary=summary):
                    sent["urgent"] += 1
            except Exception as e:
                log.warning("%s send_urgent failed: %s", name, e)
            sleep_fn(interval)

        if digest_enabled:
            try:
                sent["digest"] = bool(
                    notifier.send_digest(alerts, stats, summary=summary, routes=routes)
                )
            except Exception as e:
                log.warning("%s send_digest failed: %s", name, e)
            sleep_fn(interval)

        report[name] = sent
        log.info("notifier %s: %d urgent, digest=%s", name, sent["urgent"], sent["digest"])

    return report


def dispatch_hidden_city(cfg, hits: Optional[list] = None,
                         sleep_fn: Callable[[float], None] = time.sleep,
                         interval: float = 1.0) -> dict:
    """把隐藏城市命中作为独立卡片下发给支持它的 enabled 渠道。

    无命中时不发送任何卡片。Never raises — 单渠道失败只记录不抛出。
    """
    hits = list(hits or [])
    if not hits:
        return {}
    notifiers_cfg = getattr(cfg, "notifiers", {}) or {}
    dashboard_cfg = getattr(cfg, "dashboard", {}) or {}
    report: dict = {}
    for name, ncfg in notifiers_cfg.items():
        ncfg = ncfg or {}
        if not ncfg.get("enabled"):
            continue
        cls = REGISTRY.get(name)
        if cls is None:
            continue
        merged_cfg = dict(ncfg)
        merged_cfg["dashboard"] = dashboard_cfg
        try:
            notifier = cls(merged_cfg)
        except Exception as e:
            log.warning("notifier %s init failed: %s", name, e)
            continue
        send = getattr(notifier, "send_hidden_city", None)
        if not callable(send):
            continue
        try:
            report[name] = bool(send(hits))
        except Exception as e:
            log.warning("%s send_hidden_city failed: %s", name, e)
            report[name] = False
        sleep_fn(interval)
    return report
