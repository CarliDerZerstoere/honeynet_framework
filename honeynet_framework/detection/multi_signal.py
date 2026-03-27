"""
Multi-Signal Detector v1 — weighted linear score with clamping.

Uses 6 features from SessionSnapshot:
1. fast_action_ratio    — high → more agent-like
2. lure_hit_ratio       — high → more agent-like
3. repeat_pattern_ratio — high → more agent-like
4. inter_action_jitter  — LOW jitter → more agent-like (bots are regular)
5. trajectory_linearity — HIGH → more agent-like (bots follow patterns)
6. latency_uniformity   — derived from stddev — LOW stddev → more agent-like

Score is clamped to [0.0, 1.0], then mapped to 3-zone decision.
All weights are documented, manually calibrated, no ML black-box.

Also provides EnsembleDetector — aggregates multiple BaseDetector instances
via weighted voting into a single DetectorResult.
"""

from __future__ import annotations

import logging
from typing import Optional, Sequence

from ..telemetry_features import SessionSnapshot
from .base import BaseDetector
from .config import DetectorConfig
from .result import Decision, DetectorResult

logger = logging.getLogger(__name__)

# Minimum events needed for a reliable analysis.
_MIN_EVENTS_FOR_ANALYSIS = 5


class MultiSignalDetector(BaseDetector):
    """Weighted linear multi-signal detector with explainable scoring.

    For sessions with fewer than ``_MIN_EVENTS_FOR_ANALYSIS`` events,
    returns ALLOW with an "insufficient evidence" reason.
    """

    def __init__(self, config: Optional[DetectorConfig] = None) -> None:
        super().__init__(config)

    def analyze(self, snapshot: SessionSnapshot) -> DetectorResult:
        """Analyze session snapshot and return 3-zone decision."""
        reasons: list[str] = []
        contributions: dict[str, float] = {}

        if snapshot.event_count < _MIN_EVENTS_FOR_ANALYSIS:
            return DetectorResult(
                score=0.0,
                decision=Decision.ALLOW,
                reasons=[
                    f"Insufficient evidence: {snapshot.event_count} events "
                    f"(need >= {_MIN_EVENTS_FOR_ANALYSIS})"
                ],
                feature_contributions={},
                thresholds_used=self.config.version,
                t_allow=self.config.t_allow,
                t_agent=self.config.t_agent,
                run_id=snapshot.run_id,
            )

        cfg = self.config
        raw_score = 0.0

        # --- Feature 1: fast_action_ratio ---
        fa = snapshot.fast_action_ratio
        weighted_fa = fa * cfg.fast_action_weight
        contributions["fast_action_ratio"] = round(weighted_fa, 4)
        raw_score += weighted_fa
        if fa > 0.8:
            reasons.append(f"Very fast actions ({fa:.0%} under threshold)")

        # --- Feature 2: lure_hit_ratio ---
        lh = snapshot.lure_hit_ratio
        weighted_lh = lh * cfg.lure_hit_weight
        contributions["lure_hit_ratio"] = round(weighted_lh, 4)
        raw_score += weighted_lh
        if lh > 0.5:
            reasons.append(f"High lure follow rate ({lh:.0%})")

        # --- Feature 3: repeat_pattern_ratio ---
        rp = snapshot.repeat_pattern_ratio
        weighted_rp = rp * cfg.repeat_pattern_weight
        contributions["repeat_pattern_ratio"] = round(weighted_rp, 4)
        raw_score += weighted_rp
        if rp > 0.6:
            reasons.append(f"Repeated command patterns ({rp:.0%})")

        # --- Feature 4: inter_action_jitter (inverse — low jitter = agent) ---
        if snapshot.inter_action_jitter >= 0:
            # Normalize: jitter=0 → signal=1.0 (max agent), jitter>=2.0 → signal=0.0
            jitter_signal = max(0.0, 1.0 - snapshot.inter_action_jitter / 2.0)
        else:
            jitter_signal = 0.0  # unmeasured → neutral
        weighted_jitter = jitter_signal * cfg.jitter_weight
        contributions["inter_action_jitter"] = round(weighted_jitter, 4)
        raw_score += weighted_jitter
        if jitter_signal > 0.8:
            reasons.append(f"Very uniform timing (jitter={snapshot.inter_action_jitter:.3f}s)")

        # --- Feature 5: trajectory_linearity ---
        tl = snapshot.trajectory_linearity
        weighted_tl = tl * cfg.linearity_weight
        contributions["trajectory_linearity"] = round(weighted_tl, 4)
        raw_score += weighted_tl
        if tl > 0.9:
            reasons.append(f"Highly linear action trajectory ({tl:.0%})")

        # --- Feature 6: latency_uniformity (inverse of stddev) ---
        if snapshot.stddev_latency_s >= 0 and snapshot.median_latency_s > 0:
            # CV (coefficient of variation) as uniformity proxy
            cv = snapshot.stddev_latency_s / max(snapshot.median_latency_s, 0.01)
            uniformity_signal = max(0.0, 1.0 - cv)
        else:
            uniformity_signal = 0.0  # unmeasured → neutral
        weighted_uniform = uniformity_signal * cfg.latency_uniformity_weight
        contributions["latency_uniformity"] = round(weighted_uniform, 4)
        raw_score += weighted_uniform
        if uniformity_signal > 0.8:
            reasons.append("Very uniform latency distribution")

        # Clamp final score
        score = max(0.0, min(1.0, raw_score))

        # Map to decision zone
        if score < cfg.t_allow:
            decision = Decision.ALLOW
        elif score < cfg.t_agent:
            decision = Decision.REVIEW
        else:
            decision = Decision.LIKELY_AGENT

        if not reasons:
            if decision == Decision.ALLOW:
                reasons.append("No significant automated-actor signals detected")
            else:
                reasons.append(
                    f"Aggregate score {score:.2f} exceeded threshold "
                    f"(t_allow={cfg.t_allow}, t_agent={cfg.t_agent})"
                )

        return DetectorResult(
            score=round(score, 4),
            decision=decision,
            reasons=reasons,
            feature_contributions=contributions,
            thresholds_used=cfg.version,
            t_allow=cfg.t_allow,
            t_agent=cfg.t_agent,
            run_id=snapshot.run_id,
        )


class EnsembleDetector(BaseDetector):
    """Aggregate multiple BaseDetector instances via weighted scoring.

    Each child detector's score is multiplied by its weight, then summed and
    normalised to [0.0, 1.0].  Thresholds from the *first* detector's config
    (or the explicit ``config`` argument) are used for the final decision.

    Reasons from all child detectors are collected and prefixed with the
    detector class name for traceability.

    Parameters
    ----------
    detectors:
        Sequence of ``(detector, weight)`` pairs.  Weights need not sum to 1 —
        they are normalised automatically.  Negative weights are treated as 0.
    config:
        Optional threshold config for the final decision mapping.  If None,
        the config of the first detector is used.
    """

    def __init__(
        self,
        detectors: Sequence[tuple[BaseDetector, float]],
        config: Optional[DetectorConfig] = None,
    ) -> None:
        if not detectors:
            raise ValueError("EnsembleDetector requires at least one child detector")
        first_config = config or detectors[0][0].config
        super().__init__(first_config)
        # Normalise weights (clamp negatives to 0, guard zero-sum)
        raw_weights = [max(0.0, w) for _, w in detectors]
        weight_sum = sum(raw_weights)
        if weight_sum == 0:
            logger.warning(
                "EnsembleDetector: all weights are zero after clamping negatives "
                "— using uniform weights (1/N) so every detector contributes equally"
            )
            raw_weights = [1.0] * len(detectors)
            weight_sum = float(len(detectors))
        self._members: list[tuple[BaseDetector, float]] = [
            (det, w / weight_sum) for (det, _), w in zip(detectors, raw_weights)
        ]

    def analyze(self, snapshot: SessionSnapshot) -> DetectorResult:
        """Run all child detectors and combine results via weighted average."""
        aggregate_score = 0.0
        all_reasons: list[str] = []
        merged_contributions: dict[str, float] = {}
        run_id = snapshot.run_id

        for detector, weight in self._members:
            result = detector.analyze(snapshot)
            aggregate_score += result.score * weight
            label = type(detector).__name__
            for reason in result.reasons:
                all_reasons.append(f"[{label}] {reason}")
            for feature, contribution in result.feature_contributions.items():
                key = f"{label}.{feature}"
                # Multiply by ensemble weight so contributions reflect actual
                # influence on the final aggregate score.
                merged_contributions[key] = round(contribution * weight, 4)

        score = max(0.0, min(1.0, aggregate_score))
        cfg = self.config
        if score < cfg.t_allow:
            decision = Decision.ALLOW
        elif score < cfg.t_agent:
            decision = Decision.REVIEW
        else:
            decision = Decision.LIKELY_AGENT

        if not all_reasons and decision == Decision.ALLOW:
            all_reasons.append("Ensemble: no significant automated-actor signals detected")

        return DetectorResult(
            score=round(score, 4),
            decision=decision,
            reasons=all_reasons,
            feature_contributions=merged_contributions,
            thresholds_used=cfg.version,
            t_allow=cfg.t_allow,
            t_agent=cfg.t_agent,
            run_id=run_id,
        )
