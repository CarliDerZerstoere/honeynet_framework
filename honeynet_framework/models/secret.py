"""
Secret models - NEVER deployed, only in artifacts/simulation.
"""

from dataclasses import dataclass, field


@dataclass
class SecretSimulate:
    """Simulation properties for secrets."""
    strength: str = "medium"
    age_days: int = 0
    reused_by: list[str] = field(default_factory=list)
    leaked_in: list[str] = field(default_factory=list)
    known_to: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "strength": self.strength,
            "age_days": self.age_days,
            "reused_by": self.reused_by,
            "leaked_in": self.leaked_in,
            "known_to": self.known_to,
        }


@dataclass
class Secret:
    """
    Secret/honeytoken definition - NEVER deployed.

    Exists only in artifacts, logs, and LLM interaction.
    """
    name: str
    type: str
    value: str = ""
    simulate: SecretSimulate = field(default_factory=SecretSimulate)

    def to_dict(self) -> dict:
        d: dict = {
            "name": self.name,
            "type": self.type,
            "simulate": self.simulate.to_dict(),
        }
        # Deliberately exclude 'value' from serialization — secrets must
        # NEVER be persisted to disk artifacts.  The value is only held
        # in memory for runtime use.
        return d
