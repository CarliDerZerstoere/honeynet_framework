"""
Apply catalog snapshot to a WorldModel: systems with deploy.catalog_archetype get default_image.

Optional honeynet.catalog_pack plugins expose ``resolve(world_model, snapshot) -> WorldModel``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

from .snapshot import CatalogSnapshot

if TYPE_CHECKING:
    from ..models import WorldModel

logger = logging.getLogger(__name__)


class CatalogResolutionError(Exception):
    """Raised when an archetype id is missing or resolution is inconsistent."""


def apply_catalog_resolution(
    world_model: "WorldModel",
    snapshot: CatalogSnapshot,
    *,
    plugins: Optional[list[Any]] = None,
) -> tuple["WorldModel", list[dict[str, str]]]:
    """
    Mutates deploy.image for systems that declare catalog_archetype.

    Returns the world model and a list of applied operations for artifact logging.
    """
    idx = snapshot.entry_by_id()
    applied: list[dict[str, str]] = []

    for name, sys in world_model.systems.items():
        if not sys.deploy or not sys.deploy.catalog_archetype:
            continue
        aid = sys.deploy.catalog_archetype.strip()
        if not aid:
            continue
        entry = idx.get(aid)
        if entry is None:
            raise CatalogResolutionError(
                f"catalog_archetype {aid!r} for system {name!r} not found in snapshot"
            )
        sys.deploy.image = entry.default_image
        sys.deploy.catalog_archetype = None  # consumed; clear to prevent re-processing / artifact in output
        applied.append({"system": name, "catalog_archetype": aid, "image": entry.default_image})

    wm: WorldModel = world_model
    for plug in plugins or []:
        fn = getattr(plug, "resolve", None)
        if callable(fn):
            out = fn(wm, snapshot)
            if out is not None:
                from ..models import WorldModel as _WorldModel
                if isinstance(out, _WorldModel):
                    # Re-apply catalog image resolutions to the new WorldModel
                    # in case the plugin returned a fresh copy without them.
                    for op in applied:
                        sys_name = op["system"]
                        if sys_name in out.systems and out.systems[sys_name].deploy:
                            if not out.systems[sys_name].deploy.image or out.systems[sys_name].deploy.catalog_archetype:
                                out.systems[sys_name].deploy.image = op["image"]
                                out.systems[sys_name].deploy.catalog_archetype = None
                    wm = out
                else:
                    logger.warning(
                        "Catalog plugin %r returned %r instead of WorldModel — ignoring",
                        plug, type(out).__name__,
                    )

    return wm, applied
