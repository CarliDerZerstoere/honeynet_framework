"""Consistency fixer — repairs structural inconsistencies in a WorldModel.

This module performs *safe, deterministic* mutations:

1. Zone consistency
   - If a system references a zone that doesn't exist in ``wm.zones``, the
     zone is auto-created with a default subnet so the model compiles.
   - Logging-only variant: just log; don't mutate (used when
     ``enable_consistency_fixer=False``).

2. Secret / env hygiene (log-only, never mutates)
   - Detects env-var values that look like bare secrets (passwords, tokens,
     keys) passed in plaintext instead of through ``wm.secrets``.
   - Emits a WARNING for each one so the operator is aware.

Usage
-----
    from .consistency_fixer import fix_zone_consistency, log_secret_issues

    fixes = fix_zone_consistency(world_model)
    warnings = log_secret_issues(world_model)
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import WorldModel

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Zone consistency
# ---------------------------------------------------------------------------

def fix_zone_consistency(world_model: "WorldModel") -> list[str]:
    """Ensure every system's deploy.zone exists in wm.zones.

    Missing zones are auto-created with a default CIDR.  Returns a list of
    human-readable fix descriptions (empty = nothing changed).
    """
    from .models.zone import WorldZone, ZoneDeploy

    fixes: list[str] = []

    for sys_name, sys in world_model.systems.items():
        if not sys.deploy:
            continue
        zone_name = sys.deploy.zone
        if not zone_name:
            continue
        if zone_name in world_model.zones:
            continue

        # Auto-create a minimal zone so the compiler doesn't crash.
        # Scan existing subnets to avoid collisions.
        used_offsets: set[int] = set()
        for z in world_model.zones.values():
            existing = getattr(z.deploy, "subnet", "") or ""
            # Parse offset from "172.30.{n}.0/24"
            if existing.startswith("172.30.") and existing.endswith(".0/24"):
                try:
                    used_offsets.add(int(existing.split(".")[2]))
                except (ValueError, IndexError):
                    pass
        subnet_offset = 0
        while subnet_offset in used_offsets:
            subnet_offset += 1
            if subnet_offset > 255:
                raise ValueError(
                    f"Subnet space exhausted (256 /24 subnets used) — "
                    f"cannot auto-create zone '{zone_name}'"
                )
        # If any system in this zone has ports defined, the zone must not
        # be internal — otherwise port publishing is silently suppressed.
        has_ports = any(
            s.deploy and s.deploy.zone == zone_name and s.deploy.ports
            for s in world_model.systems.values()
        )
        # Ensure network_name uniqueness: check existing zone network names
        used_network_names = {
            z.deploy.network_name
            for z in world_model.zones.values()
            if z.deploy and z.deploy.network_name
        }
        network_name = zone_name
        if network_name in used_network_names:
            suffix = 2
            while f"{network_name}_{suffix}" in used_network_names:
                suffix += 1
            network_name = f"{network_name}_{suffix}"
            logger.warning(
                "ConsistencyFixer: network_name '%s' already in use — using '%s' instead",
                zone_name, network_name,
            )

        world_model.zones[zone_name] = WorldZone(
            name=zone_name,
            deploy=ZoneDeploy(
                network_name=network_name,
                subnet=f"172.30.{subnet_offset}.0/24",
                internal=not has_ports,
            ),
        )
        msg = f"Auto-created missing zone '{zone_name}' (referenced by system '{sys_name}')"
        fixes.append(msg)
        logger.warning("ConsistencyFixer: %s", msg)

    return fixes


# ---------------------------------------------------------------------------
# Secret / env hygiene (log-only)
# ---------------------------------------------------------------------------

# Env-var names that are almost certainly secrets.
# Deliberately narrow: we skip generic terms like "auth", "cert" that appear
# in non-secret var names like AUTH_SERVICE_URL or CERT_PATH.
_SECRET_KEY_HINTS = re.compile(
    r"password|passwd|secret|_token|api[_.]?key|private[_.]?key|credentials?|jwt[_.]?secret",
    re.I,
)


def scrub_env_for_artifact(env_list: list[str]) -> list[str]:
    """Return a copy of an env list with secret values replaced by '[REDACTED]'.

    Only the serialized artifact copy is scrubbed — the live WorldModel and
    DeployProjection are never mutated so containers still receive real values.

    Examples::

        ["POSTGRES_PASSWORD=hunter2", "PORT=5432"]
        → ["POSTGRES_PASSWORD=[REDACTED]", "PORT=5432"]
    """
    result: list[str] = []
    for entry in env_list:
        if "=" not in entry:
            result.append(entry)
            continue
        key, _, value = entry.partition("=")
        stripped_value = value.strip()
        if _SECRET_KEY_HINTS.search(key.strip()) and stripped_value and not (
            stripped_value.startswith("${") and stripped_value.endswith("}")
        ):
            result.append(f"{key}=[REDACTED]")
        else:
            result.append(entry)
    return result


def log_secret_issues(world_model: "WorldModel") -> list[str]:
    """Scan env vars for plaintext secrets and emit warnings.

    Returns a list of warning strings (empty = nothing suspicious found).
    This function never mutates the WorldModel.
    """
    warnings: list[str] = []

    for sys_name, sys in world_model.systems.items():
        if not sys.deploy:
            continue
        for env_entry in sys.deploy.env or []:
            if "=" not in env_entry:
                continue
            key, _, value = env_entry.partition("=")
            key = key.strip()
            value = value.strip()

            # Flag if the key name looks like a secret AND the value is non-empty
            if _SECRET_KEY_HINTS.search(key) and value:
                # Skip values that are already a template reference like ${...}
                # (they reference an external secret rather than containing one)
                stripped = value.strip()
                secret_ref = stripped.startswith("${") and stripped.endswith("}")
                if not secret_ref:
                    msg = (
                        f"System '{sys_name}' has env var '{key}' with a "
                        f"plaintext value — consider moving it to wm.secrets"
                    )
                    warnings.append(msg)
                    logger.warning("ConsistencyFixer (secret): %s", msg)

    return warnings
