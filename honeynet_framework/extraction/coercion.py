"""
Type coercion utilities for parsing LLM/YAML payloads.

These functions defensively normalize loosely-typed data from LLM outputs
into the strict Python types expected by WorldModel dataclasses.
"""

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..models import VolumeMount

logger = logging.getLogger(__name__)


def coerce_bool(value: object, default: bool = False) -> bool:
    """Parse booleans defensively from LLM/YAML payloads.

    LLM output sometimes quotes booleans as strings like "false" or "no".
    Using bool(<string>) would incorrectly treat those as True.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y", "on"}:
            return True
        if normalized in {"false", "0", "no", "n", "off", ""}:
            return False
    return default


def coerce_string_list(value: object) -> list[str]:
    """Normalize YAML/LLM scalar-or-list fields into a clean string list."""
    if value is None:
        return []
    if isinstance(value, list):
        raw_items = value
    else:
        raw_items = [value]

    items: list[str] = []
    for item in raw_items:
        if item is None:
            logger.debug("coerce_string_list: dropping None element")
            continue
        # YAML auto-converts yes/no/true/false to Python booleans.
        # Convert them to their lowercase string representations instead of
        # dropping them, since they may be legitimate values (e.g. env vars).
        if isinstance(item, bool):
            logger.debug("coerce_string_list: converting bool(%s) to string — likely YAML boolean", item)
            item = "true" if item else "false"
        text = str(item).strip()
        if text:
            items.append(text)
    return items


def coerce_int_list(value: object) -> list[int]:
    """Normalize scalar-or-list numeric payloads into integer lists.

    Handles Docker-style port specs: plain ints, "host:container" mappings,
    port ranges ("8080-8090"), and protocol suffixes ("53/udp").
    For host:container mappings, the container (right-hand) port is used.
    Port ranges are expanded into individual ports.
    """
    if value is None:
        return []
    raw_items = value if isinstance(value, list) else [value]

    items: list[int] = []
    for item in raw_items:
        s = str(item).strip()
        # Strip protocol suffix (e.g. "53/udp" → "53")
        if "/" in s:
            s = s.split("/")[0].strip()
        # Handle host:container mapping (e.g. "8080:80" → 80)
        if ":" in s:
            parts = s.split(":")
            s = parts[-1].strip()  # use container port (rightmost)
        # Handle port range (e.g. "8080-8090")
        if "-" in s:
            range_parts = s.split("-", 1)
            try:
                start, end = int(range_parts[0]), int(range_parts[1])
                capped_end = min(end, start + 100)
                if capped_end < end:
                    logger.warning(
                        "coerce_int_list: port range %d-%d truncated to %d-%d (max 100 ports)",
                        start, end, start, capped_end,
                    )
                for p in range(start, capped_end + 1):
                    items.append(p)
                continue
            except (ValueError, IndexError):
                pass
        try:
            items.append(int(s))
        except (TypeError, ValueError):
            logger.warning("coerce_int_list: dropping non-integer value %r", item)
            continue
    return items


def coerce_volume_mounts(value: object) -> list["VolumeMount"]:
    """Normalize scalar-or-list volume declarations into VolumeMount objects."""
    from ..models import VolumeMount

    raw_items = value if isinstance(value, list) else ([value] if value else [])
    mounts: list[VolumeMount] = []

    for raw in raw_items:
        if isinstance(raw, dict):
            mounts.append(
                VolumeMount(
                    name=str(raw.get("name", "")),
                    path=str(raw.get("path", "")),
                    readonly=coerce_bool(raw.get("readonly", False), False),
                )
            )
        elif isinstance(raw, str) and ":" in raw:
            parts = raw.split(":")
            # Windows drive-letter aware: if parts[0] is a single letter (C, D, ...)
            # and parts[1] starts with \ or /, rejoin as "C:\path" to avoid splitting
            # the drive letter from the host path.
            if len(parts) >= 3 and len(parts[0]) == 1 and parts[0].isalpha():
                # Rejoin drive-letter: "C" + ":\\" + rest
                host_path = parts[0] + ":" + parts[1]
                container_path = parts[2] if len(parts) > 2 else ""
                readonly = "ro" in parts[3:] or (len(parts) > 3 and parts[-1].lower() == "ro")
                mounts.append(VolumeMount(name=host_path, path=container_path, readonly=readonly))
            else:
                mounts.append(
                    VolumeMount(
                        name=parts[0],
                        path=parts[1] if len(parts) > 1 else "",
                        readonly="ro" in parts[2:] or (len(parts) > 2 and parts[-1].lower() == "ro"),
                    )
                )

    return mounts
