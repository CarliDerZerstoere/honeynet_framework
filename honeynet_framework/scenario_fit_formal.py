"""
Formal scenario-fit evaluation against benchmark scenario references.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .models import WorldModel


def load_benchmark_reference(path: Path) -> dict[str, Any]:
    """Load benchmark scenario YAML and return parsed dictionary."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {}
    return data


def _as_dict_req(req: Any) -> dict[str, Any] | None:
    if isinstance(req, dict):
        return req
    if isinstance(req, str) and req.strip():
        return {"id": req.strip()}
    return None


def compute_formal_scenario_fit(world_model: WorldModel, scenario_doc: dict[str, Any] | None) -> dict[str, Any]:
    """Compute formal scenario-fit metrics from world model and scenario reference.

    *scenario_doc* may be ``None`` (file not found) or an empty / malformed
    dict.  These cases are reported distinctly:

    * ``reference_status: "not_loaded"`` — caller passed ``None`` (file missing).
    * ``reference_status: "empty"``       — file was parsed but has no reference
      section or no requirements.
    * ``reference_status: "evaluated"``   — at least one requirement was found.
    """
    if scenario_doc is None:
        return {
            "benchmark_id": "",
            "reference_status": "not_loaded",
            "benchmark_pass": False,
            "vacuous_reference": True,
            "planned_service_coverage": 0.0,
            "planned_zone_coverage": 0.0,
            "planned_dep_coverage": 0.0,
            "placement_violations": 0,
            "details": {},
        }
    reference = scenario_doc.get("reference", {}) if isinstance(scenario_doc, dict) else {}
    required_services = _as_list(reference.get("required_services"))
    required_zones = _as_list(reference.get("required_zones"))
    required_dependencies = _as_list(reference.get("required_dependencies"))
    forbidden_placements = _as_list(reference.get("forbidden_placements"))

    service_matches: dict[str, set[str]] = {}
    for req in required_services:
        req_d = _as_dict_req(req)
        if not req_d:
            continue
        req_id = str(req_d.get("id", "")).strip()
        if not req_id:
            continue
        matches = set()
        for sys_name, system in world_model.systems.items():
            if _matches_required_service(system, sys_name, req_d):
                matches.add(sys_name)
        service_matches[req_id] = matches

    zone_matches: dict[str, set[str]] = {}
    for req in required_zones:
        req_d = _as_dict_req(req)
        if not req_d:
            continue
        req_id = str(req_d.get("id", "")).strip()
        if not req_id:
            continue
        matches = set()
        for zone_name, zone in world_model.zones.items():
            if _matches_required_zone(zone, zone_name, req_d):
                matches.add(zone_name)
        zone_matches[req_id] = matches

    total_services = len(service_matches)
    matched_services = sum(1 for v in service_matches.values() if v)
    service_hit_rate = (matched_services / total_services) if total_services > 0 else 0.0

    total_zones = len(zone_matches)
    matched_zones = sum(1 for v in zone_matches.values() if v)
    zone_hit_rate = (matched_zones / total_zones) if total_zones > 0 else 0.0

    total_deps = 0
    matched_deps = 0
    for dep in required_dependencies:
        if not isinstance(dep, dict):
            continue
        src = str(dep.get("source_service_id", "")).strip()
        dst = str(dep.get("target_service_id", "")).strip()
        if not src or not dst:
            continue
        total_deps += 1
        if _dependency_satisfied(world_model, service_matches.get(src, set()), service_matches.get(dst, set())):
            matched_deps += 1
    dependency_hit_rate = (matched_deps / total_deps) if total_deps > 0 else 0.0

    violations = 0
    violation_details: list[dict[str, str]] = []
    for rule in forbidden_placements:
        if not isinstance(rule, dict):
            continue
        service_id = str(rule.get("service_id", "")).strip()
        zone_id = str(rule.get("zone_id", "")).strip()
        if not service_id or not zone_id:
            continue
        candidate_systems = service_matches.get(service_id, set())
        forbidden_zones = zone_matches.get(zone_id, set())
        for system_name in candidate_systems:
            system = world_model.systems.get(system_name)
            deployed_zone = system.deploy.zone if system and system.deploy else ""
            if deployed_zone in forbidden_zones:
                violations += 1
                violation_details.append(
                    {"service_id": service_id, "zone_id": zone_id, "system": system_name, "deployed_zone": deployed_zone}
                )

    has_requirements = (
        total_services > 0
        or total_zones > 0
        or total_deps > 0
        or bool(forbidden_placements)
    )
    if not has_requirements:
        formal_pass = False
        reference_status = "empty"
    else:
        reference_status = "evaluated"
        formal_pass = (
            (total_services == 0 or service_hit_rate >= 1.0)
            and (total_zones == 0 or zone_hit_rate >= 1.0)
            and (total_deps == 0 or dependency_hit_rate >= 1.0)
            and violations == 0
        )

    # Composite scenario-fit score: equal-weight average of coverage dimensions,
    # with a proportional penalty per placement violation (10% each, floored at 50%).
    _coverage_components = [service_hit_rate, zone_hit_rate, dependency_hit_rate]
    _raw_score = sum(_coverage_components) / len(_coverage_components) if has_requirements else 0.0
    _violation_penalty = max(0.5, 1.0 - 0.1 * violations) if violations > 0 else 1.0
    scenario_fit_score = _raw_score * _violation_penalty

    return {
        "benchmark_id": str(scenario_doc.get("id", "")),
        "reference_status": reference_status,
        "planned_service_coverage": service_hit_rate,
        "planned_zone_coverage": zone_hit_rate,
        "planned_dep_coverage": dependency_hit_rate,
        "placement_violations": violations,
        "benchmark_pass": formal_pass,
        "scenario_fit_score": scenario_fit_score,
        "vacuous_reference": not has_requirements,
        "details": {
            "matched_services": {k: sorted(v) for k, v in service_matches.items()},
            "matched_zones": {k: sorted(v) for k, v in zone_matches.items()},
            "dependency_matches": {"matched": matched_deps, "total": total_deps},
            "placement_violations_detail": violation_details,
        },
    }


def compute_deployed_scenario_fit(
    world_model: WorldModel,
    scenario_doc: dict[str, Any] | None,
    running_containers: set[str],
    container_prefix: str = "hn_",
) -> dict[str, Any]:
    """Evaluate scenario fit against *actually running* containers.

    Filters ``world_model.systems`` to only those whose container is in
    *running_containers*, then delegates to ``compute_formal_scenario_fit``.
    """
    if scenario_doc is None:
        return compute_formal_scenario_fit(world_model, None)

    # Build the set of system names that are actually running
    running_system_names: set[str] = set()
    for cname in running_containers:
        # Strip prefix: "hn_postgres_db" → "postgres_db"
        clean = cname.removeprefix(container_prefix) if container_prefix else cname
        if clean in world_model.systems:
            running_system_names.add(clean)
        elif cname in world_model.systems:
            running_system_names.add(cname)

    # Build a filtered WorldModel with only running systems
    from copy import copy
    deployed_wm = copy(world_model)
    deployed_wm.systems = {
        k: v for k, v in world_model.systems.items()
        if k in running_system_names
    }

    result = compute_formal_scenario_fit(deployed_wm, scenario_doc)
    result["evaluation_scope"] = "deployed"
    result["running_system_count"] = len(running_system_names)
    result["total_system_count"] = len(world_model.systems)
    # Re-key planned_* → running_* so consumers read the semantically correct keys.
    # Keep planned_* as aliases for backward-compatibility.
    for _suffix in ("service_coverage", "zone_coverage", "dep_coverage"):
        _planned_key = f"planned_{_suffix}"
        _running_key = f"running_{_suffix}"
        if _planned_key in result:
            result[_running_key] = result[_planned_key]
    return result


def _dependency_satisfied(world_model: WorldModel, source_names: set[str], target_names: set[str]) -> bool:
    if not source_names or not target_names:
        return False
    for src_name in source_names:
        system = world_model.systems.get(src_name)
        depends_on = set(system.deploy.depends_on) if system and system.deploy else set()
        if depends_on.intersection(target_names):
            return True
    return False


def _matches_required_service(system: Any, system_name: str, req: dict[str, Any]) -> bool:
    if bool(req.get("deployable_only", False)) and not getattr(system, "deploy", None):
        return False
    if not _matches_any(getattr(getattr(system, "kind", None), "value", ""), req.get("kinds_any")):
        return False

    archetype = ""
    if getattr(system, "deploy", None):
        archetype = str(getattr(system.deploy, "catalog_archetype", "") or "")
        if not archetype:
            archetype = str(getattr(system.deploy, "image", "") or "")
    if not _matches_any(archetype, req.get("archetypes_any")):
        return False

    role = ""
    if getattr(system, "simulate", None):
        role = str(getattr(system.simulate, "role", "") or "")
    if not _matches_any(role, req.get("roles_any")):
        return False

    if not _matches_any(system_name, req.get("names_any")):
        return False
    return True


def _matches_required_zone(zone: Any, zone_name: str, req: dict[str, Any]) -> bool:
    if not _matches_any(zone_name, req.get("names_any")):
        return False

    exposure = ""
    if getattr(zone, "simulate", None):
        exposure = str(getattr(zone.simulate, "exposure", "") or "")
    if not exposure and getattr(zone, "deploy", None):
        exposure = "internal" if bool(getattr(zone.deploy, "internal", True)) else "public"
    if not _matches_any(exposure, req.get("exposure_any")):
        return False

    req_internal = req.get("internal")
    if req_internal is not None and getattr(zone, "deploy", None):
        if bool(getattr(zone.deploy, "internal", True)) != bool(req_internal):
            return False
    return True


def _matches_any(value: str, patterns: Any) -> bool:
    """Check if *value* matches any of *patterns* (word-boundary aware).

    Uses ``\\b`` word boundaries so that "go" does NOT match "mongo",
    but "postgres" still matches "postgresql" (``\\b`` fires at the end).
    Underscore counts as a word char, so "api_gateway" matches "gateway".
    """
    import re as _re

    values = _as_list(patterns)
    if not values:
        return True
    v = str(value).lower()
    for p in values:
        ps = str(p).lower().strip()
        if not ps:
            continue
        # Use word boundary on the pattern side so short patterns
        # don't spuriously match inside longer unrelated words.
        if _re.search(rf'\b{_re.escape(ps)}', v):
            return True
    return False


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]

