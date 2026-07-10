"""Alert engine package (milestone M2).

Importing this package registers all built-in rules (via ``rules`` module side
effects) and exposes the engine entry point ``run_alerts``.
"""

from .rules import (  # noqa: F401
    Alert,
    AlertRule,
    RuleContext,
    REGISTRY,
    register_rule,
)
from .engine import run_alerts  # noqa: F401

__all__ = ["Alert", "AlertRule", "RuleContext", "REGISTRY", "register_rule", "run_alerts"]
