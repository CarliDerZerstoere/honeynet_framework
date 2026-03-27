"""
Detection module — LLM agent / automated actor detection for honeynets.

Provides:
- DetectorResult: structured 3-zone decision (allow/review/likely_agent)
- DetectorConfig: threshold configuration with versioning
- BaseDetector: abstract interface for detector implementations
- MultiSignalDetector: weighted linear score detector
"""

from .result import Decision, DetectorResult
from .config import DetectorConfig
from .base import BaseDetector
from .multi_signal import MultiSignalDetector

__all__ = [
    "Decision",
    "DetectorResult",
    "DetectorConfig",
    "BaseDetector",
    "MultiSignalDetector",
]
