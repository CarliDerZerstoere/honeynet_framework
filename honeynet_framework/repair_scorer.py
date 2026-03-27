"""Repair scorer — assigns a confidence score to LLM repair proposals.

The scorer is purely heuristic and deterministic (no LLM calls).  It rewards
proposals that are clearly moving toward a verified known-good image and
penalises proposals that look like hallucinations or non-changes.

Score range: 0.0 (very unlikely to help) → 1.0 (high confidence).

Usage
-----
    from .repair_scorer import score_proposal, score_proposals

    proposals = score_proposals(failure_contexts, raw_proposals)
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

from .repair_types import FailureContext, FailureType, RepairProposal

logger = logging.getLogger(__name__)

# Path to the known-good image registry (same file used by ImageResolver)
_KNOWN_GOOD_PATH = Path(__file__).parent / "data" / "known_good_images.json"

_known_good_cache: Optional[set[str]] = None
_known_good_lock = threading.Lock()


def _known_good_images() -> set[str]:
    """Return the set of known-good image tags (cached)."""
    global _known_good_cache
    with _known_good_lock:
        if _known_good_cache is not None:
            return _known_good_cache
        try:
            import json
            data = json.loads(_KNOWN_GOOD_PATH.read_text(encoding="utf-8"))
            tags: set[str] = set()
            # Support both list format [{"tag": "..."}] and dict format
            # {"images": {"name": ["tag1", "tag2"]}} (current schema).
            entries: list = []
            if isinstance(data, list):
                entries = data
            elif isinstance(data, dict):
                images = data.get("images", data)
                if isinstance(images, dict):
                    for tag_list in images.values():
                        if isinstance(tag_list, list):
                            for tag in tag_list:
                                if isinstance(tag, str) and tag:
                                    tags.add(tag.lower())
                elif isinstance(images, list):
                    entries = images
            for entry in entries:
                tag = entry.get("tag") or entry.get("image") or "" if isinstance(entry, dict) else str(entry)
                if tag:
                    tags.add(tag.lower())
            _known_good_cache = tags
            return tags
        except Exception:
            logger.warning(
                "Failed to load known_good_images from %s — will retry on next call",
                _KNOWN_GOOD_PATH,
            )
            # Do NOT cache on failure — return empty set but allow retry
            return set()


# ---------------------------------------------------------------------------
# Scoring logic
# ---------------------------------------------------------------------------

def score_proposal(
    ctx: FailureContext,
    proposed_image: str,
    proposed_command: Optional[list[str]],
) -> tuple[float, str]:
    """Score a single repair proposal.

    Returns
    -------
    (score: float, reason: str)
    """
    known_good = _known_good_images()
    orig = (ctx.image or "").strip().lower()
    prop = (proposed_image or "").strip().lower()

    # No-op: proposed image identical to original → useless
    if prop == orig or not prop:
        return 0.1, "Proposed image is identical to original — repair has no effect."

    # Is the proposed image in the known-good registry?
    # Normalize to lowercase for comparison since registry tags are stored lowercase.
    in_known_good = prop in known_good

    # --- Base score by failure type ------------------------------------------
    if ctx.failure_type == FailureType.IMAGE_NOT_FOUND:
        # The original image doesn't exist — any real change is helpful
        base = 0.7 if in_known_good else 0.45

    elif ctx.failure_type == FailureType.BAD_COMMAND:
        # Command problem — if image changed it might help, but less certain
        base = 0.6 if in_known_good else 0.35
        # If the command was also changed, give extra credit
        if proposed_command and proposed_command != ctx.command:
            base = min(base + 0.15, 1.0)

    elif ctx.failure_type == FailureType.IMAGE_PULL_RATE_LIMITED:
        # Rate-limited → switching to any locally cached/known-good image is great
        base = 0.85 if in_known_good else 0.5

    elif ctx.failure_type == FailureType.CONTAINER_EXIT_IMMEDIATE:
        base = 0.55 if in_known_good else 0.3

    elif ctx.failure_type == FailureType.MISSING_ENV:
        # Image change alone unlikely to fix a missing env-var
        base = 0.25

    elif ctx.failure_type == FailureType.PORT_CONFLICT:
        # Port conflict — image change won't fix it unless it's a different service
        base = 0.2

    elif ctx.failure_type == FailureType.LOOP_DETECTED:
        # We're in a loop — skeptical of any proposal
        base = 0.1

    else:
        base = 0.4 if in_known_good else 0.2

    # --- Adjustments ---------------------------------------------------------
    reason_parts: list[str] = []

    if in_known_good:
        reason_parts.append("proposed image is in known-good registry (+)")
    else:
        reason_parts.append("proposed image NOT in known-good registry (-)")

    # Penalty: proposed image looks like a generic stand-in
    standin_hints = ["nginx", "busybox", "alpine", "scratch", "hello-world"]
    if any(h in prop for h in standin_hints) and ctx.failure_type not in (
        FailureType.IMAGE_NOT_FOUND, FailureType.IMAGE_PULL_RATE_LIMITED
    ):
        base = max(base - 0.1, 0.05)
        reason_parts.append("proposed image appears to be a generic stand-in (-)")

    # Bonus: proposed image shares the same base name (different tag)
    orig_name = orig.split(":")[0]
    prop_name = prop.split(":")[0]
    if orig_name == prop_name and orig != prop:
        base = min(base + 0.1, 1.0)
        reason_parts.append("same image name with different tag (+)")

    score = round(min(max(base, 0.0), 1.0), 3)
    reason = "; ".join(reason_parts) if reason_parts else "heuristic base score"

    logger.debug(
        "RepairScorer: %s [%s] → %s = %.3f (%s)",
        ctx.system_name, ctx.failure_type.value, proposed_image, score, reason,
    )
    return score, reason


def score_proposals(
    contexts: list[FailureContext],
    proposals: list[RepairProposal],
) -> list[RepairProposal]:
    """Score a list of proposals in-place and return them.

    Matches proposals to contexts by system_name; unmatched proposals get a
    neutral score of 0.4.
    """
    ctx_by_name = {c.system_name: c for c in contexts}

    for prop in proposals:
        ctx = ctx_by_name.get(prop.system_name)
        if ctx is None:
            prop.score = 0.4
            prop.score_reason = "no failure context found for system"
        else:
            prop.score, prop.score_reason = score_proposal(
                ctx, prop.proposed_image, prop.proposed_command,
            )

    return proposals
