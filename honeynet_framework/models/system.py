"""
System models - core containers with deploy/simulate split.
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .enums import SystemKind


@dataclass
class VolumeMount:
    """Volume mount specification for containers."""
    name: str
    path: str
    readonly: bool = False

    def to_dict(self) -> dict:
        return {"name": self.name, "path": self.path, "readonly": self.readonly}


@dataclass
class SystemDeploy:
    """
    Deploy-time system settings - maps directly to deterministic IaC.
    """
    image: str
    zone: str
    ports: list[int] = field(default_factory=list)
    env: list[str] = field(default_factory=list)
    volumes: list[VolumeMount] = field(default_factory=list)
    command: Optional[list[str]] = None
    depends_on: list[str] = field(default_factory=list)
    healthcheck: Optional[dict] = None
    catalog_archetype: Optional[str] = None

    def to_dict(self) -> dict:
        result = {
            "image": self.image,
            "zone": self.zone,
            "ports": self.ports,
            "env": self.env,
            "volumes": [v.to_dict() for v in self.volumes],
            "depends_on": self.depends_on,
        }
        if self.command:
            result["command"] = self.command
        if self.healthcheck:
            result["healthcheck"] = self.healthcheck
        if self.catalog_archetype:
            result["catalog_archetype"] = self.catalog_archetype
        return result


@dataclass
class SystemSimulate:
    """
    Simulation-time system behavior - for population and runtime.
    """
    hostname: str = ""
    role: str = ""
    issues: list[str] = field(default_factory=list)
    secrets: list[str] = field(default_factory=list)
    behaviors: list[str] = field(default_factory=list)
    services: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "hostname": self.hostname,
            "role": self.role,
            "issues": self.issues,
            "secrets": self.secrets,
            "behaviors": self.behaviors,
            "services": self.services,
        }


@dataclass
class System:
    """
    Core system definition - the heart of the World Model.
    """
    name: str
    kind: "SystemKind" = None  # type: ignore[assignment]
    owner: Optional[str] = None
    deploy: Optional[SystemDeploy] = None
    simulate: Optional[SystemSimulate] = None

    def __post_init__(self):
        from .enums import SystemKind
        if self.kind is None:
            self.kind = SystemKind.UNKNOWN
        elif isinstance(self.kind, str) and not isinstance(self.kind, SystemKind):
            self.kind = SystemKind.coerce(self.kind)

    @property
    def is_deployable(self) -> bool:
        """Check if this system has deploy configuration."""
        return self.deploy is not None

    def to_dict(self) -> dict:
        result = {"name": self.name, "kind": self.kind.value}
        if self.owner:
            result["owner"] = self.owner
        if self.deploy:
            result["deploy"] = self.deploy.to_dict()
        if self.simulate:
            result["simulate"] = self.simulate.to_dict()
        return result
