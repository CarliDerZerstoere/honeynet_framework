"""Utility functions for the Honeynet Framework.

This module provides common helper functions used across the codebase.
"""

from __future__ import annotations

import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def safe_deep_get(
    data: dict[str, Any],
    keys: Sequence[str],
    default: T = None,
) -> Any | T:
    """Safely access nested dictionary keys without raising KeyError.

    Replaces the common pattern:
        value = data.get("key1", {}).get("key2", {}).get("key3", default)

    With:
        value = safe_deep_get(data, ["key1", "key2", "key3"], default)

    Args:
        data: The dictionary to access
        keys: Sequence of keys to traverse
        default: Value to return if any key is missing

    Returns:
        The value at the nested path, or default if not found

    Examples:
        >>> data = {"a": {"b": {"c": 42}}}
        >>> safe_deep_get(data, ["a", "b", "c"])
        42
        >>> safe_deep_get(data, ["a", "x", "y"], "missing")
        'missing'
        >>> safe_deep_get(data, ["a", "b"])
        {'c': 42}
    """
    _MISSING = object()
    result: Any = data
    for key in keys:
        if not isinstance(result, dict):
            return default
        result = result.get(key, _MISSING)
        if result is _MISSING:
            return default
    return result


def safe_deep_set(
    data: dict[str, Any],
    keys: Sequence[str],
    value: Any,
) -> None:
    """Safely set a value at a nested dictionary path, creating intermediate dicts.

    Args:
        data: The dictionary to modify
        keys: Sequence of keys to traverse/create
        value: Value to set at the final key

    Examples:
        >>> data = {}
        >>> safe_deep_set(data, ["a", "b", "c"], 42)
        >>> data
        {'a': {'b': {'c': 42}}}
    """
    if not keys:
        return

    current = data
    for key in keys[:-1]:
        if key not in current or not isinstance(current[key], dict):
            current[key] = {}
        current = current[key]

    current[keys[-1]] = value


def coalesce(*values: T | None) -> T | None:
    """Return the first non-None value, or None if all are None.

    Similar to SQL COALESCE or JavaScript's ?? operator chain.

    Args:
        *values: Values to check

    Returns:
        First non-None value, or None

    Examples:
        >>> coalesce(None, None, "default")
        'default'
        >>> coalesce("first", "second")
        'first'
    """
    for value in values:
        if value is not None:
            return value
    return None


def truncate(text: str, max_length: int = 100, suffix: str = "...") -> str:
    """Truncate text to a maximum length with a suffix.

    Args:
        text: Text to truncate
        max_length: Maximum length including suffix
        suffix: Suffix to add when truncated

    Returns:
        Truncated text with suffix if it was shortened

    Examples:
        >>> truncate("hello world", 8)
        'hello...'
        >>> truncate("short", 10)
        'short'
    """
    if len(text) <= max_length:
        return text
    if max_length <= len(suffix):
        return text[:max_length]
    return text[: max_length - len(suffix)] + suffix


def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    """Write *content* to *path* atomically via temp-file + rename.

    On POSIX ``os.replace`` is atomic within the same filesystem.
    On Windows it is *close enough* (replaces the target in a single
    system call) and far safer than a direct ``write_text`` which can
    leave a truncated file on crash or power loss.

    ``os.fsync`` is called before close to flush OS write buffers to the
    underlying storage device, guarding against data loss on power loss.

    On Windows ``os.replace`` fails with ``PermissionError`` if the target
    file is held open by another process.  We retry up to 3 times with a
    short back-off before re-raising.

    If the write or rename fails the temp file is cleaned up and the
    original file (if any) is left untouched.
    """
    import time as _time

    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd = None
    tmp_path: str | None = None
    try:
        fd, tmp_path = tempfile.mkstemp(
            dir=str(parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        data = content.encode(encoding)
        while data:
            n = os.write(fd, data)
            data = data[n:]
        os.fsync(fd)  # flush OS write buffers to storage before rename
        os.close(fd)
        fd = None  # prevent double-close in finally
        # On Windows, os.replace can fail with PermissionError if the target
        # is held open by another reader.  Retry a few times.
        for _attempt in range(3):
            try:
                os.replace(tmp_path, str(path))
                break
            except PermissionError:
                if _attempt == 2:
                    raise
                _time.sleep(0.05 * (2 ** _attempt))
        tmp_path = None  # successfully renamed — nothing to clean up
    except BaseException:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise


def detect_dependency_cycle(
    nodes: dict[str, list[str]],
) -> list[str]:
    """Detect circular dependencies using iterative DFS.

    Parameters
    ----------
    nodes:
        Mapping of node name → list of dependency names.  Unknown names
        (not keys in *nodes*) are silently skipped.

    Returns
    -------
    Cycle path (e.g. ``["A", "B", "C", "A"]``) or empty list if acyclic.
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {name: WHITE for name in nodes}

    for start in nodes:
        if color[start] != WHITE:
            continue
        color[start] = GRAY
        deps_iter = iter(nodes.get(start, []))
        stack: list[tuple[str, object]] = [(start, deps_iter)]

        while stack:
            node, it = stack[-1]
            try:
                dep = next(it)  # type: ignore[call-overload]
            except StopIteration:
                color[node] = BLACK
                stack.pop()
                continue

            if dep not in color:
                continue
            if color[dep] == GRAY:
                cycle = [dep]
                for frame_node, _ in reversed(stack):
                    cycle.append(frame_node)
                    if frame_node == dep:
                        break
                return list(reversed(cycle))
            if color[dep] == WHITE:
                color[dep] = GRAY
                dep_iter = iter(nodes.get(dep, []))
                stack.append((dep, dep_iter))

    return []
