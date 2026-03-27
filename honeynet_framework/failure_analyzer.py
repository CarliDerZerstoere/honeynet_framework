"""Failure analyzer — classifies container failures into typed categories.

This module is **read-only** with respect to the WorldModel: it only looks at
error text and docker logs and returns enriched FailureContext objects.  No
side effects beyond structured logging.

Usage
-----
    from .failure_analyzer import classify, enrich_failing_list

    failure_type, diagnosis = classify(raw_error, docker_logs)
    enriched = enrich_failing_list(failing_dicts)
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from .repair_types import FailureContext, FailureType

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pattern tables
# Each entry is (regex_pattern, FailureType, human_diagnosis_template).
# Patterns are tested in order; first match wins.
# ---------------------------------------------------------------------------

_ERROR_PATTERNS: list[tuple[re.Pattern, FailureType, str]] = [
    # Image not found / manifest unknown
    (re.compile(r"manifest (unknown|not found)", re.I),
     FailureType.IMAGE_NOT_FOUND,
     "Image does not exist on Docker Hub (manifest unknown)."),

    (re.compile(r"repository does not exist|image not found|no such image", re.I),
     FailureType.IMAGE_NOT_FOUND,
     "Image repository or tag does not exist."),

    (re.compile(r"pull access denied|denied: access forbidden", re.I),
     FailureType.IMAGE_NOT_FOUND,
     "Image pull access denied — likely a private or non-existent image."),

    # Rate limiting
    (re.compile(r"too many requests|rate limit|toomanyrequests", re.I),
     FailureType.IMAGE_PULL_RATE_LIMITED,
     "Docker Hub rate limit hit. Consider using a known-good local image."),

    # Exec / command problems
    (re.compile(r"exec format error", re.I),
     FailureType.BAD_COMMAND,
     "Exec format error — wrong architecture or corrupt binary."),

    (re.compile(r"no such file or directory", re.I),
     FailureType.BAD_COMMAND,
     "Command or entrypoint binary does not exist inside the container."),

    (re.compile(r"permission denied", re.I),
     FailureType.PERMISSION_DENIED,
     "Permission denied — file or socket not accessible by the container user."),

    # Environment
    (re.compile(r"required environment variable|env.*not set|missing.*env", re.I),
     FailureType.MISSING_ENV,
     "A required environment variable is not set."),

    # Port conflicts
    (re.compile(r"bind: address already in use|port.*already allocated", re.I),
     FailureType.PORT_CONFLICT,
     "Port conflict — another container or host process owns this port."),

    # OOM
    (re.compile(r"out of memory|oom kill|killed.*signal 9", re.I),
     FailureType.OOM,
     "Container killed by OOM killer. Consider raising memory limits."),

    # Dependency not ready
    (re.compile(r"connection refused|dial tcp.*refused|no route to host", re.I),
     FailureType.DEPENDENCY_NOT_READY,
     "Upstream dependency not yet reachable — container started too early."),

    # Immediate exit (generic)
    (re.compile(r"exited.*exit code [1-9]|container.*exited immediately", re.I),
     FailureType.CONTAINER_EXIT_IMMEDIATE,
     "Container exited immediately with a non-zero exit code."),
]

# Patterns applied to docker logs only (often richer than tofu stderr)
_LOG_PATTERNS: list[tuple[re.Pattern, FailureType, str]] = [
    (re.compile(r"can't open file|cannot open file|no such file", re.I),
     FailureType.BAD_COMMAND,
     "Entrypoint script or file not found inside the image."),

    (re.compile(r"password authentication failed|authentication.*failed", re.I),
     FailureType.MISSING_ENV,
     "Authentication failed — likely a missing or wrong password env-var."),

    (re.compile(r"address already in use|bind.*failed", re.I),
     FailureType.PORT_CONFLICT,
     "Port already in use (from docker logs)."),

    (re.compile(r"panic:|fatal error:|FATAL|Caused by:", re.I),
     FailureType.CONTAINER_EXIT_IMMEDIATE,
     "Application panicked or encountered a fatal startup error."),
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def classify(
    raw_error: str,
    docker_logs: str = "",
) -> tuple[FailureType, str]:
    """Classify a single container failure.

    Returns
    -------
    (FailureType, diagnosis_string)
    """
    combined = (raw_error or "").strip()
    logs = (docker_logs or "").strip()

    # Test against stderr/tofu error text first
    for pattern, ftype, diagnosis in _ERROR_PATTERNS:
        if pattern.search(combined):
            return ftype, diagnosis

    # Then against docker logs (often more informative)
    for pattern, ftype, diagnosis in _LOG_PATTERNS:
        if pattern.search(logs):
            return ftype, diagnosis

    return FailureType.UNKNOWN, "Could not classify failure automatically."


def enrich_failing_list(
    failing: list[dict],
    attempt: int = 0,
) -> list[FailureContext]:
    """Convert raw failing-container dicts to typed FailureContext objects.

    Parameters
    ----------
    failing:
        List of dicts as produced by ``_parse_failing_containers`` /
        ``_enrich_failing_containers_with_logs``.
    attempt:
        Which repair iteration this is (0-indexed).
    """
    result: list[FailureContext] = []
    for c in failing:
        raw_error = c.get("error", "")
        docker_logs = c.get("docker_logs", "")
        ftype, diagnosis = classify(raw_error, docker_logs)

        ctx = FailureContext(
            system_name=c.get("system_name", ""),
            image=c.get("image", ""),
            command=c.get("command"),
            zone=c.get("zone", ""),
            role=c.get("role", ""),
            raw_error=raw_error,
            docker_logs=docker_logs,
            failure_type=ftype,
            diagnosis=diagnosis,
            attempt=attempt,
        )
        result.append(ctx)

        logger.info(
            "FailureAnalyzer: %s → %s — %s",
            ctx.system_name, ftype.value, diagnosis,
        )

    return result
