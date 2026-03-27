"""Loop guard — detects oscillating repair proposals in the apply-repair loop.

Problem: the LLM sometimes alternates between two broken images
(A → B → A → B …) producing an infinite loop with no progress.

Detection strategies
--------------------
image_repeat
    A proposed image was already tried and failed in an earlier attempt.

no_change
    The proposed image is identical to the one that just failed.

hybrid (default)
    Either of the above.

Usage
-----
    guard = LoopGuard(mode="hybrid")
    guard.record_contexts(failure_contexts)     # before LLM call
    # ... call LLM, collect proposals ...
    detected, reason = guard.check(proposals, attempt=1)   # check BEFORE recording
    if detected:
        logger.warning("Loop detected: %s", reason)
    guard.record_proposals(proposals)           # record AFTER check, only if not detected
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .repair_types import FailureContext, RepairProposal

logger = logging.getLogger(__name__)


@dataclass
class LoopGuard:
    """Tracks per-system image history across repair attempts.

    Parameters
    ----------
    mode:
        ``"image_repeat"``, ``"no_change"``, or ``"hybrid"`` (default).
    min_attempts:
        Don't flag a loop until at least this many attempts have been made.
        Defaults to 1 so the second attempt can already trigger detection.
    """

    mode: str = "hybrid"
    min_attempts: int = 1
    max_failures_per_system: int = 3  # flag loop after N failures regardless of image

    _VALID_MODES = frozenset({"hybrid", "image_repeat", "no_change"})

    def __post_init__(self) -> None:
        if self.mode not in self._VALID_MODES:
            raise ValueError(
                f"Invalid LoopGuard mode {self.mode!r} — must be one of {sorted(self._VALID_MODES)}"
            )

    # system_name → list of images that actually *failed* (from record_contexts)
    _failed: dict[str, list[str]] = field(default_factory=dict, init=False, repr=False)
    # system_name → list of images *proposed* by the LLM (from record_proposals)
    _proposed: dict[str, list[str]] = field(default_factory=dict, init=False, repr=False)

    def reset(self) -> None:
        """Clear all recorded history.  Call between independent repair cycles."""
        self._failed.clear()
        self._proposed.clear()

    def record_contexts(self, contexts: list[FailureContext]) -> None:
        """Record all images from a failing-context list (before the LLM call)."""
        for ctx in contexts:
            self._failed.setdefault(ctx.system_name, []).append(ctx.image)

    def check(
        self,
        proposals: list[RepairProposal],
        attempt: int,
    ) -> tuple[bool, str]:
        """Check whether the proposals look like a loop.

        Must be called BEFORE ``record_proposals`` so that the proposed images
        are not yet in the history when we check for repeats.

        Returns
        -------
        (loop_detected: bool, reason: str)
        """
        if attempt < self.min_attempts:
            return False, ""

        repeat_systems: list[str] = []
        no_change_systems: list[str] = []
        exhausted_systems: list[str] = []

        for prop in proposals:
            name = prop.system_name
            failed_history = self._failed.get(name, [])
            proposed_history = self._proposed.get(name, [])
            proposed = prop.proposed_image

            # Per-system failure count: catches LLMs that propose monotonically
            # new but broken images (e.g. nginx:1.20 → nginx:1.21 → nginx:1.22)
            if len(failed_history) >= self.max_failures_per_system:
                exhausted_systems.append(f"{name}:{len(failed_history)} failures")

            if self.mode in ("no_change", "hybrid"):
                if failed_history and failed_history[-1] == proposed:
                    no_change_systems.append(f"{name}:{proposed}")

            if self.mode in ("image_repeat", "hybrid"):
                # Check the full failed_history; in hybrid mode the no_change
                # check above already covers the last entry, but in image_repeat
                # mode we must also catch re-proposing the most-recently-failed image.
                check_history = failed_history[:-1] if self.mode == "hybrid" else failed_history
                if proposed in check_history:
                    repeat_systems.append(f"{name}:{proposed}")
                elif proposed in proposed_history:
                    repeat_systems.append(f"{name}:{proposed}")

        messages: list[str] = []
        if exhausted_systems:
            messages.append(
                f"systems exhausted max failures: {', '.join(exhausted_systems)}"
            )
        if no_change_systems:
            messages.append(
                f"no-change proposals: {', '.join(no_change_systems)}"
            )
        if repeat_systems:
            messages.append(
                f"previously-tried images re-proposed: {', '.join(repeat_systems)}"
            )

        if messages:
            reason = "; ".join(messages)
            logger.warning("LoopGuard detected loop at attempt %d — %s", attempt, reason)
            return True, reason

        return False, ""

    def record_proposals(self, proposals: list[RepairProposal]) -> None:
        """Record the images that the LLM proposed.

        Must be called AFTER ``check`` to avoid poisoning the repeat-detection
        history with the proposals before they have been evaluated.
        """
        for prop in proposals:
            self._proposed.setdefault(prop.system_name, []).append(prop.proposed_image)

    def summary(self) -> dict[str, dict[str, list[str]]]:
        """Return the full per-system image history (for logging / artifacts)."""
        return {
            "failed": dict(self._failed),
            "proposed": dict(self._proposed),
        }
