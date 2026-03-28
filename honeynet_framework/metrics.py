"""
Deployment metrics tracking for honeynet thesis evaluation.

Tracks two metric groups:
1. Deployability: world model validity, config validation, plan/deploy success,
   runtime health checks, container start rate
2. Scenario Fit: planned/running service coverage, zone coverage, dependency coverage

Schema version 4: separated prompt-fit vs formal-benchmark metrics, fixed
container_start_rate denominator semantics, added final_system_count,
image_first_pass_rate, and deterministic correction counts.
"""

from dataclasses import dataclass, field

from .utils import utc_now_iso as _utc_now_iso


@dataclass
class DeploymentMetrics:
    """Tracks all deployability and scenario-fit metrics."""

    # Observability (min bundle — pipeline run info)
    run_id: str = ""
    work_dir: str = ""
    tofu_version: str = ""
    started_at_utc: str = ""  # Pipeline start timestamp (set by orchestrator)
    failure_stage: str = ""   # Earliest pipeline stage that failed
    path_a_diagnostics_collected: bool = False
    path_a_diagnostics_artifact: str = ""
    path_a_failing_resources_count: int = 0
    path_a_docker_targets_count: int = 0
    path_a_error_class: str = ""  # Coarse error bucket for routing/diagnostics

    # Deploy progress (for blocked_before_deploy vs deploy_eligible KPIs)
    deploy_completed: bool = False  # True once tofu apply succeeds (full or partial)
    partial_deploy: bool = False    # True when only a subset of containers was targeted

    # Deployability metrics
    world_model_valid: bool = False           # LLM extraction produced a valid world model
    validation_first_pass_success: bool = False  # Validation passed without needing repair
    validation_repair_attempted: bool = False
    validation_repair_success: bool = False
    config_validation_pass: bool = False      # Static config analysis passed (tofu validate)
    plan_success: bool = False                # Deployment plan generation succeeded
    deploy_success: bool = False              # Full apply succeeded (False for partial deploys)
    deploy_first_attempt: bool = False        # Deploy succeeded on the first try (no retry)
    deploy_retried: bool = False              # Deploy needed at least one retry
    deploy_retry_count: int = 0              # Accumulated retry attempts (import + apply-repair)
    runtime_verification_success: bool = False  # All expected containers are running and healthy
    health_check_rate: float = 0.0           # Fraction of connectivity/health checks that passed
    container_start_rate: float = 0.0        # running / originally planned (denominator never shifts)
    expected_containers: int = 0             # Original planned container count (set once, never updated after drops)
    planned_containers: int = 0              # Alias: containers originally planned before any systems were dropped
    dropped_containers: int = 0              # Systems removed during repair/probe
    running_containers: int = 0
    health_checks_total: int = 0
    health_checks_passed: int = 0

    # Explicit evaluation status (not_run | measured | error | not_applicable)
    health_check_status: str = "not_run"     # Status of health/connectivity check evaluation
    container_health_status: str = "not_run" # Status of runtime container health evaluation

    # Scenario fit metrics
    zone_count: int = 0
    system_count: int = 0                    # System count at extraction time (before drops/repairs)
    final_system_count: int = 0              # System count after all repairs and drops
    # Internal consistency: fraction of declared depends_on edges pointing to existing systems
    model_dep_rate: float = 0.0
    image_first_pass_rate: float = 0.0       # First-pass image resolution rate (diagnostic, before repair)
    image_check_rate: float = 0.0            # Post-repair image resolution rate (final)

    # Prompt-fit evaluation (always computed from NL prompt analysis)
    prompt_fit_service_coverage: float = 0.0
    prompt_fit_zone_coverage: float = 0.0
    prompt_fit_pass: bool = False            # service_recall >= 0.7 AND zone_recall >= 0.5

    # Formal benchmark evaluation (computed only when benchmark reference exists)
    formal_service_coverage: float = 0.0
    formal_zone_coverage: float = 0.0
    formal_benchmark_pass: bool = False      # All formal requirements met (100% coverage, 0 violations)

    # Composite fields (formal wins when present, else prompt-fit)
    planned_service_coverage: float = 0.0    # Effective service coverage used for reporting
    running_service_coverage: float = 0.0    # Fraction of benchmark-required services actually running
    planned_zone_coverage: float = 0.0       # Effective zone coverage used for reporting
    running_zone_coverage: float = 0.0       # Fraction of benchmark-required zones with >=1 running container
    planned_dep_coverage: float = 0.0        # Fraction of benchmark-required dependencies satisfied in the plan
    running_dep_coverage: float = 0.0        # Fraction of benchmark-required dependencies where both ends are running
    benchmark_dep_required: int = 0          # Number of dependencies the benchmark requires (0 = none defined)
    placement_violations: int = 0            # Services deployed in zones where they are forbidden
    benchmark_pass: bool = False             # Composite: formal_benchmark_pass when ref exists, else prompt_fit_pass
    benchmark_status: str = "not_configured" # not_configured | measured | error | empty_reference
    benchmark_id: str = ""
    benchmark_ref: str = ""

    # Per-stage timing (stage name → wall-clock seconds)
    stage_durations: dict[str, float] = field(default_factory=dict)

    # Coarse error classification (taxonomy: llm_schema_error, invalid_image,
    # port_conflict, network_error, timeout, dependency_missing, tofu_syntax, unknown)
    error_class: str = ""

    # QA pass rate broken down by check type (e.g. container_running, tcp_connect, http_status)
    health_check_rate_by_type: dict[str, float] = field(default_factory=dict)

    # Composite scenario-fit score (0.0–1.0)
    scenario_fit_score: float = 0.0

    # Repair / incidents
    repair_incident_count: int = 0
    repair_actions: list[dict] = field(default_factory=list)

    # Deterministic post-processing correction counts (for thesis analysis)
    deterministic_dep_addition_count: int = 0
    deterministic_placement_fix_count: int = 0

    # Error tracking
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        # Emit None for metrics of stages that never ran, so aggregation
        # code can distinguish "not measured" from "measured as 0%".
        health_rate = (
            round(self.health_check_rate, 4)
            if self.health_check_status == "measured"
            else None
        )
        start_rate = (
            round(self.container_start_rate, 4)
            if self.container_health_status == "measured"
            else None
        )
        dep = {
            "world_model_valid": self.world_model_valid,
            "validation_first_pass_success": self.validation_first_pass_success,
            "validation_repair_attempted": self.validation_repair_attempted,
            "validation_repair_success": self.validation_repair_success,
            "config_validation_pass": self.config_validation_pass,
            "plan_success": self.plan_success,
            "deploy_success": self.deploy_success,
            "deploy_first_attempt": self.deploy_first_attempt,
            "deploy_retried": self.deploy_retried,
            "deploy_retry_count": self.deploy_retry_count,
            "runtime_verification_success": self.runtime_verification_success,
            "health_check_rate": health_rate,
            "container_start_rate": start_rate,
            "expected_containers": self.expected_containers,
            "planned_containers": self.planned_containers,
            "dropped_containers": self.dropped_containers,
            "running_containers": self.running_containers,
            "health_checks_total": self.health_checks_total,
            "health_checks_passed": self.health_checks_passed,
            "health_check_status": self.health_check_status,
            "container_health_status": self.container_health_status,
            "health_check_rate_by_type": {
                k: round(v, 4) for k, v in self.health_check_rate_by_type.items()
            },
        }
        scen = {
            "zone_count": self.zone_count,
            "system_count": self.system_count,
            "final_system_count": self.final_system_count,
            "model_dep_rate": round(self.model_dep_rate, 4),
            "image_first_pass_rate": round(self.image_first_pass_rate, 4),
            "image_check_rate": round(self.image_check_rate, 4),
            "prompt_fit_service_coverage": round(self.prompt_fit_service_coverage, 4),
            "prompt_fit_zone_coverage": round(self.prompt_fit_zone_coverage, 4),
            "prompt_fit_pass": self.prompt_fit_pass,
            "formal_service_coverage": round(self.formal_service_coverage, 4),
            "formal_zone_coverage": round(self.formal_zone_coverage, 4),
            "formal_benchmark_pass": (
                None
                if self.benchmark_status == "not_configured"
                else self.formal_benchmark_pass
            ),
            "planned_service_coverage": round(self.planned_service_coverage, 4),
            "running_service_coverage": round(self.running_service_coverage, 4),
            "planned_zone_coverage": round(self.planned_zone_coverage, 4),
            "running_zone_coverage": round(self.running_zone_coverage, 4),
            "planned_dep_coverage": round(self.planned_dep_coverage, 4),
            "running_dep_coverage": round(self.running_dep_coverage, 4),
            "benchmark_dep_required": self.benchmark_dep_required,
            "placement_violations": self.placement_violations,
            "benchmark_pass": (
                None
                if self.benchmark_status == "not_configured"
                else self.benchmark_pass
            ),
            "benchmark_status": self.benchmark_status,
            "benchmark_id": self.benchmark_id,
            "benchmark_ref": self.benchmark_ref,
            "scenario_fit_score": round(self.scenario_fit_score, 4),
        }
        obs = {
            "run_id": self.run_id,
            "work_dir": self.work_dir,
            "tofu_version": self.tofu_version,
            "started_at_utc": self.started_at_utc,
            "failure_stage": self.failure_stage,
            "error_class": self.error_class,
            "deploy_completed": self.deploy_completed,
            "repair_incident_count": self.repair_incident_count,
            "repair_actions": self.repair_actions,
            "deterministic_dep_addition_count": self.deterministic_dep_addition_count,
            "deterministic_placement_fix_count": self.deterministic_placement_fix_count,
            "stage_durations": {
                k: round(v, 3) for k, v in self.stage_durations.items()
            },
            "path_a": {
                "diagnostics_collected": self.path_a_diagnostics_collected,
                "diagnostics_artifact": self.path_a_diagnostics_artifact,
                "failing_resources_count": self.path_a_failing_resources_count,
                "docker_targets_count": self.path_a_docker_targets_count,
                "error_class": self.path_a_error_class,
            },
        }
        blocked = self._compute_blocked_before_deploy()
        return {
            "schema_version": 4,
            "recorded_at_utc": _utc_now_iso(),
            "envelope": {
                "metrics_schema_version": 4,
            },
            "observability": obs,
            "deployability": dep,
            "scenario_fit": scen,
            "pipeline_kpis": {
                "blocked_before_deploy": blocked,
                "deploy_eligible": self.world_model_valid and not blocked,
            },
            "errors": self.errors,
        }

    def _compute_blocked_before_deploy(self) -> bool:
        """True when the run failed before a successful deploy (gate for downstream KPIs)."""
        if self.deploy_completed:
            return False
        fs = (self.failure_stage or "").strip().lower()
        if not fs:
            # Unknown/missing stage: treat as blocked to avoid falsely marking as deploy_eligible
            return True
        # Completed / runtime work after deploy — not blocked
        if fs in ("completed", "runtime_verify", "none"):
            return False
        return True

    def summary(self) -> str:
        lines = [
            "Deployment Metrics:",
            f"  World Model Valid:     {'YES' if self.world_model_valid else 'NO'}",
            f"  Config Validation:     {'PASS' if self.config_validation_pass else 'FAIL'}",
            f"  Plan Success:          {'YES' if self.plan_success else 'NO'}",
            f"  Deploy Success:        {'YES' if self.deploy_success else 'NO'}",
            f"  Runtime Health:        {'PASS' if self.runtime_verification_success else 'FAIL'}",
            f"  Container Start Rate:  {self.running_containers}/{self.expected_containers} ({self.container_start_rate:.0%})",
            f"  Health Check Rate:     {self.health_checks_passed}/{self.health_checks_total} ({self.health_check_rate:.0%})",
            f"  Health check status:   {self.health_check_status}",
            f"  Container health:      {self.container_health_status}",
            f"  Benchmark status:      {self.benchmark_status}",
            f"  Zones: {self.zone_count}  Systems: {self.system_count} (final: {self.final_system_count})",
            f"  Image Check Rate:      {self.image_check_rate:.0%} (first pass: {self.image_first_pass_rate:.0%})",
            f"  Model Dep Rate:        {self.model_dep_rate:.0%}",
            f"  Scenario Fit Score:    {self.scenario_fit_score:.2f}",
            f"  Error Class:           {self.error_class or 'n/a'}",
        ]
        if self.health_check_rate_by_type:
            for ct, rate in self.health_check_rate_by_type.items():
                lines.append(f"  QA {ct}: {rate:.0%}")
        if self.stage_durations:
            total_s = sum(self.stage_durations.values())
            lines.append(f"  Total Duration:        {total_s:.1f}s")
        if self.errors:
            lines.append(f"  Errors: {len(self.errors)}")
            for e in self.errors[:5]:
                lines.append(f"    - {e}")
        return "\n".join(lines)
