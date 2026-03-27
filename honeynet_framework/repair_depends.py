"""
Deterministic repair for depends_on edges (no LLM, no hardcoded container commands).

Repair strategies (in order):
1. Drop self-dependencies.
2. Case-insensitive exact match (e.g. ``Postgres`` → ``postgres``).
3. Fuzzy match: substring, prefix/suffix, Levenshtein distance ≤ 3.
4. If no unique match found, keep the broken ref (validator will report it).
"""

from __future__ import annotations

import logging

from .models import WorldModel

logger = logging.getLogger(__name__)


def try_repair_depends_edges(world_model: WorldModel) -> tuple[WorldModel, bool]:
    """
    Mutate world_model in place. Returns (world_model, changed).
    """
    changed = False
    known = set(world_model.systems.keys())
    lower_map = {k.lower(): k for k in world_model.systems.keys()}

    for name, sys in list(world_model.systems.items()):
        if not sys.deploy or not sys.deploy.depends_on:
            continue
        new_deps: list[str] = []
        for dep in list(sys.deploy.depends_on):
            if dep == name:
                changed = True
                continue
            if dep in known:
                new_deps.append(dep)
                continue
            # Case-insensitive exact match
            lk = dep.lower()
            if lk in lower_map:
                fixed = lower_map[lk]
                if fixed != dep:
                    changed = True
                new_deps.append(fixed)
                continue
            # Fuzzy match
            match = _fuzzy_match_system(dep, known)
            if match and match != name:
                logger.info(
                    "Fuzzy depends_on repair: '%s' → '%s' (system '%s')",
                    dep, match, name,
                )
                new_deps.append(match)
                changed = True
                continue
            elif match and match == name:
                logger.info(
                    "Fuzzy depends_on repair: '%s' → '%s' would create self-dependency for '%s' — dropping",
                    dep, match, name,
                )
                changed = True
                continue
            # No match — keep broken ref for validator to report
            new_deps.append(dep)
        if new_deps != sys.deploy.depends_on:
            sys.deploy.depends_on = new_deps
            changed = True

    return world_model, changed


def _fuzzy_match_system(target: str, available: set[str]) -> str | None:
    """Find the closest matching system name for a broken depends_on reference.

    Returns the match if exactly one candidate is found (unambiguous).
    Returns ``None`` if zero or multiple candidates match (ambiguous / no match).
    """
    target_lower = target.lower()

    # Strategy 1: Substring containment
    #   "postgres" matches "postgres_metadata", "api_gateway" matches "partner_api_gateway"
    candidates = [s for s in available if target_lower in s.lower() or s.lower() in target_lower]
    if len(candidates) == 1:
        return candidates[0]

    # Strategy 2: Suffix/prefix match with underscore boundary
    #   "api_gateway" matches "partner_api_gateway" (suffix)
    #   "postgres" matches "postgres_replica" (prefix)
    candidates = [
        s for s in available
        if s.lower().endswith(f"_{target_lower}")
        or s.lower().startswith(f"{target_lower}_")
        or target_lower.endswith(f"_{s.lower()}")
        or target_lower.startswith(f"{s.lower()}_")
    ]
    if len(candidates) == 1:
        return candidates[0]

    # Strategy 3: Levenshtein distance (adaptive threshold — shorter names
    #   are more sensitive to false-positive matches, so the threshold
    #   scales with name length: floor(len / 4), capped at 3).
    max_dist = min(3, max(1, len(target_lower) // 4))
    close = [
        (s, _levenshtein(target_lower, s.lower()))
        for s in available
    ]
    close = [(s, d) for s, d in close if d <= max_dist]
    if len(close) == 1:
        return close[0][0]

    # Strategy 4: Token overlap (split on underscore, check shared tokens).
    #   Require at least 2 overlapping tokens to avoid false matches on
    #   short, generic names like "auth", "db", "api".
    target_tokens = set(target_lower.split("_"))
    best_overlap = 0
    best_candidates: list[str] = []
    for s in available:
        s_tokens = set(s.lower().split("_"))
        overlap = len(target_tokens & s_tokens)
        if overlap > best_overlap:
            best_overlap = overlap
            best_candidates = [s]
        elif overlap == best_overlap and overlap > 0:
            best_candidates.append(s)
    if len(best_candidates) == 1 and best_overlap >= 2:
        return best_candidates[0]

    return None  # Ambiguous or no match


def _levenshtein(s1: str, s2: str) -> int:
    """Compute Levenshtein edit distance between two strings."""
    if len(s1) < len(s2):
        return _levenshtein(s2, s1)

    if len(s2) == 0:
        return len(s1)

    previous_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row

    return previous_row[-1]
