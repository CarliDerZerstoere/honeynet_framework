"""
Deploy Projection models - compiler output for deterministic IaC.
"""

from dataclasses import dataclass, field
from typing import Optional
import platform


@dataclass
class Port:
    """Port mapping for a container."""
    internal: int
    external: int
    protocol: str = "tcp"
    ip: Optional[str] = None

    def to_dict(self) -> dict:
        d = {"internal": self.internal, "external": self.external, "protocol": self.protocol}
        if self.ip is not None:
            d["ip"] = self.ip
        return d


@dataclass
class DeployNetwork:
    """Network ready for Terraform generation."""
    name: str
    driver: str = "bridge"
    internal: bool = True
    zone_name: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "driver": self.driver,
            "internal": self.internal,
        }


@dataclass
class DeployContainer:
    """Container ready for Terraform generation."""
    name: str
    image: str
    networks: list[str] = field(default_factory=list)
    env: list[str] = field(default_factory=list)
    ports: list[Port] = field(default_factory=list)
    volumes: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    command: Optional[list[str]] = None
    resolved_digest_ref: Optional[str] = None
    network_aliases: dict[str, list[str]] = field(default_factory=dict)
    hostname: Optional[str] = None
    network_mode: Optional[str] = None
    healthcheck: Optional[dict] = None

    def to_dict(self) -> dict:
        result = {
            "name": self.name,
            "image": self.image,
            "networks": self.networks,
            "env": self.env,
            "ports": [p.to_dict() for p in self.ports],
            "volumes": self.volumes,
            "depends_on": self.depends_on,
        }
        if self.command:
            result["command"] = self.command
        if self.resolved_digest_ref:
            result["resolved_digest_ref"] = self.resolved_digest_ref
        if self.network_aliases:
            result["network_aliases"] = {k: list(v) for k, v in self.network_aliases.items()}
        if self.hostname:
            result["hostname"] = self.hostname
        if self.network_mode:
            result["network_mode"] = self.network_mode
        if self.healthcheck:
            result["healthcheck"] = dict(self.healthcheck)
        return result


@dataclass
class TerraformConstraints:
    """Terraform provider constraints."""
    provider_source: str = "registry.terraform.io/kreuzwerker/docker"
    provider_version: str = "3.6.2"
    docker_host: str = ""

    def __post_init__(self):
        if not self.docker_host:
            self.docker_host = (
                "npipe:////./pipe/docker_engine"
                if platform.system() == "Windows"
                else "unix:///var/run/docker.sock"
            )


@dataclass
class DeployProjection:
    """
    Pure deployment data extracted from WorldModel.

    Key Property: This is a pure function output - same WorldModel always
    produces the same DeployProjection.
    """
    project_name: str
    networks: list[DeployNetwork] = field(default_factory=list)
    containers: list[DeployContainer] = field(default_factory=list)
    volumes: list[str] = field(default_factory=list)
    constraints: TerraformConstraints = field(default_factory=TerraformConstraints)
    backbone_network_name: str = ""
    # Bidirectional mapping: system_name ↔ container_name (set by DeployCompiler)
    container_to_system: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Serialize for JSON output."""
        result = {
            "project_name": self.project_name,
            "networks": [n.to_dict() for n in self.networks],
            "containers": [c.to_dict() for c in self.containers],
            "volumes": self.volumes,
        }
        if self.backbone_network_name:
            result["backbone_network_name"] = self.backbone_network_name
        return result
