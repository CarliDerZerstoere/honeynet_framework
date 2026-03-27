"""
Detector threshold configuration with versioning.

Thresholds define the boundaries between the three zones:
  score < t_allow    → ALLOW
  t_allow <= score < t_agent → REVIEW
  score >= t_agent   → LIKELY_AGENT

Config is versioned so that DetectorResult always records which
thresholds produced the decision — essential for reproducibility.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any


@dataclass(frozen=True)
class DetectorConfig:
    """Immutable threshold configuration for the 3-zone detector.

    Attributes:
        version: Monotonic version string (e.g. "v1", "v2").
        t_allow: Upper bound for ALLOW zone (exclusive).
        t_agent: Lower bound for LIKELY_AGENT zone (inclusive).
        feature_weights: Named weights for multi-signal scoring.
    """

    version: str = "v1"
    t_allow: float = 0.30
    t_agent: float = 0.70

    # Feature weights (name → weight).  Must sum to ~1.0 for normalized scoring.
    # Weights are documented and manually calibrated — no ML auto-tuning in v1.
    fast_action_weight: float = 0.20
    lure_hit_weight: float = 0.25
    repeat_pattern_weight: float = 0.15
    jitter_weight: float = 0.15
    linearity_weight: float = 0.10
    latency_uniformity_weight: float = 0.15

    def __post_init__(self) -> None:
        if self.t_allow >= self.t_agent:
            raise ValueError(
                f"t_allow ({self.t_allow}) must be < t_agent ({self.t_agent})"
            )
        weight_sum = (
            self.fast_action_weight
            + self.lure_hit_weight
            + self.repeat_pattern_weight
            + self.jitter_weight
            + self.linearity_weight
            + self.latency_uniformity_weight
        )
        if abs(weight_sum - 1.0) > 0.001:
            raise ValueError(
                f"Feature weights must sum to ~1.0 (tolerance 0.001), got {weight_sum:.4f}"
            )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Any) -> "DetectorConfig":
        if not isinstance(raw, dict):
            return cls()
        kwargs = {}
        for fld in cls.__dataclass_fields__:
            if fld in raw:
                kwargs[fld] = raw[fld]
        try:
            return cls(**kwargs)
        except (TypeError, ValueError) as exc:
            import logging
            logging.getLogger(__name__).warning(
                "DetectorConfig.from_dict: invalid config (%s) — falling back to defaults", exc,
            )
            return cls()
