"""Fetcher abstraction + self-registration registry (report section 4.4).

Adding a new data source = new file implementing ``FetcherAdapter`` decorated
with ``@register_fetcher("name")`` + one line in config's ``sources``. No core
changes required.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Optional


class FetchError(Exception):
    """Raised when a fetch fails.

    ``retryable=True`` means the pipeline may retry (with backoff) before
    degrading to the next source (e.g. transient empty result / network blip).
    ``retryable=False`` means give up on this source immediately (e.g. quota
    exhausted, source disabled).
    """

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class FetcherAdapter(ABC):
    #: unique source name, must match config ``sources`` entries
    name: str = "base"

    @abstractmethod
    def available(self) -> bool:
        """Return True if this fetcher can run right now (deps/env present)."""
        raise NotImplementedError

    @abstractmethod
    def fetch(self, route, depart_date: str) -> list:
        """Return a list[FlightQuote] for one route on one departure date.

        Should raise :class:`FetchError` on failure. Must not crash the process
        when optional third-party libraries are missing (lazy import).
        """
        raise NotImplementedError


# ----------------------------------------------------------------- registry
REGISTRY: dict[str, type] = {}
_INSTANCES: dict[str, FetcherAdapter] = {}


def register_fetcher(name: str) -> Callable[[type], type]:
    def deco(cls: type) -> type:
        cls.name = name
        REGISTRY[name] = cls
        return cls
    return deco


def get_fetcher(name: str) -> Optional[FetcherAdapter]:
    """Return a cached fetcher instance by name, or None if unregistered."""
    if name not in REGISTRY:
        return None
    if name not in _INSTANCES:
        _INSTANCES[name] = REGISTRY[name]()  # type: ignore[call-arg]
    return _INSTANCES[name]


def available_fetchers() -> list[str]:
    out = []
    for name in REGISTRY:
        f = get_fetcher(name)
        if f is not None and f.available():
            out.append(name)
    return out
