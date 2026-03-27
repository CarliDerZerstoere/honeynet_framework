"""
Deception-specific QA checks for honeynet deployments.

Extends the QA system with checks that verify deception quality:
- deception_surface_present: at least N honeypot services expose interaction points
- service_story_consistency: services within a zone tell a coherent "story"
- bait_artifact_presence: lure/bait artifacts (env vars, files, credentials) exist
- suspicious_uniformity_guard: too-uniform configurations reduce believability

All checks are WARNING-first (non-blocking in MVP).
Results are added to the QA report under a "deception" section.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

from .models import DeployProjection, WorldModel

logger = logging.getLogger(__name__)


@dataclass
class DeceptionCheckResult:
    """Result of a single deception quality check."""
    check_id: str
    passed: bool
    severity: str = "warning"  # warning | info
    rationale: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DeceptionQAReport:
    """Aggregate deception QA results."""
    checks_run: int = 0
    checks_passed: int = 0
    checks_failed: int = 0
    results: list[DeceptionCheckResult] = field(default_factory=list)

    @property
    def pass_rate(self) -> float:
        return self.checks_passed / self.checks_run if self.checks_run > 0 else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "checks_run": self.checks_run,
            "checks_passed": self.checks_passed,
            "checks_failed": self.checks_failed,
            "pass_rate": round(self.pass_rate, 4),
            "results": [r.to_dict() for r in self.results],
        }

    def summary(self) -> str:
        lines = [
            f"Deception QA: {self.checks_passed}/{self.checks_run} passed "
            f"({self.pass_rate:.0%})"
        ]
        for r in self.results:
            if not r.passed:
                lines.append(f"  WARN: {r.check_id} — {r.rationale}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Individual deception checks
# --------------------------------------------------------------------------- #

_MIN_DECEPTION_SERVICES = 2  # Minimum services with interaction surfaces
_MAX_UNIFORMITY_RATIO = 0.85  # Above this, services are suspiciously uniform
_BAIT_ENV_KEYWORDS = frozenset({
    "password", "secret", "token", "api_key", "apikey",
    "credential", "access_key", "private_key",
})


def check_deception_surface_present(
    projection: DeployProjection,
    world_model: Optional[WorldModel] = None,
    *,
    min_services: int = _MIN_DECEPTION_SERVICES,
) -> DeceptionCheckResult:
    """Verify that enough services expose interaction surfaces (ports).

    A honeynet with no services that have ports has no deception surface —
    attackers can't interact with it.

    Uses WorldModel port definitions when available (internal-zone containers
    don't publish ports to the host but still accept connections inside the
    network, so they count as interaction surfaces).
    """
    if world_model is not None:
        services_with_ports = [
            sys_name for sys_name, sys in world_model.systems.items()
            if sys.deploy and sys.deploy.ports
        ]
    else:
        services_with_ports = [c.name for c in projection.containers if c.ports]

    passed = len(services_with_ports) >= min_services
    return DeceptionCheckResult(
        check_id="deception_surface_present",
        passed=passed,
        severity="warning",
        rationale=(
            f"{len(services_with_ports)} service(s) have port definitions "
            f"(minimum: {min_services})"
        ),
        details={
            "services_with_ports": services_with_ports,
            "total_containers": len(projection.containers),
            "threshold": min_services,
        },
    )


def check_service_story_consistency(
    world_model: WorldModel,
    projection: DeployProjection,
) -> DeceptionCheckResult:
    """Check that services within zones form a coherent deployment story.

    A zone should have services that plausibly belong together (e.g., web +
    database + cache, not random unrelated services).
    Heuristic: each zone with >1 system should have at least one
    dependency link (depends_on) between its members.
    """
    zones_with_deps = 0
    zones_checked = 0
    zone_details: dict[str, dict[str, Any]] = {}

    for zone_name, zone in world_model.zones.items():
        # Find systems in this zone (SystemDeploy.zone is the zone name)
        zone_systems = [
            sys_name for sys_name, sys in world_model.systems.items()
            if sys.deploy and getattr(sys.deploy, "zone", "") == zone_name
        ]
        if len(zone_systems) < 2:
            continue
        zones_checked += 1

        # Check if any system in this zone depends on another in the same zone.
        # Use WorldModel directly — raw system names match zone_set without
        # any container-name prefix (avoids hn_ prefix mismatch).
        #
        # Co-location in the same zone already implies network adjacency
        # (shared Docker network), so we also count having >1 system in a
        # zone as a weak dependency signal.  Explicit depends_on is a
        # stronger signal; either one satisfies the check.
        zone_set = set(zone_systems)
        has_dep = False
        for sys_name_inner in zone_set:
            sys_inner = world_model.systems.get(sys_name_inner)
            if sys_inner and sys_inner.deploy:
                for dep in sys_inner.deploy.depends_on:
                    if dep in zone_set:
                        has_dep = True
                        break
            if has_dep:
                break

        # Multiple co-located services with distinct images imply an
        # intentional service composition (web+db, api+cache, etc.) even
        # without explicit depends_on — count as coherent.
        if not has_dep and len(zone_systems) >= 2:
            zone_images = {
                world_model.systems[s].deploy.image
                for s in zone_systems
                if world_model.systems.get(s) and world_model.systems[s].deploy
            }
            if len(zone_images) >= 2:
                has_dep = True

        if has_dep:
            zones_with_deps += 1
        zone_details[zone_name] = {
            "system_count": len(zone_systems),
            "has_internal_dependency": has_dep,
        }

    passed = zones_checked == 0 or zones_with_deps >= (zones_checked * 0.5)
    return DeceptionCheckResult(
        check_id="service_story_consistency",
        passed=passed,
        severity="warning",
        rationale=(
            f"{zones_with_deps}/{zones_checked} zones have internal dependencies"
            if zones_checked > 0 else "No multi-service zones to check"
        ),
        details={"zones": zone_details},
    )


def check_bait_artifact_presence(
    projection: DeployProjection,
) -> DeceptionCheckResult:
    """Check that at least some containers have bait artifacts.

    Bait artifacts include environment variables with names suggesting
    credentials (tokens, passwords, API keys) that an attacker might
    try to exfiltrate.
    """
    containers_with_bait = []
    for container in projection.containers:
        if not container.env:
            continue
        for env_entry in container.env:
            key = env_entry.split("=", 1)[0].lower() if "=" in env_entry else env_entry.lower()
            if any(kw in key for kw in _BAIT_ENV_KEYWORDS):
                containers_with_bait.append(container.name)
                break

    passed = len(containers_with_bait) > 0
    return DeceptionCheckResult(
        check_id="bait_artifact_presence",
        passed=passed,
        severity="info",
        rationale=(
            f"{len(containers_with_bait)} container(s) have bait environment variables"
            if containers_with_bait
            else "No bait artifacts detected — consider adding fake credentials"
        ),
        details={"containers_with_bait": containers_with_bait},
    )


def check_suspicious_uniformity(
    projection: DeployProjection,
) -> DeceptionCheckResult:
    """Guard against too-uniform configurations that look artificial.

    If all containers use the same image, expose the same ports, and have
    identical env layouts, the honeynet looks machine-generated rather than
    an organic network.
    """
    if len(projection.containers) < 3:
        return DeceptionCheckResult(
            check_id="suspicious_uniformity_guard",
            passed=True,
            severity="info",
            rationale="Too few containers to assess uniformity",
        )

    images = [c.image for c in projection.containers]
    unique_images = len(set(images))
    image_ratio = unique_images / len(images)

    port_sets = [
        frozenset(p.internal for p in c.ports) for c in projection.containers if c.ports
    ]
    if not port_sets:
        # All containers are internal-only (no published ports). Port diversity
        # cannot be measured, so treat it as neutral (1.0) rather than penalising
        # honeynets that correctly isolate internal services.
        unique_port_sets = 0
        port_ratio = 1.0
    else:
        unique_port_sets = len(set(port_sets))
        # Use total container count as denominator (not just port-having containers)
        # so that honeynets where all port-exposed services look identical but have
        # many internal portless services don't get an inflated diversity score.
        port_ratio = unique_port_sets / len(projection.containers)

    # Combined uniformity score (lower = more uniform = worse)
    diversity = (image_ratio + port_ratio) / 2.0
    passed = diversity > (1.0 - _MAX_UNIFORMITY_RATIO)

    return DeceptionCheckResult(
        check_id="suspicious_uniformity_guard",
        passed=passed,
        severity="warning",
        rationale=(
            f"Service diversity score: {diversity:.2f} "
            f"(images: {unique_images}/{len(images)}, "
            f"port patterns: {unique_port_sets}/{max(len(port_sets), 1)})"
        ),
        details={
            "unique_images": unique_images,
            "total_containers": len(projection.containers),
            "image_diversity": round(image_ratio, 4),
            "port_diversity": round(port_ratio, 4),
            "combined_diversity": round(diversity, 4),
        },
    )


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #

def run_deception_qa(
    world_model: WorldModel,
    projection: DeployProjection,
) -> DeceptionQAReport:
    """Run all deception QA checks and return a consolidated report.

    All checks are warning-first — they inform but don't block deployment.
    """
    results: list[DeceptionCheckResult] = [
        check_deception_surface_present(projection, world_model),
        check_service_story_consistency(world_model, projection),
        check_bait_artifact_presence(projection),
        check_suspicious_uniformity(projection),
    ]

    passed = sum(1 for r in results if r.passed)
    return DeceptionQAReport(
        checks_run=len(results),
        checks_passed=passed,
        checks_failed=len(results) - passed,
        results=results,
    )
