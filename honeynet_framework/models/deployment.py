"""
Deployment result models.
"""

from dataclasses import dataclass, field
from typing import Optional

from .enums import DeploymentStatus
from .world_model import WorldModel
from .projection import DeployProjection


@dataclass
class RuntimeSummary:
    """Runtime health status after deploy."""
    total_expected: int = 0
    total_running: int = 0
    failed_containers: list[str] = field(default_factory=list)

    @property
    def is_healthy(self) -> bool:
        return (
            self.total_expected > 0
            and len(self.failed_containers) == 0
            and self.total_running == self.total_expected
        )

    def human_summary(self) -> str:
        if self.is_healthy:
            return f"All {self.total_running}/{self.total_expected} containers running"
        lines = [f"{self.total_running}/{self.total_expected} containers running"]
        if self.failed_containers:
            lines.append(f"  Failed: {', '.join(self.failed_containers)}")
        return "\n".join(lines)


@dataclass
class DeploymentResult:
    """Result of the honeynet deployment.

    success is True for DEPLOYED and RUNTIME_DEGRADED (run finished; runtime/QA may
    still be imperfect). For a strict green check use status == DEPLOYED; see
    docs/ARCHITECTURE_STATUS.md (API semantics).
    """
    status: DeploymentStatus
    failure_stage: Optional[str] = None
    run_id: Optional[str] = None
    world_model: Optional[WorldModel] = None
    deploy_projection: Optional[DeployProjection] = None
    terraform_code: Optional[str] = None
    running_containers: int = 0
    expected_containers: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    summary: str = ""
    runtime_summary: Optional[RuntimeSummary] = None
    metrics: Optional[dict] = None
    qa_report: Optional[dict] = None

    @property
    def success(self) -> bool:
        """True if the pipeline completed without *failed* status (includes degraded runtime)."""
        return self.status in {
            DeploymentStatus.DEPLOYED,
            DeploymentStatus.RUNTIME_DEGRADED,
        }

    @property
    def strict_success(self) -> bool:
        """True only if status is DEPLOYED — all containers up and QA green.

        Use this for automation gates where degraded runtime should block promotion.
        """
        return self.status == DeploymentStatus.DEPLOYED
