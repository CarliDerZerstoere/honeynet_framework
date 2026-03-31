"""
Discover plugins via ``importlib.metadata.entry_points`` (groups declared in pyproject.toml).

Falls back to empty lists if metadata is missing or a distribution is broken.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class PluginLoadError(Exception):
    """Raised when a critical plugin fails to load and the caller requested strict mode."""

    def __init__(self, group: str, name: str, cause: Exception):
        self.group = group
        self.name = name
        self.cause = cause
        super().__init__(f"Failed to load plugin {group}:{name}: {cause}")

REPAIR_GROUP = "honeynet.repair_strategy"
CATALOG_GROUP = "honeynet.catalog_pack"

# Module-level allowlist — set by the orchestrator before discovery.
# None (never called) = deny all (default-secure).
# Empty list [] = allow ALL plugins (explicitly opened).
# Non-empty list = only listed plugin names are loaded.
_plugin_allowlist: list[str] | None = None  # None = not yet configured → deny all
_plugin_allowlist_lock = threading.Lock()


def set_plugin_allowlist(names: list[str]) -> None:
    """Restrict which plugin names may be loaded.

    Pass a non-empty list to allow only specific plugins by name.
    Pass an empty list to allow ALL plugins (open allowlist).
    ``None`` (never called) blocks all plugins (default-deny).
    """
    global _plugin_allowlist
    with _plugin_allowlist_lock:
        _plugin_allowlist = list(names)
    if not names:
        logger.info("Plugin allowlist set to [] — all plugins allowed")


def _is_allowed(name: str) -> bool:
    """Check whether a plugin name passes the allowlist.

    - ``None`` (never configured): deny all (default-secure).
    - ``[]`` (empty list): allow all (explicitly opened).
    - ``["a", "b"]``: allow only "a" and "b".
    """
    with _plugin_allowlist_lock:
        allowlist = _plugin_allowlist
    if allowlist is None:
        return False
    if len(allowlist) == 0:
        return True  # empty = allow all
    return name in allowlist


def discover_repair_strategies(*, strict: bool = False) -> list[tuple[str, Any]]:
    """Return (name, loaded_object) pairs; skips broken entries.

    If *strict* is True, raises :class:`PluginLoadError` on the first failure
    instead of logging and skipping.

    Only plugins whose name is in the allowlist (if set) are loaded.
    """
    try:
        from importlib.metadata import entry_points
    except ImportError:
        return []

    eps = entry_points()
    selected = eps.select(group=REPAIR_GROUP) if hasattr(eps, "select") else [
        e for e in eps.get(REPAIR_GROUP, [])  # type: ignore[union-attr]
    ]
    out: list[tuple[str, Any]] = []
    seen: set[str] = set()
    for ep in selected:
        if ep.name in seen:
            logger.warning("Duplicate repair_strategy entry point name %r — skipping duplicate", ep.name)
            continue
        seen.add(ep.name)
        if not _is_allowed(ep.name):
            logger.info("Plugin %r blocked by allowlist — skipping", ep.name)
            continue
        try:
            obj = ep.load()
            out.append((ep.name, obj))
        except Exception as e:
            if strict:
                raise PluginLoadError(REPAIR_GROUP, ep.name, e) from e
            logger.error("Failed to load repair_strategy %r: %s", ep.name, e)
    return out


def load_repair_strategy_callables() -> list[Callable[..., Any]]:
    """Return callables from entry points; unknown shapes are skipped."""
    callables: list[Callable[..., Any]] = []
    for name, obj in discover_repair_strategies():
        if callable(obj):
            callables.append(obj)
        else:
            logger.warning("repair_strategy %r is not callable", name)
    return callables


def discover_catalog_packs(*, strict: bool = False) -> list[tuple[str, Any]]:
    """Return (name, loaded_object) for group honeynet.catalog_pack.

    If *strict* is True, raises :class:`PluginLoadError` on the first failure.
    Only plugins whose name is in the allowlist (if set) are loaded.
    """
    try:
        from importlib.metadata import entry_points
    except ImportError:
        return []

    eps = entry_points()
    selected = eps.select(group=CATALOG_GROUP) if hasattr(eps, "select") else [
        e for e in eps.get(CATALOG_GROUP, [])  # type: ignore[union-attr]
    ]
    out: list[tuple[str, Any]] = []
    seen: set[str] = set()
    for ep in selected:
        if ep.name in seen:
            logger.warning("Duplicate catalog_pack entry point name %r — skipping duplicate", ep.name)
            continue
        seen.add(ep.name)
        if not _is_allowed(ep.name):
            logger.info("Plugin %r blocked by allowlist — skipping", ep.name)
            continue
        try:
            obj = ep.load()
            out.append((ep.name, obj))
        except Exception as e:
            if strict:
                raise PluginLoadError(CATALOG_GROUP, ep.name, e) from e
            logger.error("Failed to load catalog_pack %r: %s", ep.name, e)
    return out
