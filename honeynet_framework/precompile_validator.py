"""Pre-compile validator — structural checks on a WorldModel before Terraform render.

Catches problems that would silently produce broken IaC or confusing runtime
failures:

  - Missing images (empty string)
  - Missing zone references
  - Dangling depends_on references
  - Empty system list (would produce an empty Terraform file)

Severity levels
---------------
ERROR   → blocks compilation when ``enable_precompile_validation=True``
WARNING → logged but does not block
INFO    → informational only

Usage
-----
    from .precompile_validator import validate as precompile_validate

    issues = precompile_validate(world_model)
    errors = [i for i in issues if i.severity == IssueSeverity.ERROR]
    if errors:
        raise ValueError(...)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .repair_types import IssueSeverity, PreCompileIssue

if TYPE_CHECKING:
    from .models import WorldModel

logger = logging.getLogger(__name__)


def validate(world_model: "WorldModel") -> list[PreCompileIssue]:
    """Run all pre-compile checks and return a list of issues.

    An empty list means the WorldModel is structurally clean.
    """
    issues: list[PreCompileIssue] = []

    deployable = {
        k: v for k, v in world_model.systems.items() if v.deploy
    }

    # --- Check: non-empty system list ----------------------------------------
    if not deployable:
        issues.append(PreCompileIssue(
            severity=IssueSeverity.ERROR,
            system_name=None,
            field="systems",
            message="WorldModel has no deployable systems (deploy section missing on all systems).",
            suggestion="Add a 'deploy' block with at least 'image' and 'zone' to one or more systems.",
        ))
        # Nothing else to check
        _log_issues(issues)
        return issues

    # --- Check: required fields on each system --------------------------------
    for sys_name, sys in deployable.items():
        d = sys.deploy

        # Missing image
        if not d.image or not d.image.strip():
            issues.append(PreCompileIssue(
                severity=IssueSeverity.ERROR,
                system_name=sys_name,
                field="deploy.image",
                message=f"System '{sys_name}' has no Docker image specified.",
                suggestion="Set deploy.image to a valid Docker image tag (e.g. 'nginx:1.25-alpine').",
            ))

        # Missing zone reference
        if not d.zone or not d.zone.strip():
            issues.append(PreCompileIssue(
                severity=IssueSeverity.ERROR,
                system_name=sys_name,
                field="deploy.zone",
                message=f"System '{sys_name}' has no zone assigned.",
                suggestion="Set deploy.zone to an existing zone name or let ConsistencyFixer create it.",
            ))
        elif d.zone.strip() not in world_model.zones:
            issues.append(PreCompileIssue(
                severity=IssueSeverity.ERROR,
                system_name=sys_name,
                field="deploy.zone",
                message=f"System '{sys_name}' references zone '{d.zone.strip()}' which is not in wm.zones.",
                suggestion="Enable enable_consistency_fixer to auto-create missing zones.",
            ))

        # Dangling depends_on (ERROR — matches validator severity)
        for dep in (d.depends_on or []):
            if dep not in world_model.systems:
                issues.append(PreCompileIssue(
                    severity=IssueSeverity.ERROR,
                    system_name=sys_name,
                    field="deploy.depends_on",
                    message=f"System '{sys_name}' depends_on '{dep}' which doesn't exist.",
                    suggestion=f"Remove '{dep}' from depends_on or add a system with that name.",
                ))

        # Self-dependency
        for dep in (d.depends_on or []):
            if dep == sys_name:
                issues.append(PreCompileIssue(
                    severity=IssueSeverity.ERROR,
                    system_name=sys_name,
                    field="deploy.depends_on",
                    message=f"System '{sys_name}' depends on itself.",
                    suggestion="Remove the self-reference from depends_on.",
                ))

        # Port range validation
        for port in (d.ports or []):
            if port < 1 or port > 65535:
                issues.append(PreCompileIssue(
                    severity=IssueSeverity.ERROR,
                    system_name=sys_name,
                    field="deploy.ports",
                    message=f"System '{sys_name}' has invalid port {port} (must be 1-65535).",
                    suggestion="Use a valid port number between 1 and 65535.",
                ))

    # --- Check: at least one zone defined -------------------------------------
    if not world_model.zones:
        issues.append(PreCompileIssue(
            severity=IssueSeverity.ERROR,
            system_name=None,
            field="zones",
            message="WorldModel has no zones defined.",
            suggestion="Define at least one zone or enable ConsistencyFixer to auto-create them.",
        ))

    # --- Check: unique network names ------------------------------------------
    seen_networks: dict[str, str] = {}
    for zone_name, zone in world_model.zones.items():
        net = (zone.deploy.network_name or "").strip() if zone.deploy else ""
        if net and net in seen_networks:
            issues.append(PreCompileIssue(
                severity=IssueSeverity.ERROR,
                system_name=None,
                field="zones.deploy.network_name",
                message=(
                    f"Zones '{seen_networks[net]}' and '{zone_name}' share "
                    f"network_name '{net}' — Docker would create overlapping networks."
                ),
                suggestion="Give each zone a unique network_name.",
            ))
        elif net:
            seen_networks[net] = zone_name

    # --- Check: circular dependencies ----------------------------------------
    from .utils import detect_dependency_cycle

    # Include ALL systems (not just deployable) so that cycles through
    # non-deployable systems are detected — consistent with validator.py.
    dep_nodes = {
        name: list(sys.deploy.depends_on or []) if sys.deploy else []
        for name, sys in world_model.systems.items()
    }
    cycle = detect_dependency_cycle(dep_nodes)
    if cycle:
        issues.append(PreCompileIssue(
            severity=IssueSeverity.ERROR,
            system_name=cycle[0],
            field="deploy.depends_on",
            message=f"Circular dependency detected: {' -> '.join(cycle)}.",
            suggestion="Break the cycle by removing one depends_on edge.",
        ))

    _log_issues(issues)
    return issues


def _log_issues(issues: list[PreCompileIssue]) -> None:
    for issue in issues:
        prefix = f"[{issue.system_name}] " if issue.system_name else ""
        if issue.severity == IssueSeverity.ERROR:
            logger.error("PreCompileValidator: %s%s", prefix, issue.message)
        elif issue.severity == IssueSeverity.WARNING:
            logger.warning("PreCompileValidator: %s%s", prefix, issue.message)
        else:
            logger.info("PreCompileValidator: %s%s", prefix, issue.message)
