"""
Structured 3-zone detector result.

Replaces binary classification with allow / review / likely_agent.
Every result carries a score, decision, reasons, and threshold metadata
for full auditability.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


class Decision(str, Enum):
    """Three-zone decision output from the detector."""

    ALLOW = "allow"
    REVIEW = "review"
    LIKELY_AGENT = "likely_agent"


@dataclass
class DetectorResult:
    """Immutable result of a detection analysis.

    Attributes:
        score: Raw detector score (0.0 = certainly human, 1.0 = certainly agent).
        decision: Three-zone classification derived from score + thresholds.
        reasons: Human-readable list of contributing factors.
        feature_contributions: Per-feature score breakdowns for explainability.
        thresholds_used: The threshold config version that produced this decision.
        run_id: Optional correlation to a specific pipeline run.
    """

    score: float = 0.0
    decision: Decision = Decision.ALLOW
    reasons: list[str] = field(default_factory=list)
    feature_contributions: dict[str, float] = field(default_factory=dict)
    thresholds_used: str = "v0"
    t_allow: float = 0.30
    t_agent: float = 0.70
    run_id: str = ""

    def __post_init__(self) -> None:
        import logging as _logging
        import math
        if math.isnan(self.score) or math.isinf(self.score):
            _logging.getLogger(__name__).warning(
                "DetectorResult received invalid score %r — defaulting to 0.5",
                self.score,
            )
            self.score = 0.5  # Conservative neutral score for invalid inputs
        else:
            self.score = max(0.0, min(1.0, self.score))

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["decision"] = self.decision.value
        return d

    @classmethod
    def from_dict(cls, raw: Any) -> "DetectorResult":
        """Defensive parse from serialized form."""
        if not isinstance(raw, dict):
            return cls()
        decision_str = raw.get("decision", "allow")
        try:
            decision = Decision(decision_str)
        except ValueError:
            decision = Decision.ALLOW
        try:
            score_val = float(raw.get("score", 0.0))
        except (TypeError, ValueError):
            score_val = 0.0
        # Build the result without a decision first so __post_init__ clamps
        # the score, then derive the decision from the clamped score to ensure
        # score and decision are always consistent (the serialised decision may
        # have been produced with different thresholds).
        try:
            t_allow = float(raw.get("t_allow", 0.30))
        except (TypeError, ValueError):
            t_allow = 0.30
        try:
            t_agent = float(raw.get("t_agent", 0.70))
        except (TypeError, ValueError):
            t_agent = 0.70
        inst = cls(
            score=score_val,
            decision=Decision.ALLOW,  # placeholder — overwritten below
            reasons=list(raw.get("reasons", [])),
            feature_contributions=dict(raw.get("feature_contributions", {})),
            thresholds_used=str(raw.get("thresholds_used", "v0")),
            t_allow=t_allow,
            t_agent=t_agent,
            run_id=str(raw.get("run_id", "")),
        )
        # Re-derive decision from the score using the thresholds that were
        # stored alongside the result (survives round-trip correctly).
        if inst.score < t_allow:
            inst.decision = Decision.ALLOW
        elif inst.score >= t_agent:
            inst.decision = Decision.LIKELY_AGENT
        else:
            inst.decision = Decision.REVIEW
        return inst

    @property
    def is_suspicious(self) -> bool:
        """True if decision is review or likely_agent."""
        return self.decision in (Decision.REVIEW, Decision.LIKELY_AGENT)

    @property
    def is_likely_agent(self) -> bool:
        return self.decision == Decision.LIKELY_AGENT
