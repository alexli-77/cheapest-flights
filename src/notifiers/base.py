"""Notifier abstraction + self-registration registry (milestone M2).

Adding a channel = new module implementing :class:`Notifier` decorated with
``@register_notifier("name")`` + a ``notifiers.<name>`` block in config. The
name must match the config key so ``dispatch`` can pair enabled config with the
right class.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Optional


class Notifier(ABC):
    #: unique channel name, must match the config ``notifiers`` key
    name: str = "base"

    def __init__(self, cfg: Optional[dict] = None):
        #: the ``notifiers.<name>`` config block
        self.cfg = cfg or {}

    @abstractmethod
    def send_digest(self, alerts: list, stats: dict,
                    summary: Optional[dict] = None,
                    routes: Optional[list] = None) -> bool:
        """Send the daily digest (heartbeat). Sent even when ``alerts`` empty.

        ``summary`` (enhanced dashboard summary) and ``routes`` (config Route
        list) let rich channels render per-route price摘要; plain channels may
        ignore them. Returns True on (attempted) success. Must never raise.
        """
        raise NotImplementedError

    @abstractmethod
    def send_urgent(self, alert, summary: Optional[dict] = None) -> bool:
        """Send a single urgent alert immediately. Must never raise.

        ``summary`` (optional) lets channels attach flight details.
        """
        raise NotImplementedError


# ----------------------------------------------------------------- registry
REGISTRY: dict[str, type] = {}


def register_notifier(name: str) -> Callable[[type], type]:
    def deco(cls: type) -> type:
        cls.name = name
        REGISTRY[name] = cls
        return cls
    return deco
