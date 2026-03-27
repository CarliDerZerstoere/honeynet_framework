"""
Telemetry Feature Schema v2 — structured interaction features for session analysis.

Provides:
- InteractionFeature: per-event feature record with defensive defaults
- SessionSnapshot: aggregate features computed over a session's events
- Defensive parsing: missing/malformed fields never crash

Schema-versioned and backward-compatible with telemetry_events v1.
"""

from __future__ import annotations

import json
import logging
import statistics
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

FEATURE_SCHEMA_VERSION = 1

# --------------------------------------------------------------------------- #
# Per-event feature record
# --------------------------------------------------------------------------- #

# Canonical action families — extensible via "other".
ACTION_FAMILIES = frozenset({
    "navigate", "interact", "exfiltrate", "enumerate",
    "authenticate", "deploy", "configure", "observe", "other",
})

# Canonical source classes for event origin.
SOURCE_CLASSES = frozenset({
    "human", "automated", "replay", "unknown",
})


@dataclass
class InteractionFeature:
    """Structured feature record attached to a single telemetry event.

    All fields have safe defaults so that partial/missing data never
    crashes downstream consumers.
    """

    event_type: str = "unknown"
    latency_s: float = -1.0        # -1 = not measured
    timestamp_s: float = -1.0      # epoch seconds; -1 = not available
    lure_followed: bool = False
    command_hash: str = ""          # deterministic hash of the action
    action_family: str = "other"    # must be in ACTION_FAMILIES
    source_class: str = "unknown"   # must be in SOURCE_CLASSES
    confidence_hint: float = 0.0    # 0.0–1.0 producer confidence

    def __post_init__(self) -> None:
        if self.action_family not in ACTION_FAMILIES:
            self.action_family = "other"
        if self.source_class not in SOURCE_CLASSES:
            self.source_class = "unknown"
        self.confidence_hint = max(0.0, min(1.0, self.confidence_hint))

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["schema_version"] = FEATURE_SCHEMA_VERSION
        return d

    @classmethod
    def from_dict(cls, raw: Any) -> "InteractionFeature":
        """Defensive parse — unknown/missing keys use defaults."""
        if not isinstance(raw, dict):
            return cls()
        kwargs: dict[str, Any] = {}
        for fld in cls.__dataclass_fields__:
            if fld in raw:
                kwargs[fld] = raw[fld]
        try:
            return cls(**kwargs)
        except (TypeError, ValueError):
            return cls()


# --------------------------------------------------------------------------- #
# Session-level aggregate snapshot
# --------------------------------------------------------------------------- #

@dataclass
class SessionSnapshot:
    """Aggregate feature snapshot computed over all events in a session.

    Designed so that the detector can operate on a single object
    without re-parsing raw JSONL.
    """

    run_id: str = ""
    schema_version: int = FEATURE_SCHEMA_VERSION

    # Counts
    event_count: int = 0
    lure_hit_count: int = 0

    # Ratios (0.0–1.0)
    fast_action_ratio: float = 0.0     # events with latency < threshold
    lure_hit_ratio: float = 0.0
    repeat_pattern_ratio: float = 0.0  # fraction of repeated command_hash

    # Timing
    median_latency_s: float = -1.0
    stddev_latency_s: float = -1.0
    min_latency_s: float = -1.0
    max_latency_s: float = -1.0

    # Action distribution
    action_family_counts: dict[str, int] = field(default_factory=dict)
    source_class_counts: dict[str, int] = field(default_factory=dict)

    # Detector-relevant derived features
    inter_action_jitter: float = -1.0       # stddev of inter-event gaps
    trajectory_linearity: float = 0.0       # 0=random, 1=perfectly sequential

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Any) -> "SessionSnapshot":
        if not isinstance(raw, dict):
            return cls()
        kwargs: dict[str, Any] = {}
        for fld in cls.__dataclass_fields__:
            if fld in raw:
                kwargs[fld] = raw[fld]
        try:
            return cls(**kwargs)
        except (TypeError, ValueError):
            return cls()


# --------------------------------------------------------------------------- #
# Session feature computation
# --------------------------------------------------------------------------- #

_FAST_LATENCY_THRESHOLD_S = 0.5  # events faster than this are "fast"


def compute_session_snapshot(
    run_id: str,
    features: list[InteractionFeature],
    *,
    fast_threshold_s: float = _FAST_LATENCY_THRESHOLD_S,
) -> SessionSnapshot:
    """Compute a SessionSnapshot from a list of per-event features.

    Pure function — no I/O, no side-effects, no exceptions on bad data.
    """
    snap = SessionSnapshot(run_id=run_id)
    n = len(features)
    snap.event_count = n
    if n == 0:
        return snap

    # Latency stats (ignore -1 = unmeasured)
    latencies = [f.latency_s for f in features if f.latency_s >= 0]
    if latencies:
        snap.median_latency_s = round(statistics.median(latencies), 4)
        snap.min_latency_s = round(min(latencies), 4)
        snap.max_latency_s = round(max(latencies), 4)
        snap.stddev_latency_s = round(statistics.stdev(latencies), 4) if len(latencies) > 1 else 0.0
        snap.fast_action_ratio = round(
            sum(1 for lat in latencies if lat < fast_threshold_s) / len(latencies), 4
        )

    # Lure hits
    snap.lure_hit_count = sum(1 for f in features if f.lure_followed)
    snap.lure_hit_ratio = round(snap.lure_hit_count / n, 4)

    # Repeat patterns (command_hash collisions)
    hashes = [f.command_hash for f in features if f.command_hash]
    if hashes:
        from collections import Counter
        counts = Counter(hashes)
        repeated = sum(c - 1 for c in counts.values() if c > 1)
        snap.repeat_pattern_ratio = round(repeated / len(hashes), 4)

    # Action family & source class distribution
    for f in features:
        snap.action_family_counts[f.action_family] = (
            snap.action_family_counts.get(f.action_family, 0) + 1
        )
        snap.source_class_counts[f.source_class] = (
            snap.source_class_counts.get(f.source_class, 0) + 1
        )

    # Inter-action jitter (stddev of time gaps between consecutive events).
    # Uses actual event timestamps (not per-action response times) so the
    # metric reflects the regularity of *when* actions happen — a low jitter
    # means actions are suspiciously evenly spaced (automated), high jitter
    # means irregular timing (more likely human).
    timestamps = sorted(f.timestamp_s for f in features if f.timestamp_s >= 0)
    if len(timestamps) > 2:
        gaps = [timestamps[i + 1] - timestamps[i] for i in range(len(timestamps) - 1)]
        snap.inter_action_jitter = round(statistics.stdev(gaps), 4) if len(gaps) > 1 else 0.0
    elif len(latencies) > 2:
        # Fallback for legacy data without timestamps: approximate inter-event
        # jitter from latency data by computing stddev of consecutive latency
        # differences (simulates timing gap variation, not response-time spread).
        lat_diffs = [abs(latencies[i + 1] - latencies[i]) for i in range(len(latencies) - 1)]
        snap.inter_action_jitter = round(statistics.stdev(lat_diffs), 4) if len(lat_diffs) > 1 else 0.0

    # Trajectory linearity (fraction of events in the most common action_family)
    if snap.action_family_counts:
        most_common_count = max(snap.action_family_counts.values())
        snap.trajectory_linearity = round(most_common_count / n, 4)

    return snap


# --------------------------------------------------------------------------- #
# JSONL reader with defensive parsing
# --------------------------------------------------------------------------- #

def read_session_features(work_dir: Path, run_id: Optional[str] = None) -> list[InteractionFeature]:
    """Read interaction features from telemetry JSONL.

    Parses the ``extra.features`` field from each event line.
    Lines without features or with parse errors are silently skipped.
    If *run_id* is given, only events matching that run are returned.
    """
    from .telemetry_events import telemetry_path
    path = telemetry_path(work_dir)
    if not path.exists():
        return []

    features: list[InteractionFeature] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            if record.get("kind") == "honeynet_telemetry_header":
                continue
            if run_id and record.get("run_id") != run_id:
                continue
            extra = record.get("extra")
            if isinstance(extra, dict) and "features" in extra:
                feat = InteractionFeature.from_dict(extra["features"])
                # Inject event timestamp if the feature doesn't carry one
                if feat.timestamp_s < 0:
                    ts_ms = record.get("timestamp_ms")
                    if isinstance(ts_ms, (int, float)) and ts_ms > 0:
                        feat.timestamp_s = ts_ms / 1000.0
                features.append(feat)
    except OSError as e:
        logger.warning("Failed to read telemetry features: %s", e)

    return features
