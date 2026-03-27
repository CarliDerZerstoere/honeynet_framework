"""
Abstract base class for detectors.

Provides the interface contract and a legacy compatibility adapter
that maps 3-zone results to boolean for callers that only need is/isn't agent.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..telemetry_features import SessionSnapshot
from .config import DetectorConfig
from .result import Decision, DetectorResult


class BaseDetector(ABC):
    """Interface for session-level detectors.

    Subclasses implement ``analyze`` to produce a ``DetectorResult``
    with score, decision, and reasons.
    """

    def __init__(self, config: DetectorConfig | None = None) -> None:
        self.config = config or DetectorConfig()

    @abstractmethod
    def analyze(self, snapshot: SessionSnapshot) -> DetectorResult:
        """Analyze a session snapshot and return a structured result."""
        ...

    # ------------------------------------------------------------------ #
    # Legacy compatibility layer
    # ------------------------------------------------------------------ #

    def is_agent(self, snapshot: SessionSnapshot) -> bool:
        """Legacy boolean interface — True when decision is LIKELY_AGENT."""
        return self.analyze(snapshot).is_likely_agent

    def classify(self, snapshot: SessionSnapshot) -> str:
        """Legacy string interface — returns decision value string."""
        return self.analyze(snapshot).decision.value
