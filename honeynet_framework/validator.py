"""
Structural validation for WorldModel.

Pure structural checks — no catalog, no policies, no hardcoded rules in this file.
Rules are registered in ``STRUCTURAL_RULES`` for testability and extension (P2).

depends_on semantics
--------------------
``depends_on`` models **container start ordering** (Compose-style linear prerequisite).
It must form a DAG — cycles are a hard validation error because a topological
ordering (= start sequence) is not satisfiable for a cycle.

Cycle detection runs on **resolved system names only**: if a name in depends_on
is unknown (typo, missing system), it produces ``REF_DEPENDS_ON`` but the edge is
skipped in cycle detection.  This means a latent cycle can be hidden behind a
typo and only surface once the name is corrected.

If the scenario needs to express bidirectional coupling (e.g. two control planes
that interact at runtime), that relationship should NOT be modelled via depends_on.
depends_on is strictly "must be started before", not "communicates with".
"""

import logging
import re
from collections.abc import Callable

from .models import WorldModel, ValidationError, ValidationResult

from .image_patterns import (
    ONE_SHOT_BINARIES as _ONE_SHOT_BINARIES,
    SCRIPT_EXTENSIONS as _SCRIPT_EXTENSIONS,
    MODULE_LAUNCHERS as _MODULE_LAUNCHERS,
    LIKELY_BUILTIN_PYTHON_MODULES as _LIKELY_BUILTIN_PYTHON_MODULES,
)

logger = logging.getLogger(__name__)

RuleFn = Callable[[WorldModel], list[ValidationError]]


def _rule_min_zones(world_model: WorldModel) -> list[ValidationError]:
    if not world_model.zones:
        return [
            ValidationError(
                rule="MIN_ZONES",
                details="World model has no zones defined",
            )
        ]
    return []


def _rule_min_systems(world_model: WorldModel) -> list[ValidationError]:
    deployable = world_model.deployable_systems
    if not deployable:
        return [
            ValidationError(
                rule="MIN_SYSTEMS",
                details="World model has no deployable systems (systems with deploy section)",
            )
        ]
    return []


def _rule_unique_network_names(world_model: WorldModel) -> list[ValidationError]:
    errors: list[ValidationError] = []
    seen_networks: dict[str, str] = {}
    for zone_name, zone in world_model.zones.items():
        net_name = zone.deploy.network_name
        if net_name in seen_networks:
            errors.append(
                ValidationError(
                    rule="UNIQUE_NETWORK_NAME",
                    details=(
                        f"Duplicate network_name '{net_name}' in zones '{zone_name}' "
                        f"and '{seen_networks[net_name]}'"
                    ),
                )
            )
        else:
            seen_networks[net_name] = zone_name
    return errors


def _rule_systems(world_model: WorldModel) -> list[ValidationError]:
    errors: list[ValidationError] = []
    for sys_name, system in world_model.systems.items():
        if not system.deploy:
            continue

        zone_val = (system.deploy.zone or "").strip()
        if zone_val not in world_model.zones:
            if zone_val:
                errors.append(
                    ValidationError(
                        rule="REF_ZONE",
                        details=f"System '{sys_name}' references undefined zone '{zone_val}'",
                        fix_hint=f"Available zones: {', '.join(world_model.zones.keys())}",
                    )
                )
            else:
                errors.append(
                    ValidationError(
                        rule="REF_ZONE",
                        details=f"System '{sys_name}' has no zone assigned",
                    )
                )

        image_val = (system.deploy.image or "").strip()
        if not image_val:
            errors.append(
                ValidationError(
                    rule="REQUIRED_IMAGE",
                    details=f"System '{sys_name}' has no Docker image specified",
                )
            )

        for dep in system.deploy.depends_on:
            if dep not in world_model.systems:
                errors.append(
                    ValidationError(
                        rule="REF_DEPENDS_ON",
                        details=f"System '{sys_name}' depends on undefined system '{dep}'",
                        fix_hint=(
                            f"Check for typos in depends_on. Available systems: "
                            f"{', '.join(world_model.systems.keys())}. "
                            f"Note: fixing a typo here may expose a circular dependency "
                            f"(CIRCULAR_DEPENDENCY) — re-run validation after correcting the name."
                        ),
                    )
                )
            elif dep == sys_name:
                errors.append(
                    ValidationError(
                        rule="SELF_DEPENDENCY",
                        details=f"System '{sys_name}' depends on itself",
                    )
                )

        for port in system.deploy.ports:
            if port < 1 or port > 65535:
                errors.append(
                    ValidationError(
                        rule="PORT_RANGE",
                        details=f"System '{sys_name}' has invalid port {port}",
                    )
                )
    return errors


def _rule_no_cycles(world_model: WorldModel) -> list[ValidationError]:
    cycle = _detect_cycle(world_model)
    if cycle:
        return [
            ValidationError(
                rule="CIRCULAR_DEPENDENCY",
                details=f"Circular dependency detected: {' -> '.join(cycle)}",
            )
        ]
    return []


def _normalize_command(cmd: object) -> list[str]:
    """Normalize command to list[str].

    Commands may arrive as list[str] (correct) or as a bare string
    (from LLM output).  A string is tokenized via shlex.split to
    correctly handle quoted arguments.
    """
    import shlex

    if isinstance(cmd, list):
        return [str(c) for c in cmd]
    if isinstance(cmd, str):
        try:
            return shlex.split(cmd)
        except ValueError:
            # Malformed quoting — split on spaces as fallback
            return cmd.split()
    return []


def _warn_one_shot_command(world_model: WorldModel) -> list[ValidationError]:
    """Warn when a container's command looks like a one-shot/batch job.

    The kreuzwerker/docker provider expects PID 1 to stay running.
    One-shot commands (backup tools, dump utilities, file operations)
    cause 'container exited immediately' errors at apply time.
    """
    warnings: list[ValidationError] = []
    for sys_name, system in world_model.systems.items():
        if not system.deploy or not system.deploy.command:
            continue
        cmd = _normalize_command(system.deploy.command)
        if not cmd:
            continue
        # command is list[str]; first element is the binary (or shell -c wrapper)
        binary = cmd[0].strip().split("/")[-1]  # handle /usr/bin/restic etc.

        # Handle "sh -c ..." / "bash -c ..." wrappers — inspect the inner command
        if binary in ("sh", "bash") and len(cmd) >= 3 and cmd[1] == "-c":
            inner_parts = cmd[2].strip().split()
            inner_first = inner_parts[0].split("/")[-1] if inner_parts else ""
            if inner_first in _ONE_SHOT_BINARIES:
                warnings.append(ValidationError(
                    rule="ONE_SHOT_COMMAND",
                    details=(
                        f"System '{sys_name}' command starts with '{inner_first}' "
                        f"(via shell wrapper) which is a one-shot tool — container will exit immediately"
                    ),
                    severity="WARNING",
                    fix_hint="Use a daemon image or long-running server process instead of a batch command.",
                ))
            continue

        if binary in _ONE_SHOT_BINARIES:
            warnings.append(ValidationError(
                rule="ONE_SHOT_COMMAND",
                details=(
                    f"System '{sys_name}' command starts with '{binary}' "
                    f"which is a one-shot tool — container will exit immediately"
                ),
                severity="WARNING",
                fix_hint="Use a daemon image or long-running server process instead of a batch command.",
            ))
    return warnings


def _warn_fictional_script(world_model: WorldModel) -> list[ValidationError]:
    """Warn when a base image runs a custom script that doesn't ship with it.

    E.g. ``python:3.12-slim`` with command ``python scheduler.py`` — that file
    doesn't exist in the image and the container will crash on start.
    """
    warnings: list[ValidationError] = []
    for sys_name, system in world_model.systems.items():
        if not system.deploy or not system.deploy.command:
            continue
        # Detect base/runtime images: check if the image name starts with a
        # known runtime prefix (python:, node:, ruby:, etc.).  Only these
        # bare runtime images lack application code — custom images like
        # mycompany/myapp:1.0 may legitimately run "python app.py".
        image = (system.deploy.image or "").lower()
        image_base = image.split(":")[0].split("/")[-1]  # "python" from "python:3.12-slim"
        is_base_image = image_base in _MODULE_LAUNCHERS
        if not is_base_image:
            continue

        cmd = _normalize_command(system.deploy.command)
        if not cmd:
            continue

        # Unwrap shell wrappers: "sh -c 'python app.py'" → check inner command
        binary = cmd[0].strip().split("/")[-1]
        if binary in ("sh", "bash") and len(cmd) >= 3 and cmd[1] == "-c":
            inner_parts = cmd[2].strip().split()
            if inner_parts:
                cmd = inner_parts  # replace cmd with inner command for script check

        # Look for script file references in the command (including cmd[0] itself)
        for arg in cmd:
            arg_stripped = arg.strip()
            if _SCRIPT_EXTENSIONS.search(arg_stripped):
                warnings.append(ValidationError(
                    rule="FICTIONAL_SCRIPT",
                    details=(
                        f"System '{sys_name}' runs '{arg_stripped}' on base image "
                        f"'{system.deploy.image}' — this file likely doesn't exist in the image"
                    ),
                    severity="WARNING",
                    fix_hint=(
                        "Use an image that includes the script, mount it via a volume, "
                        "or use the image's default entrypoint (set command to null)."
                    ),
                ))
                break  # one warning per system is enough
        else:
            # Also catch module-style launches on base images (python -m app.main,
            # node server, ruby app) that often require copied artifacts.
            launcher = cmd[0].strip().split("/")[-1].lower()
            if launcher in _MODULE_LAUNCHERS and len(cmd) >= 2:
                if launcher.startswith("python") and cmd[1] == "-m" and len(cmd) >= 3:
                    module_name = cmd[2].strip()
                    if "." in module_name and module_name not in _LIKELY_BUILTIN_PYTHON_MODULES:
                        warnings.append(ValidationError(
                            rule="FICTIONAL_SCRIPT",
                            details=(
                                f"System '{sys_name}' runs module '{module_name}' on base image "
                                f"'{system.deploy.image}' — required project artifacts may be missing in the image"
                            ),
                            severity="WARNING",
                            fix_hint=(
                                "Use an image that includes your application module, mount source artifacts, "
                                "or switch to an image with the app already baked in."
                            ),
                        ))
                elif launcher in {"node", "ruby", "php"}:
                    entry = cmd[1].strip()
                    if not entry.startswith("-") and not _SCRIPT_EXTENSIONS.search(entry):
                        warnings.append(ValidationError(
                            rule="FICTIONAL_SCRIPT",
                            details=(
                                f"System '{sys_name}' runs entry '{entry}' on base image "
                                f"'{system.deploy.image}' — required app artifacts may be missing in the image"
                            ),
                            severity="WARNING",
                            fix_hint=(
                                "Use an image that contains the application files/modules, "
                                "or mount them explicitly via volumes."
                            ),
                        ))
    return warnings


def _rule_invalid_command(world_model: WorldModel) -> list[ValidationError]:
    """Error when a container command violates the command policy.

    This is a structural ERROR (not a warning) so it triggers the repair loop.
    The built-in command-policy repair strategy will auto-fix these.
    """
    from .command_policy import evaluate_command

    errors: list[ValidationError] = []
    for sys_name, system in world_model.systems.items():
        if not system.deploy or not system.deploy.command:
            continue
        if not isinstance(system.deploy.command, (list, str)):
            errors.append(ValidationError(
                rule="INVALID_COMMAND",
                details=(
                    f"System '{sys_name}' has command of unexpected type "
                    f"'{type(system.deploy.command).__name__}' — must be a list or string"
                ),
                fix_hint="Set command to a list of strings or a shell command string.",
            ))
            continue
        verdict, _suggested = evaluate_command(
            system.deploy.image or "", system.deploy.command,
        )
        if verdict == "strip":
            errors.append(ValidationError(
                rule="INVALID_COMMAND",
                details=(
                    f"System '{sys_name}' has command {system.deploy.command!r} "
                    f"which is invalid for image '{system.deploy.image}' "
                    f"(policy verdict: {verdict})"
                ),
                fix_hint="Will be auto-repaired by the command-policy repair strategy.",
            ))
    return errors


STRUCTURAL_RULES: list[RuleFn] = [
    _rule_min_zones,
    _rule_min_systems,
    _rule_unique_network_names,
    _rule_systems,
    _rule_no_cycles,
    _rule_invalid_command,
]

WARNING_RULES: list[RuleFn] = [
    _warn_one_shot_command,
    _warn_fictional_script,
]


def validate(world_model: WorldModel) -> ValidationResult:
    """Validate a WorldModel for structural integrity.

    Returns a ValidationResult with errors and warnings.
    """
    errors: list[ValidationError] = []
    warnings: list[ValidationError] = []

    for rule_fn in STRUCTURAL_RULES:
        errors.extend(rule_fn(world_model))

    for rule_fn in WARNING_RULES:
        warnings.extend(rule_fn(world_model))

    passed = len(errors) == 0
    return ValidationResult(passed=passed, errors=errors, warnings=warnings)


def _detect_cycle(world_model: WorldModel) -> list[str]:
    """Detect circular dependencies using iterative DFS. Returns cycle path or empty list."""
    from .utils import detect_dependency_cycle

    nodes = {
        name: list(sys.deploy.depends_on) if sys.deploy else []
        for name, sys in world_model.systems.items()
    }
    return detect_dependency_cycle(nodes)
