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
    def send_digest(self, alerts: list, stats: dict) -> bool:
        """Send the daily digest (heartbeat). Sent even when ``alerts`` empty.

        Returns True on (attempted) success, False on failure. Must never raise.
        """
        raise NotImplementedError

    @abstractmethod
    def send_urgent(self, alert) -> bool:
        """Send a single urgent alert immediately. Must never raise."""
        raise NotImplementedError


# ----------------------------------------------------------------- registry
REGISTRY: dict[str, type] = {}


def register_notifier(name: str) -> Callable[[type], type]:
    def deco(cls: type) -> type:
        cls.name = name
        REGISTRY[name] = cls
        return cls
    return deco
