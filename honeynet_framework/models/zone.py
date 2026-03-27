"""
Zone models - networks with deploy/simulate split.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class ZoneDeploy:
    """Deploy-time zone/network settings."""
    network_name: str
    driver: str = "bridge"
    internal: bool = True
    subnet: str = ""  # Optional CIDR, e.g. "172.30.1.0/24"


@dataclass
class ZoneSimulate:
    """Simulation-time zone properties."""
    exposure: str = "internal"
    sensitivity: str = "medium"
    trust_level: str = "medium"

    def to_dict(self) -> dict:
        return {
            "exposure": self.exposure,
            "sensitivity": self.sensitivity,
            "trust_level": self.trust_level,
        }


@dataclass
class WorldZone:
    """
    Zone definition with deploy/simulate split.
    """
    name: str
    deploy: ZoneDeploy
    simulate: Optional[ZoneSimulate] = None

    def to_dict(self) -> dict:
        deploy_dict = {
            "network_name": self.deploy.network_name,
            "driver": self.deploy.driver,
            "internal": self.deploy.internal,
        }
        if self.deploy.subnet:
            deploy_dict["subnet"] = self.deploy.subnet
        result = {
            "name": self.name,
            "deploy": deploy_dict,
        }
        if self.simulate:
            result["simulate"] = self.simulate.to_dict()
        return result
