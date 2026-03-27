"""Repair history — session-scoped JSONL tracking of all repair decisions.

Each RepairDecision is serialised as a single JSON line and appended to
``<work_dir>/repair_history.jsonl`` alongside other run artifacts.

The file can be post-processed with standard tools:
    jq '.' output/repair_history.jsonl

Usage
-----
    history = RepairHistory(run_id="abc123", work_dir=Path("output"))
    history.record(decision)   # appends one JSON line
    history.finalize()         # writes a summary entry (total attempts etc.)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .repair_types import RepairDecision

logger = logging.getLogger(__name__)


class RepairHistory:
    """Append-only JSONL writer for repair decisions.

    Parameters
    ----------
    run_id:
        Unique identifier for the current deploy run.
    work_dir:
        Directory where the ``repair_history.jsonl`` file is written.
        Must exist (orchestrator creates it before using this class).
    """

    FILENAME = "repair_history.jsonl"

    def __init__(self, run_id: str, work_dir: Path) -> None:
        self.run_id = run_id
        self._path = work_dir / self.FILENAME
        self._records: list[RepairDecision] = []
        self._finalized: bool = False

    def record(self, decision: RepairDecision) -> None:
        """Append a single repair decision to the JSONL file."""
        try:
            line = json.dumps(self._serialise(decision), ensure_ascii=False)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            # Append to in-memory list only after successful disk write
            # to keep in-memory and on-disk state consistent.
            self._records.append(decision)
            logger.debug(
                "RepairHistory: wrote attempt %d to %s",
                decision.attempt, self._path,
            )
        except Exception as exc:
            # Do NOT append to in-memory list on disk write failure
            # to keep in-memory and on-disk state consistent.
            logger.warning("RepairHistory: disk write failed (not recorded in-memory): %s", exc)

    def any_loop_detected(self) -> bool:
        """Return True if any recorded decision has loop_detected=True."""
        return any(r.loop_detected for r in self._records)

    def finalize(
        self,
        total_attempts: int,
        succeeded: bool,
        loop_detected: bool = False,
    ) -> None:
        """Append a summary entry at the end of the run.

        Idempotent — calling finalize() more than once is a no-op after the
        first call to prevent duplicate summary records in the JSONL file.
        """
        if self._finalized:
            logger.debug("RepairHistory.finalize() called again — skipping duplicate summary")
            return
        self._finalized = True
        # Count on-disk records for THIS run only.  The JSONL file may contain
        # entries from prior runs if the same work_dir is reused.
        disk_count = 0
        try:
            if self._path.exists():
                disk_count = sum(
                    1 for line in self._path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                    and '"_type": "repair_decision"' in line
                    and f'"run_id": "{self.run_id}"' in line
                )
        except Exception:
            disk_count = len(self._records)  # fallback to in-memory count
        summary = {
            "_type": "summary",
            "run_id": self.run_id,
            "total_repair_attempts": total_attempts,
            "succeeded": succeeded,
            "loop_detected": loop_detected,
            "decision_count": disk_count,
        }
        try:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(summary, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.warning("RepairHistory: failed to write summary: %s", exc)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _serialise(decision: RepairDecision) -> dict:
        """Convert a RepairDecision to a JSON-serialisable dict."""
        d = {
            "_type": "repair_decision",
            "run_id": decision.run_id,
            "attempt": decision.attempt,
            "timestamp": decision.timestamp,
            "loop_detected": decision.loop_detected,
            "loop_reason": decision.loop_reason,
            "apply_succeeded_after": decision.apply_succeeded_after,
            "failing_containers": [
                {
                    "system_name": fc.system_name,
                    "image": fc.image,
                    "zone": fc.zone,
                    "role": fc.role,
                    "failure_type": fc.failure_type.value,
                    "diagnosis": fc.diagnosis,
                    "raw_error": (fc.raw_error or "")[:500],
                }
                for fc in decision.failing_containers
            ],
            "proposals": [
                {
                    "system_name": p.system_name,
                    "original_image": p.original_image,
                    "proposed_image": p.proposed_image,
                    "score": p.score,
                    "score_reason": p.score_reason,
                    "applied": p.applied,
                    "skip_reason": p.skip_reason,
                }
                for p in decision.proposals
            ],
        }
        return d
