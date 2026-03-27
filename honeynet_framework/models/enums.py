"""
Shared enums for the Honeynet Framework.
"""

from enum import Enum


class DeploymentStatus(Enum):
    """Status of the honeynet deployment."""
    PENDING = "pending"
    PLANNING = "planning"
    DEPLOYING = "deploying"
    DEPLOYED = "deployed"
    RUNTIME_DEGRADED = "runtime_degraded"
    PARTIAL = "partial"
    FAILED = "failed"


class SystemKind(str, Enum):
    """Structural categories for systems."""
    WEB = "web"
    DATABASE = "database"
    QUEUE = "queue"
    RUNTIME = "runtime"
    STORAGE = "storage"
    IDENTITY = "identity"
    MONITOR = "monitor"
    INFRA = "infra"
    UNKNOWN = "unknown"

    @classmethod
    def coerce(cls, value: str) -> "SystemKind":
        normalized = str(value or "").strip().lower()
        try:
            return cls(normalized)
        except ValueError:
            return cls.UNKNOWN
