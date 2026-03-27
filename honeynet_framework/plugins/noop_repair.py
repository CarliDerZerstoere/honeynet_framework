"""
No-op repair strategy — reference implementation of the ``honeynet.repair_strategy`` entry-point.

A repair strategy is a callable with the signature::

    (world_model: WorldModel, failure_report: FailureReport) -> WorldModel

This implementation returns the world model unchanged. Register via pyproject.toml::

    [project.entry-points."honeynet.repair_strategy"]
    noop = "honeynet_framework.plugins.noop_repair:noop_repair"
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..models import WorldModel

logger = logging.getLogger(__name__)


def noop_repair(world_model: Any, failure_report: Any = None) -> Any:
    """Return the world model unchanged (no-op repair strategy)."""
    logger.debug("noop_repair: no changes applied")
    return world_model
