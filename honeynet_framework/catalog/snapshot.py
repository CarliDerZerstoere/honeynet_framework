"""
Load and validate catalog snapshot JSON from disk.

Validation is structural (required keys, types) — no business thresholds in code.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class CatalogEntry:
    """One archetype row in a catalog snapshot."""

    id: str
    default_image: str
    labels: dict[str, str] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class CatalogSnapshot:
    """In-memory catalog snapshot."""

    schema_version: int
    entries: list[CatalogEntry]
    raw: dict[str, Any] = field(default_factory=dict)

    def entry_by_id(self) -> dict[str, CatalogEntry]:
        return {e.id: e for e in self.entries}


def load_catalog_snapshot(path: Path) -> CatalogSnapshot:
    """Load JSON from path; raise ValueError on missing file or invalid shape."""
    if not path.is_file():
        raise ValueError(f"Catalog snapshot not found: {path}")

    text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in catalog snapshot: {e}") from e

    if not isinstance(data, dict):
        raise ValueError("Catalog snapshot root must be an object")

    sv = data.get("schema_version")
    if not isinstance(sv, int) or sv < 1:
        raise ValueError("catalog snapshot requires positive integer schema_version")

    raw_entries = data.get("entries")
    if not isinstance(raw_entries, list):
        raise ValueError("catalog snapshot requires entries array")

    entries: list[CatalogEntry] = []
    seen: set[str] = set()
    for i, item in enumerate(raw_entries):
        if not isinstance(item, dict):
            raise ValueError(f"entries[{i}] must be an object")
        eid = item.get("id")
        img = item.get("default_image")
        if not isinstance(eid, str) or not eid.strip():
            raise ValueError(f"entries[{i}].id must be a non-empty string")
        if not isinstance(img, str) or not img.strip():
            raise ValueError(f"entries[{i}].default_image must be a non-empty string")
        if eid in seen:
            raise ValueError(f"duplicate catalog entry id: {eid!r}")
        seen.add(eid)
        labels_raw = item.get("labels")
        labels: dict[str, str] = {}
        if isinstance(labels_raw, dict):
            for k, v in labels_raw.items():
                if isinstance(k, str) and isinstance(v, str):
                    labels[k] = v
        known = {"id", "default_image", "labels"}
        extra = {k: v for k, v in item.items() if k not in known}
        entries.append(
            CatalogEntry(id=eid.strip(), default_image=img.strip(), labels=labels, extra=extra)
        )

    snap = CatalogSnapshot(schema_version=sv, entries=entries, raw=data)
    logger.info("Loaded catalog snapshot v%s with %d entries from %s", sv, len(entries), path)
    return snap
