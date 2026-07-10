"""Pipeline orchestrator for flight-watch (report milestone M1).

Flow:
  load config -> for each enabled route, for each resolved depart_date,
  try sources in order (retry 2x with exponential backoff 30s/90s, then
  degrade to the next source) -> mark is_lowest_of_day -> dedup + append JSONL
  -> build docs/data/summary.json -> call alert engine + notifier hooks.

The alert engine (src.alerts.engine.run_alerts) and notifiers
(src.notifiers.dispatch) are wired via try/except ImportError so this runs
before those modules (M2) exist.

CLI:
  python -m src.main                 # normal run
  python -m src.main --dry-run       # MockFetcher fake data, backoff/sleep=0
  python -m src.main --routes a,b    # only these route ids
  python -m src.main --config path   # custom config file
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from typing import Callable, Optional

from .config import load_config, resolve_dates, Route
from .models import FlightQuote, iso_now, today_shanghai
from .storage import Storage
from .fetchers.base import FetcherAdapter, FetchError, get_fetcher
from . import fetchers  # noqa: F401  (triggers fetcher registration)

log = logging.getLogger("flight_watch")

# Project layout (repo root = parent of this src/ package).
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG = os.path.join(ROOT, "config.yaml")
DATA_DIR = os.path.join(ROOT, "data")
DOCS_DIR = os.path.join(ROOT, "docs")
STATE_DIR = os.path.join(ROOT, "state")


# ---------------------------------------------------------------- dry-run source
class MockFetcher(FetcherAdapter):
    """Deterministic fake source for --dry-run / tests (no network, no deps)."""

    name = "mock"

    def available(self) -> bool:
        return True

    def fetch(self, route, depart_date: str) -> list:
        base = 800 + (abs(hash((route.id, depart_date))) % 1200)
        fetched_at = iso_now()
        out = []
        for i, (airline, fno) in enumerate([("MU", "MU523"), ("CA", "CA929"), ("HO", "HO1339")]):
            out.append(FlightQuote(
                fetched_at=fetched_at,
                route_id=route.id,
                origin=route.origin,
                dest=route.dest,
                depart_date=depart_date,
                airline=airline,
                flight_no=fno,
                stops=i % 2,
                price=base + i * 60,
                currency="CNY",
                raw_price=float(base + i * 60),
                raw_currency="CNY",
                price_type="total_with_tax",
                source="mock",
            ))
        return out


# ---------------------------------------------------------------- retry helper
def fetch_with_retry(
    fetcher: FetcherAdapter,
    route: Route,
    depart_date: str,
    backoffs: list,
    sleep_fn: Callable[[float], None],
) -> list:
    """Try fetch, retrying retryable FetchErrors with the given backoffs."""
    attempts = len(backoffs) + 1
    last_exc: Optional[Exception] = None
    for i in range(attempts):
        try:
            return fetcher.fetch(route, depart_date)
        except FetchError as e:
            last_exc = e
            if not e.retryable or i >= len(backoffs):
                raise
            wait = backoffs[i]
            log.warning("  %s retryable error (%s), backoff %ss then retry %d/%d",
                        fetcher.name, e, wait, i + 1, len(backoffs))
            sleep_fn(wait)
    if last_exc:
        raise last_exc
    return []


# ---------------------------------------------------------------- failures state
def _load_failures() -> dict:
    path = os.path.join(STATE_DIR, "failures.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_failures(data: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(os.path.join(STATE_DIR, "failures.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _record_route_result(failures: dict, route_id: str, success: bool, day: str) -> None:
    entry = failures.get(route_id, {"consecutive_failures": 0, "last_success": None, "last_failure": None})
    if success:
        entry["consecutive_failures"] = 0
        entry["last_success"] = day
    else:
        entry["consecutive_failures"] = int(entry.get("consecutive_failures", 0)) + 1
        entry["last_failure"] = day
    failures[route_id] = entry


# ---------------------------------------------------------------- pipeline
def mark_lowest_of_day(quotes: list) -> None:
    """Set is_lowest_of_day on the cheapest quote per (route_id, depart_date)."""
    groups: dict = {}
    for q in quotes:
        groups.setdefault((q.route_id, q.depart_date), []).append(q)
    for items in groups.values():
        for q in items:
            q.is_lowest_of_day = False
        cheapest = min(items, key=lambda q: q.price)
        cheapest.is_lowest_of_day = True


def run(
    config_path: str = DEFAULT_CONFIG,
    dry_run: bool = False,
    routes_filter: Optional[list] = None,
    backoffs: Optional[list] = None,
    request_interval: Optional[float] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict:
    cfg = load_config(config_path)
    today = today_shanghai()
    storage = Storage(DATA_DIR, DOCS_DIR)
    failures = _load_failures()

    # Backoff / inter-request sleep: injectable (0 for dry-run/tests).
    if backoffs is None:
        backoffs = list(cfg.defaults.get("retry_backoffs_seconds", [30, 90]))
    if request_interval is None:
        request_interval = float(cfg.defaults.get("request_interval_seconds", 10))
    if dry_run:
        backoffs = [0 for _ in backoffs]
        request_interval = 0

    all_quotes: list = []
    touched_routes: list = []

    for route in cfg.routes:
        if routes_filter and route.id not in routes_filter:
            continue
        if not route.enabled and not dry_run:
            log.info("route %s disabled, skipping", route.id)
            continue

        touched_routes.append(route.id)
        dates = resolve_dates(route, today)
        log.info("route %s -> %d dates", route.id, len(dates))
        route_success = False

        for depart_date in dates:
            sources = ["mock"] if dry_run else route.sources
            got = False
            for source in sources:
                fetcher = MockFetcher() if source == "mock" else get_fetcher(source)
                if fetcher is None:
                    log.warning("  source %s not registered, skipping", source)
                    continue
                if not fetcher.available():
                    log.info("  source %s unavailable, degrade to next", source)
                    continue
                try:
                    quotes = fetch_with_retry(fetcher, route, depart_date, backoffs, sleep_fn)
                    all_quotes.extend(quotes)
                    got = True
                    route_success = True
                    log.info("  %s %s via %s: %d quotes", route.id, depart_date, source, len(quotes))
                    break  # success on this source, stop trying others
                except FetchError as e:
                    log.warning("  %s %s via %s failed: %s -> degrade", route.id, depart_date, source, e)
                    continue
            if not got:
                log.warning("  %s %s: all sources failed", route.id, depart_date)
            # Anti-throttle pause between requests (0 in dry-run/tests).
            if request_interval:
                sleep_fn(request_interval)

        _record_route_result(failures, route.id, route_success, today.isoformat())

    # Mark lowest-of-day, dedup + persist.
    mark_lowest_of_day(all_quotes)
    written = storage.append_quotes(all_quotes)
    log.info("wrote %d new quotes (of %d fetched)", written, len(all_quotes))

    _save_failures(failures)

    # SerpAPI remaining quota for the dashboard, if that fetcher is around.
    meta = {}
    serp = get_fetcher("serpapi")
    if serp is not None and hasattr(serp, "remaining_quota"):
        try:
            meta["serpapi_remaining_quota"] = serp.remaining_quota()  # type: ignore[attr-defined]
        except Exception:
            pass

    # Run statistics for the digest heartbeat (consumed by src.notifiers).
    routes_ok = sum(
        1 for rid in touched_routes
        if int(failures.get(rid, {}).get("consecutive_failures", 0)) == 0
    )
    meta["run_stats"] = {
        "routes_total": len(touched_routes),
        "routes_ok": routes_ok,
        "routes_failed": len(touched_routes) - routes_ok,
        "fetched_count": len(all_quotes),
        "run_date": today.isoformat(),
    }

    summary = storage.build_summary(route_ids=None, extra=meta)
    log.info("summary.json built: %d routes", len(summary.get("routes", {})))

    # --- Hooks for later agents (M2). Modules may not exist yet. ---
    alerts: list = []
    try:
        from src.alerts.engine import run_alerts  # type: ignore
        alerts = run_alerts(cfg, storage, summary) or []
        log.info("alert engine ran: %d alerts", len(alerts))
    except ImportError:
        log.info("alert engine (src.alerts.engine) not present yet, skipping hook")
    except Exception as e:  # never let alerting crash the data pipeline
        log.error("alert engine raised: %s", e)

    try:
        from src.notifiers import dispatch  # type: ignore
        dispatch(cfg, summary, alerts=alerts)
        log.info("notifier dispatch ran")
    except ImportError:
        log.info("notifiers (src.notifiers.dispatch) not present yet, skipping hook")
    except Exception as e:
        log.error("notifier dispatch raised: %s", e)

    return {
        "routes": touched_routes,
        "fetched": len(all_quotes),
        "written": written,
        "summary_routes": list(summary.get("routes", {}).keys()),
    }


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="flight-watch daily pipeline")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--dry-run", action="store_true",
                        help="use MockFetcher fake data, backoff/sleep=0")
    parser.add_argument("--routes", default=None,
                        help="comma-separated route ids to run")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(message)s")

    routes_filter = [r.strip() for r in args.routes.split(",")] if args.routes else None
    result = run(config_path=args.config, dry_run=args.dry_run, routes_filter=routes_filter)
    log.info("DONE: %s", result)
    if args.dry_run:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
