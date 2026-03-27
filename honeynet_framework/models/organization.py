"""
Organization models - purely semantic, never deployed.
"""

from dataclasses import dataclass, field


@dataclass
class BusinessUnit:
    """A logical business unit in the organization."""
    id: str
    name: str
    description: str = ""

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "description": self.description}


@dataclass
class Organization:
    """
    Organizational context - purely semantic, never deployed.

    Used for narrative, artifacts, logs, and LLM context.
    """
    name: str
    type: str = ""
    regions: list[str] = field(default_factory=list)
    business_units: list[BusinessUnit] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "type": self.type,
            "regions": self.regions,
            "business_units": [bu.to_dict() for bu in self.business_units],
        }
