"""Fetcher package. Importing it registers all built-in fetchers.

Each submodule uses lazy third-party imports, so importing this package never
crashes even when fast-flights / requests are not installed.
"""

from .base import (  # noqa: F401
    FetcherAdapter,
    FetchError,
    REGISTRY,
    register_fetcher,
    get_fetcher,
    available_fetchers,
)

# Import submodules for their @register_fetcher side effects.
from . import fast_flights  # noqa: F401
from . import serpapi  # noqa: F401
from . import ctrip_local  # noqa: F401
