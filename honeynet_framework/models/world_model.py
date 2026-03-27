"""
Main WorldModel class - Single Source of Truth.
"""

from dataclasses import dataclass, field
from typing import Optional

from .organization import Organization
from .system import System
from .zone import WorldZone
from .secret import Secret


@dataclass
class WorldModel:
    """
    Complete World Model - Single Source of Truth.

    Key Invariant: deterministic IaC reads ONLY from deploy sections.
    """
    organization: Optional[Organization] = None
    systems: dict[str, System] = field(default_factory=dict)
    zones: dict[str, WorldZone] = field(default_factory=dict)
    secrets: dict[str, Secret] = field(default_factory=dict)

    @property
    def project_name(self) -> str:
        """Get project name from organization."""
        if self.organization:
            return self.organization.name.lower().replace(" ", "_")
        return "honeynet"

    @property
    def deployable_systems(self) -> dict[str, System]:
        """Get only systems that have deploy configuration."""
        return {k: v for k, v in self.systems.items() if v.is_deployable}

    def to_dict(self) -> dict:
        """Serialize to dictionary for YAML/JSON output."""
        result = {
            "zones": {k: v.to_dict() for k, v in self.zones.items()},
            "systems": {k: v.to_dict() for k, v in self.systems.items()},
        }
        if self.organization:
            result["organization"] = self.organization.to_dict()
        if self.secrets:
            result["secrets"] = {k: v.to_dict() for k, v in self.secrets.items()}
        return result
