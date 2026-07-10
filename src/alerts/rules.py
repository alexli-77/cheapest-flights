"""Alert rules + self-registration registry (milestone M2).

Adding a new alert = new ``AlertRule`` subclass decorated with
``@register_rule``. The engine evaluates every registered rule against each
``(route_id, depart_date)`` node of the dashboard summary and collects the
:class:`Alert` objects the rules emit.

Rules never touch the network and never crash the pipeline: an exception inside
``evaluate`` is swallowed by the engine (defence in depth), but rules are
written to simply return ``None`` when they do not fire.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:  # avoid import cycle / heavy imports at module load
    from ..config import Config, Route
    from ..storage import Storage


# --------------------------------------------------------------------- Alert
@dataclass
class Alert:
    """A single alert emitted by a rule (or the failure watchdog).

    ``level`` is the *effective* disposition after the engine's merge step:
      ``"urgent"``  -> single push now (and also shown in the daily digest)
      ``"normal"``  -> folded into the daily digest only
    Rules emit their intended level; the engine may downgrade ``urgent`` ->
    ``normal`` on 24h dedup or the global daily cap.
    """

    rule_id: str
    level: str
    route_id: str
    depart_date: str
    price: Optional[float] = None
    prev_price: Optional[float] = None
    target_price: Optional[float] = None
    message: str = ""

    def key(self) -> str:
        """Dedup key for urgent single-push throttling."""
        return f"{self.route_id}|{self.depart_date}"

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "level": self.level,
            "route_id": self.route_id,
            "depart_date": self.depart_date,
            "price": self.price,
            "prev_price": self.prev_price,
            "target_price": self.target_price,
            "message": self.message,
        }


# ------------------------------------------------------------------ context
@dataclass
class RuleContext:
    """Everything a rule needs to decide whether to fire, for one node."""

    route: "Route"
    route_id: str
    depart_date: str
    node: dict          # summary node: {"latest", "historical_low", "series"}
    storage: "Storage"
    cfg: "Config"

    @property
    def latest(self) -> Optional[dict]:
        return self.node.get("latest")

    @property
    def series(self) -> list:
        return self.node.get("series") or []


# ------------------------------------------------------------------ registry
REGISTRY: dict[str, type] = {}


def register_rule(cls: type) -> type:
    """Class decorator: register an :class:`AlertRule` by its ``rule_id``."""
    REGISTRY[cls.rule_id] = cls
    return cls


class AlertRule(ABC):
    #: unique identifier, also used as ``Alert.rule_id``
    rule_id: str = "base"

    @abstractmethod
    def evaluate(self, ctx: RuleContext) -> Optional[Alert]:
        """Return an :class:`Alert` if the rule fires for ``ctx`` else ``None``."""
        raise NotImplementedError


def _fmt(v) -> str:
    """Format a price-ish number without a trailing ``.0`` for whole values."""
    if v is None:
        return "-"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return str(int(f)) if f == int(f) else f"{f:.2f}"


# -------------------------------------------------------------------- rules
@register_rule
class BelowTargetRule(AlertRule):
    """Fire (urgent) when today's lowest price is below the route target."""

    rule_id = "below_target"

    def evaluate(self, ctx: RuleContext) -> Optional[Alert]:
        target = ctx.route.target_price
        latest = ctx.latest
        if target is None or not latest:
            return None
        price = latest.get("price")
        if price is None or price >= target:
            return None
        cur = latest.get("currency", "CNY")
        msg = (
            f"{ctx.route.origin}->{ctx.route.dest} {ctx.depart_date} "
            f"今日最低 {_fmt(price)} {cur}，已低于目标价 {_fmt(target)}"
        )
        return Alert(
            rule_id=self.rule_id,
            level="urgent",
            route_id=ctx.route_id,
            depart_date=ctx.depart_date,
            price=price,
            prev_price=None,
            target_price=target,
            message=msg,
        )


@register_rule
class DropPctRule(AlertRule):
    """Fire when the latest fetch's low dropped >= ``drop_alert_pct`` vs the
    previous fetch's low for the same depart_date.

    A drop of >= 2x the threshold is ``urgent``; otherwise ``normal``.
    """

    rule_id = "drop_pct"

    def evaluate(self, ctx: RuleContext) -> Optional[Alert]:
        threshold = ctx.route.drop_alert_pct
        series = ctx.series
        if threshold is None or len(series) < 2:
            return None
        prev = series[-2].get("price")
        cur = series[-1].get("price")
        if not prev or cur is None or prev <= 0:
            return None
        drop_pct = (prev - cur) / prev * 100.0
        if drop_pct < threshold:
            return None
        level = "urgent" if drop_pct >= 2 * threshold else "normal"
        msg = (
            f"{ctx.route.origin}->{ctx.route.dest} {ctx.depart_date} "
            f"较上次抓取下降 {drop_pct:.1f}%（{_fmt(prev)} -> {_fmt(cur)}），"
            f"阈值 {_fmt(threshold)}%"
        )
        return Alert(
            rule_id=self.rule_id,
            level=level,
            route_id=ctx.route_id,
            depart_date=ctx.depart_date,
            price=cur,
            prev_price=prev,
            target_price=ctx.route.target_price,
            message=msg,
        )


#: minimum number of recorded fetch-days before ``historical_low`` may fire.
HISTORICAL_MIN_DAYS = 7


@register_rule
class HistoricalLowRule(AlertRule):
    """Fire (urgent) when the latest fetch sets a new all-time low for the
    route x depart_date, requiring >= 7 days of recorded history first
    (cold start never triggers).
    """

    rule_id = "historical_low"

    def evaluate(self, ctx: RuleContext) -> Optional[Alert]:
        series = ctx.series
        # Need >= 7 recorded fetch-days of history (cold start: no fire).
        if len(series) < HISTORICAL_MIN_DAYS:
            return None
        cur = series[-1].get("price")
        prior = [s.get("price") for s in series[:-1] if s.get("price") is not None]
        if cur is None or not prior:
            return None
        prior_min = min(prior)
        if cur >= prior_min:  # not a new low
            return None
        cur_ccy = series[-1].get("currency", "CNY")
        msg = (
            f"{ctx.route.origin}->{ctx.route.dest} {ctx.depart_date} "
            f"创历史新低 {_fmt(cur)} {cur_ccy}（前低 {_fmt(prior_min)}，"
            f"{len(series)} 天历史）"
        )
        return Alert(
            rule_id=self.rule_id,
            level="urgent",
            route_id=ctx.route_id,
            depart_date=ctx.depart_date,
            price=cur,
            prev_price=prior_min,
            target_price=ctx.route.target_price,
            message=msg,
        )
