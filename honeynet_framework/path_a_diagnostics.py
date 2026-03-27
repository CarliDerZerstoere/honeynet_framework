"""
Best-effort diagnostics for Path A apply failures.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .deployer import TerraformDeployer
    from .models import DeployProjection

logger = logging.getLogger(__name__)


def classify_apply_stderr(apply_stderr: str) -> str:
    """Coarse error bucket for routing / metrics (not a full taxonomy)."""
    s = (apply_stderr or "").lower()
    if "already exists" in s:
        return "already_exists"
    if "timeout" in s or "timed out" in s:
        return "timeout"
    if "pull" in s and ("denied" in s or "not found" in s or "manifest" in s):
        return "image_pull"
    if "port is already allocated" in s or "address already in use" in s:
        return "port_conflict"
    if "no such container" in s or "no such network" in s:
        return "missing_resource"
    if "connection refused" in s or "cannot connect" in s:
        return "connection_refused"
    if "permission denied" in s or "access denied" in s:
        return "permission_denied"
    if "no space left" in s or "disk quota" in s:
        return "disk_full"
    return "unknown"


def _extract_failing_resources(apply_stderr: str) -> list[str]:
    """Extract Terraform resource addresses from apply stderr."""
    if not apply_stderr:
        return []
    matches = re.findall(
        r"with\s+((?:module\.[^.\s,]+\.)*docker_(?:network|container)\.[^,\s]+)\s*,",
        apply_stderr,
    )
    return list(dict.fromkeys(matches))


def _safe_resource_name(name: str) -> str:
    """Mirror Terraform resource identifier sanitization used by renderer."""
    sanitized = re.sub(r"[^0-9A-Za-z_]+", "_", str(name or "").strip())
    sanitized = sanitized.strip("_")
    if not sanitized:
        sanitized = "resource"
    if sanitized[0].isdigit():
        sanitized = f"r_{sanitized}"
    return sanitized


def _allocate_resource_names(names: list[str]) -> dict[str, str]:
    """Allocate unique Terraform resource IDs with deterministic dedup suffixes."""
    allocated: dict[str, str] = {}
    used: set[str] = set()
    for raw_name in names:
        base_name = _safe_resource_name(raw_name)
        candidate = base_name
        suffix = 2
        while candidate in used:
            candidate = f"{base_name}__{suffix}"
            suffix += 1
        allocated[raw_name] = candidate
        used.add(candidate)
    return allocated


def _strip_module_prefix(resource_address: str) -> str:
    """Strip optional module path from Terraform resource address."""
    match = re.search(r"(docker_(?:network|container)\.[^,\s]+)$", resource_address)
    return match.group(1) if match else resource_address


def _build_resource_to_name_map(projection: "DeployProjection") -> dict[str, str]:
    """Map renderer-generated Terraform addresses to Docker names."""
    mapping: dict[str, str] = {}
    network_names = [net.name for net in projection.networks]
    container_names = [container.name for container in projection.containers]
    network_resource_names = _allocate_resource_names(network_names)
    container_resource_names = _allocate_resource_names(container_names)
    for net_name, resource_id in network_resource_names.items():
        mapping[f"docker_network.{resource_id}"] = net_name
    for container_name, resource_id in container_resource_names.items():
        mapping[f"docker_container.{resource_id}"] = container_name
    return mapping


async def collect_path_a_apply_failure_diagnostics(
    *,
    deployer: "TerraformDeployer",
    projection: "DeployProjection",
    work_dir: Path,
    apply_stderr: str,
    run_id: str,
    logs_tail_lines: int = 200,
) -> dict[str, Any]:
    """
    Collect and persist diagnostics for Path A apply failures.

    Best-effort only: command failures are captured into the artifact and never raised.
    """
    artifact_path = Path(work_dir) / "path_a_diagnostics.json"
    failing_resources = _extract_failing_resources(apply_stderr)
    tf_to_name = _build_resource_to_name_map(projection)

    docker_targets: list[dict[str, str]] = []
    for resource in failing_resources:
        normalized_resource = _strip_module_prefix(resource)
        if normalized_resource.startswith(("docker_container.", "docker_network.")):
            fallback_name = normalized_resource.split(".", 1)[1]
            docker_targets.append(
                {
                    "resource": normalized_resource,
                    "resource_address": resource,
                    "kind": normalized_resource.split(".", 1)[0],
                    "docker_name": tf_to_name.get(normalized_resource, fallback_name),
                }
            )

    err_class = classify_apply_stderr(apply_stderr)

    diagnostics: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "path": "A",
        "failure_stage": "apply",
        "error_class": err_class,
        "apply": {
            "failing_resources": failing_resources,
            "stderr_excerpt": (apply_stderr or "")[-8000:],
        },
        "docker_targets": docker_targets,
        "docker_inspect": [],
        "docker_logs_tail": [],
        "tofu_state_list": {},
    }

    for target in docker_targets:
        name = target["docker_name"]
        kind = target["kind"]
        inspect_cmd = ["inspect", name]
        if kind == "docker_network":
            inspect_cmd = ["network", "inspect", name]
        inspect_exception = ""
        try:
            inspect_result = await deployer._run_docker_cmd(inspect_cmd)
        except Exception as e:  # best-effort diagnostics must never abort
            inspect_result = None
            inspect_exception = str(e)
        diagnostics["docker_inspect"].append(
            {
                "resource": target["resource"],
                "resource_address": target.get("resource_address", target["resource"]),
                "docker_name": name,
                "command": ["docker", *inspect_cmd],
                "success": bool(inspect_result and inspect_result.success),
                "exit_code": inspect_result.exit_code if inspect_result else None,
                "stdout": inspect_result.stdout[-12000:] if inspect_result else "",
                "stderr": inspect_result.stderr[-4000:] if inspect_result else "",
                "exception": inspect_exception,
            }
        )

        if kind == "docker_container":
            logs_cmd = ["logs", f"--tail={int(logs_tail_lines)}", name]
            logs_exception = ""
            try:
                logs_result = await deployer._run_docker_cmd(logs_cmd)
            except Exception as e:  # best-effort diagnostics must never abort
                logs_result = None
                logs_exception = str(e)
            diagnostics["docker_logs_tail"].append(
                {
                    "resource": target["resource"],
                    "resource_address": target.get("resource_address", target["resource"]),
                    "docker_name": name,
                    "command": ["docker", *logs_cmd],
                    "success": bool(logs_result and logs_result.success),
                    "exit_code": logs_result.exit_code if logs_result else None,
                    "stdout": logs_result.stdout[-12000:] if logs_result else "",
                    "stderr": logs_result.stderr[-4000:] if logs_result else "",
                    "exception": logs_exception,
                }
            )

    try:
        state_list_result = await deployer._run_tofu(["state", "list"])
        diagnostics["tofu_state_list"] = {
            "attempted": True,
            "success": state_list_result.success,
            "exit_code": state_list_result.exit_code,
            "stdout": state_list_result.stdout[-12000:],
            "stderr": state_list_result.stderr[-4000:],
            "exception": "",
        }
    except Exception as e:  # best-effort diagnostics must never abort
        diagnostics["tofu_state_list"] = {
            "attempted": True,
            "success": False,
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "exception": str(e),
        }

    summary: dict[str, Any] = {
        "collected": False,
        "artifact_path": "",
        "error_class": err_class,
        "failing_resources_count": len(failing_resources),
        "docker_targets_count": len(docker_targets),
        "inspect_success_count": sum(1 for x in diagnostics["docker_inspect"] if x.get("success")),
        "logs_success_count": sum(1 for x in diagnostics["docker_logs_tail"] if x.get("success")),
        "state_list_success": bool(diagnostics["tofu_state_list"].get("success")),
    }

    try:
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(json.dumps(diagnostics, indent=2), encoding="utf-8")
        summary["collected"] = True
        summary["artifact_path"] = str(artifact_path)
        return summary
    except OSError as e:
        logger.warning("Could not write Path A diagnostics artifact: %s", e)
        summary["error"] = str(e)
        return summary
