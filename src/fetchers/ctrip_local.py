"""Ctrip (携程) local-crawler adapter — placeholder skeleton only.

This is the plug-in slot for the domestic fallback crawler described in the
report (sections 3.3 and 4.2). It only becomes real if the M3 PoC is FALSIFIED
(fast-flights domestic coverage <80% or price deviation >15%). In that case a
local Selenium/IPv6 crawler (blueprint: Suysker/Ctrip-Crawler) runs on Leon's
own Mac via launchd, emits the SAME-schema JSONL, and git-pushes into the same
repo — so the dashboard and alerting are unaware of the source difference.

Because centralized cloud crawling of Ctrip gets IP-banned, this fetcher:
  * is disabled unless CTRIP_LOCAL_ENABLED=1 is set, AND
  * refuses to run under CI (GitHub Actions sets CI=true) — it is local-only.

``fetch`` intentionally raises NotImplementedError until A2 is falsified.
"""

from __future__ import annotations

import os

from .base import FetcherAdapter, register_fetcher


@register_fetcher("ctrip_local")
class CtripLocalFetcher(FetcherAdapter):
    name = "ctrip_local"

    def available(self) -> bool:
        # Local-only: must be explicitly enabled and NOT in a CI environment.
        if os.environ.get("CTRIP_LOCAL_ENABLED") != "1":
            return False
        if os.environ.get("CI"):  # GitHub Actions / most CI set CI=true
            return False
        return True

    def fetch(self, route, depart_date: str) -> list:
        raise NotImplementedError(
            "ctrip_local is a placeholder. Implement only if the M3 domestic PoC "
            "is falsified; runs locally (launchd) and emits same-schema JSONL."
        )
