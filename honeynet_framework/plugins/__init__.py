"""Optional setuptools entry-point plugins (catalog, repair, evaluation)."""

from .registry import (
    discover_catalog_packs,
    discover_repair_strategies,
    load_repair_strategy_callables,
    set_plugin_allowlist,
)

__all__ = [
    "discover_catalog_packs",
    "discover_repair_strategies",
    "load_repair_strategy_callables",
    "set_plugin_allowlist",
]
