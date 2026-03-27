"""Shared dataclasses and enums for the repair pipeline.

These types are the single source of truth used by:
  - failure_analyzer  (classifies errors)
  - repair_scorer     (scores proposals)
  - loop_guard        (detects oscillation)
  - repair_history    (session-scoped JSONL tracking)
  - orchestrator      (wires everything together)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------

class FailureType(str, Enum):
    """High-level category of a container failure.

    Used by the FailureAnalyzer and RepairScorer to decide how confident we
    are that a particular repair proposal will fix the problem.
    """
    UNKNOWN = "unknown"
    IMAGE_NOT_FOUND = "image_not_found"       # 404 / manifest unknown
    IMAGE_PULL_RATE_LIMITED = "rate_limited"  # too many pulls from Docker Hub
    CONTAINER_EXIT_IMMEDIATE = "exit_immediate"  # started, exited ≤1 s
    BAD_COMMAND = "bad_command"               # exec format error / no such file
    MISSING_ENV = "missing_env"               # required env-var not set
    PORT_CONFLICT = "port_conflict"           # bind: address already in use
    DEPENDENCY_NOT_READY = "dep_not_ready"    # upstream container not yet up
    OOM = "oom"                               # out-of-memory kill
    PERMISSION_DENIED = "permission"          # file/socket permission error
    LOOP_DETECTED = "loop_detected"           # same image proposed ≥2 cycles


# ---------------------------------------------------------------------------
# Per-container failure context (enriched by FailureAnalyzer)
# ---------------------------------------------------------------------------

@dataclass
class FailureContext:
    """All diagnostic information about a single failing container.

    Produced by ``failure_analyzer.enrich_failing_list()`` and passed to the
    LLM repair prompt and RepairScorer.
    """
    system_name: str
    image: str
    command: Optional[list[str]]
    zone: str
    role: str

    # Raw error string from tofu apply stderr
    raw_error: str = ""
    # Docker logs captured by startup probe (may be empty)
    docker_logs: str = ""

    # Classified failure type (set by FailureAnalyzer)
    failure_type: FailureType = FailureType.UNKNOWN

    # Human-readable diagnosis produced by FailureAnalyzer
    diagnosis: str = ""

    # Which repair attempt this is (0-indexed)
    attempt: int = 0

    # Extra metadata (free-form, for future use)
    meta: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Repair proposal (one LLM suggestion for a single container)
# ---------------------------------------------------------------------------

@dataclass
class RepairProposal:
    """A single repair suggestion from the LLM for one failing container."""
    system_name: str
    original_image: str
    proposed_image: str
    proposed_command: Optional[list[str]]

    # Score from RepairScorer (0.0–1.0); None = not yet scored
    score: Optional[float] = None

    # Reason the scorer assigned this score
    score_reason: str = ""

    # Whether this proposal was applied (True) or skipped (False/None=pending)
    applied: Optional[bool] = None

    # If skipped, the reason
    skip_reason: str = ""


# ---------------------------------------------------------------------------
# Repair decision record (one full repair cycle)
# ---------------------------------------------------------------------------

@dataclass
class RepairDecision:
    """Summary of a single repair cycle (one iteration of the apply-repair loop).

    Stored in RepairHistory and emitted as a structured log entry.
    """
    run_id: str
    attempt: int                              # 0-indexed repair attempt number
    failing_containers: list[FailureContext]  # enriched contexts going IN
    proposals: list[RepairProposal]           # what the LLM proposed
    loop_detected: bool = False
    loop_reason: str = ""
    apply_succeeded_after: bool = False       # did the subsequent apply pass?

    # ISO-8601 timestamp when this decision was recorded
    timestamp: str = ""


# ---------------------------------------------------------------------------
# Pre-compile validation issue
# ---------------------------------------------------------------------------

class IssueSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass
class PreCompileIssue:
    """A structural problem found by the pre-compile validator.

    Errors block compilation; warnings are logged only.
    """
    severity: IssueSeverity
    system_name: Optional[str]   # None = world-model-level issue
    field: str                   # e.g. "deploy.image", "deploy.zone"
    message: str
    suggestion: str = ""
