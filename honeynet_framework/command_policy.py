"""
Dynamic command policy for Docker images.

Uses OCI image introspection (ImageProfile) to decide whether a container
command is valid.  No hardcoded image prefix lists — all decisions are
derived from the image's actual Entrypoint/Cmd metadata.

Policy verdicts
---------------
* ``ok``    — command is valid, no action needed.
* ``strip`` — set command to ``None`` (image's default ENTRYPOINT is correct).
"""

from __future__ import annotations

import logging
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .image_introspector import ImageProfile

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def evaluate_command(
    image: str,
    command: Optional[list[str]],
    profile: Optional["ImageProfile"] = None,
) -> tuple[str, Optional[list[str]]]:
    """Evaluate a container command against the image's OCI profile.

    Parameters
    ----------
    image:
        Docker image reference (e.g. ``"python:3.12-slim"``).
    command:
        The command list, or ``None`` if unset.
    profile:
        Optional ``ImageProfile`` from OCI introspection.  When ``None``
        (network error, Ollama without tool-use), the command is trusted.

    Returns
    -------
    (verdict, suggested_command)
        * ``("ok", None)``    — command is fine.
        * ``("strip", None)`` — set command to ``None``.
    """
    if command is None:
        return "ok", None

    # No profile available — trust the command rather than silently
    # rewriting it based on assumptions.
    if profile is None or not profile.available:
        return "ok", None

    # Image has a real daemon entrypoint (nginx, postgres, keycloak, etc.)
    # Trust the user's command — the LLM may be providing necessary subcommands
    # or arguments (e.g. keycloak needs "start-dev", minio needs "server /data").
    # We cannot reliably distinguish "self-contained entrypoint" from "wrapper
    # that needs arguments" based on OCI metadata alone (many wrappers like
    # kc.sh have empty Cmd but still require a subcommand).
    if profile.has_daemon_entrypoint:
        return "ok", None

    # Image is a base runtime (python, node, sh) — the command is likely
    # a script reference that won't exist in the image.  Strip it so the
    # validator can flag the issue rather than silently injecting a
    # keep-alive command.
    if profile.is_base_runtime:
        return "strip", None

    # Unknown or utility image — trust the command.
    return "ok", None


def repair_world_model_commands(
    world_model: "WorldModel",
    profiles: Optional[dict[str, "ImageProfile"]] = None,
) -> list[str]:
    """Auto-repair invalid commands in a WorldModel in-place.

    Parameters
    ----------
    world_model:
        The WorldModel to repair.
    profiles:
        Optional pre-fetched image profiles keyed by image reference.
        When ``None``, commands are trusted (no repairs applied).

    Returns a list of human-readable repair descriptions.
    """
    repairs: list[str] = []
    for sys_name, system in world_model.systems.items():
        if not system.deploy:
            continue
        image = system.deploy.image or ""
        profile = (profiles or {}).get(image)
        verdict, suggested = evaluate_command(image, system.deploy.command, profile=profile)
        if verdict == "strip":
            old_cmd = system.deploy.command
            system.deploy.command = None
            repairs.append(
                f"{sys_name}: stripped command {old_cmd!r} "
                f"(image '{image}' is a base runtime — no daemon entrypoint)"
            )
            logger.info("Command policy: %s — stripped command %r", sys_name, old_cmd)
    return repairs
