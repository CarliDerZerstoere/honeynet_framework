"""
Catalog snapshot loading and deterministic resolution (P4).

Snapshot JSON is policy data; paths come from OrchestratorConfig, not hardcoded rules.
"""

from .snapshot import CatalogEntry, CatalogSnapshot, load_catalog_snapshot
from .resolver import apply_catalog_resolution, CatalogResolutionError
from .startup_probe import StartupProbeResult, probe_startup_profile

__all__ = [
    "CatalogEntry",
    "CatalogSnapshot",
    "CatalogResolutionError",
    "load_catalog_snapshot",
    "apply_catalog_resolution",
    "StartupProbeResult",
    "probe_startup_profile",
]
