"""
Benchmark runner — batch-evaluate scenarios from the benchmark corpus.

Usage (CLI):
    honeynet benchmark run --corpus benchmarks/corpus_v1 --prompt-variant concise

Usage (Python):
    from honeynet_framework.benchmark_runner import run_benchmark_corpus
    results = await run_benchmark_corpus(corpus_dir, output_dir, config)
"""

from __future__ import annotations

import asyncio
import html as _html
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from .orchestrator import HoneynetOrchestrator, OrchestratorConfig
from .models import DeploymentStatus
from .utils import atomic_write_text

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class BenchmarkResult:
    """Result of a single benchmark scenario run."""
    scenario_id: str
    difficulty: str
    prompt_variant: str
    prompt_text: str = ""

    # Scenario fit (pre-deployment, from formal evaluation against plan)
    planned_service_coverage: float = 0.0   # Fraction of benchmark-required services in deployment plan
    planned_zone_coverage: float = 0.0      # Fraction of benchmark-required zones in deployment plan
    planned_dep_coverage: float = 0.0       # Fraction of benchmark-required dependencies in deployment plan
    benchmark_dep_required: int = 0        # Total deps the benchmark requires (0 = none defined → n/a)
    placement_violations: int = 0           # Services placed in forbidden zones
    benchmark_pass: bool = False            # All benchmark requirements met in plan

    # Deployability
    deploy_success: bool = False
    deploy_status: str = ""
    containers_planned: int = 0
    containers_running: int = 0
    containers_dropped: int = 0

    # Post-deployment scenario fit (evaluated against actually running containers)
    running_service_coverage: float = 0.0   # Fraction of benchmark-required services actually running
    running_zone_coverage: float = 0.0      # Fraction of benchmark-required zones with running containers
    running_dep_coverage: float = 0.0       # Fraction of benchmark-required dependencies where both ends run

    # Composite scenario-fit score (0.0–1.0)
    scenario_fit_score: float = 0.0

    # Cost / timing
    duration_s: float = 0.0
    stage_durations: dict = field(default_factory=dict)
    failure_stage: str = ""
    error_class: str = ""
    errors: list[str] = field(default_factory=list)

    # Scenario complexity (from benchmark reference)
    scenario_complexity: dict = field(default_factory=dict)

    # Dry-run mode: only validated YAML, no deploy
    dry_run: bool = False
    dry_run_valid: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CorpusReport:
    """Aggregate report over all benchmark results."""
    total_scenarios: int = 0
    scenarios_passed: int = 0
    scenarios_failed: int = 0
    scenarios_skipped: int = 0

    # Per-difficulty aggregates
    by_difficulty: dict[str, dict[str, Any]] = field(default_factory=dict)

    # Overall averages
    mean_planned_service_coverage: float = 0.0
    mean_planned_zone_coverage: float = 0.0
    mean_planned_dep_coverage: float = 0.0
    mean_running_service_coverage: float = 0.0
    mean_scenario_fit_score: float = 0.0
    deploy_success_rate: float = 0.0
    benchmark_pass_rate: float = 0.0

    # Per-complexity aggregates (small / medium / large)
    by_complexity: dict[str, dict[str, Any]] = field(default_factory=dict)

    results: list[BenchmarkResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("results", None)
        d["results"] = [r.to_dict() for r in self.results]
        return d


# ---------------------------------------------------------------------------
# Manifest / scenario loading
# ---------------------------------------------------------------------------

def load_manifest(corpus_dir: Path) -> dict[str, Any]:
    """Load and validate the benchmark manifest."""
    manifest_path = corpus_dir / "manifest.yaml"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "scenarios" not in data:
        raise ValueError(f"Invalid manifest: {manifest_path}")
    return data


def load_scenario(corpus_dir: Path, scenario_entry: dict) -> dict[str, Any]:
    """Load a single scenario YAML from the corpus."""
    rel_path = scenario_entry.get("path", "")
    scenario_path = corpus_dir / rel_path
    if not scenario_path.exists():
        raise FileNotFoundError(f"Scenario not found: {scenario_path}")
    data = yaml.safe_load(scenario_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Invalid scenario YAML: {scenario_path}")
    data["_path"] = str(scenario_path)
    return data


def get_prompt(scenario: dict, variant: str = "concise") -> str:
    """Extract prompt text for a given variant from a scenario."""
    prompts = scenario.get("prompts", [])
    for p in prompts:
        if isinstance(p, dict) and p.get("id") == variant:
            return (p.get("text") or "").strip()
    # Fallback: first prompt
    if prompts and isinstance(prompts[0], dict):
        return (prompts[0].get("text") or "").strip()
    return ""


def filter_scenarios(
    manifest: dict,
    difficulty: str | None = None,
    scenario_id: str | None = None,
) -> list[dict]:
    """Filter scenarios from manifest by difficulty and/or scenario ID."""
    scenarios = manifest.get("scenarios", [])
    if scenario_id:
        scenarios = [s for s in scenarios if s.get("id") == scenario_id]
    if difficulty:
        scenarios = [s for s in scenarios if s.get("difficulty") == difficulty]
    return scenarios


# ---------------------------------------------------------------------------
# Dry-run validation
# ---------------------------------------------------------------------------

def validate_scenario_yaml(scenario: dict) -> list[str]:
    """Validate a scenario YAML structure. Returns list of issues (empty = valid)."""
    issues: list[str] = []
    if not scenario.get("id"):
        issues.append("Missing 'id' field")
    if not scenario.get("prompts"):
        issues.append("Missing 'prompts' section")
    ref = scenario.get("reference", {})
    if not isinstance(ref, dict):
        issues.append("'reference' must be a dict")
    elif not ref.get("required_services") and not ref.get("required_zones"):
        issues.append("reference has no required_services or required_zones")
    return issues


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def run_benchmark_corpus(
    corpus_dir: Path,
    output_dir: Path,
    config: OrchestratorConfig,
    *,
    prompt_variant: str = "detailed",
    difficulty_filter: str | None = None,
    scenario_filter: str | None = None,
    dry_run: bool = False,
) -> list[BenchmarkResult]:
    """Run all matching scenarios from a benchmark corpus.

    Parameters
    ----------
    corpus_dir : directory containing manifest.yaml and scenario YAMLs
    output_dir : directory for per-scenario results and aggregate report
    config : base OrchestratorConfig (work_dir will be overridden per scenario)
    prompt_variant : which prompt to use from each scenario YAML
    difficulty_filter : only run scenarios with this difficulty
    scenario_filter : only run this specific scenario ID
    dry_run : validate YAMLs without deploying
    """
    manifest = load_manifest(corpus_dir)
    scenarios = filter_scenarios(manifest, difficulty_filter, scenario_filter)

    if not scenarios:
        logger.warning("No scenarios matched the filter criteria")
        return []

    logger.info(
        "Benchmark: %d scenario(s) to run (difficulty=%s, variant=%s, dry_run=%s)",
        len(scenarios), difficulty_filter or "all", prompt_variant, dry_run,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[BenchmarkResult] = []

    for idx, entry in enumerate(scenarios, 1):
        scenario_id = entry.get("id", f"unknown_{idx}")
        difficulty = entry.get("difficulty", "unknown")
        logger.info("--- Scenario %d/%d: %s [%s] ---", idx, len(scenarios), scenario_id, difficulty)

        try:
            scenario = load_scenario(corpus_dir, entry)
        except (FileNotFoundError, ValueError) as e:
            logger.error("Failed to load scenario %s: %s", scenario_id, e)
            results.append(BenchmarkResult(
                scenario_id=scenario_id,
                difficulty=difficulty,
                prompt_variant=prompt_variant,
                errors=[str(e)],
                failure_stage="load",
            ))
            continue

        prompt = get_prompt(scenario, prompt_variant)
        if not prompt:
            logger.error("No prompt found for variant '%s' in %s", prompt_variant, scenario_id)
            results.append(BenchmarkResult(
                scenario_id=scenario_id,
                difficulty=difficulty,
                prompt_variant=prompt_variant,
                errors=[f"No prompt for variant '{prompt_variant}'"],
                failure_stage="load",
            ))
            continue

        if dry_run:
            issues = validate_scenario_yaml(scenario)
            results.append(BenchmarkResult(
                scenario_id=scenario_id,
                difficulty=difficulty,
                prompt_variant=prompt_variant,
                prompt_text=prompt,
                dry_run=True,
                dry_run_valid=len(issues) == 0,
                errors=issues,
            ))
            continue

        # Set up per-scenario work directory and benchmark reference
        scenario_work_dir = output_dir / scenario_id
        scenario_work_dir.mkdir(parents=True, exist_ok=True)

        from dataclasses import replace
        scenario_config = replace(
            config,
            work_dir=scenario_work_dir,
            benchmark_reference_path=Path(scenario.get("_path", "")),
        )

        result = await _run_single_scenario(
            scenario_id=scenario_id,
            difficulty=difficulty,
            prompt_variant=prompt_variant,
            prompt=prompt,
            config=scenario_config,
            scenario_ref=scenario,
        )
        results.append(result)

        # Save per-scenario result
        result_path = scenario_work_dir / "benchmark_result.json"
        atomic_write_text(result_path, json.dumps(result.to_dict(), indent=2))

        # Tear down deployed Docker resources so the next scenario starts clean.
        # Without this, Docker's limited bridge-network address pool (~31 networks)
        # gets exhausted after ~7 scenarios, causing all subsequent deploys to fail
        # with "all predefined address pools have been fully subnetted".
        await _teardown_scenario(scenario_work_dir, scenario_id, config)

    # Generate aggregate report
    report = _aggregate_results(results)
    report_path = generate_benchmark_report(report, output_dir)
    logger.info("Benchmark complete: %d/%d passed. Report: %s",
                report.scenarios_passed, report.total_scenarios, report_path)

    return results


async def _teardown_scenario(
    work_dir: Path, scenario_id: str, config: OrchestratorConfig
) -> None:
    """Destroy Docker resources from a completed scenario.

    Runs ``tofu destroy`` against the scenario's work directory to release
    Docker networks and containers.  This prevents address-pool exhaustion
    when many scenarios run sequentially (Docker defaults to ~31 bridge
    networks).  If ``tofu destroy`` fails, a best-effort ``docker`` cleanup
    is attempted for containers matching the ``hn_`` prefix.
    """
    state_file = work_dir / "terraform.tfstate"
    if not state_file.exists():
        return  # nothing was deployed (early failure)

    try:
        from .deployer import TerraformDeployer, DeployerConfig
        deployer = TerraformDeployer(DeployerConfig(
            work_dir=work_dir,
            use_docker=config.use_docker_for_tofu,
            docker_container=config.tofu_container,
            auto_approve=True,
        ))
        # Ensure provider state exists before destroy
        tf_dir = work_dir / ".terraform"
        if not tf_dir.exists():
            await deployer.init()
        result = await deployer.destroy()
        if result.success:
            logger.info("Teardown %s: destroyed successfully", scenario_id)
        else:
            logger.warning("Teardown %s: tofu destroy failed, attempting docker cleanup: %s",
                           scenario_id, (result.stderr or "")[:200])
            await _docker_force_cleanup(work_dir)
    except Exception as e:
        logger.warning("Teardown %s: exception during destroy: %s", scenario_id, e)
        await _docker_force_cleanup(work_dir)


async def _docker_force_cleanup(work_dir: Path) -> None:
    """Best-effort removal of hn_ containers and their networks via docker CLI.

    Reads the tofu state to find exact resource names when possible,
    otherwise falls back to listing containers with the ``hn_`` prefix.
    """
    import asyncio
    import functools
    import subprocess

    loop = asyncio.get_running_loop()

    # Remove all hn_ containers (from any scenario)
    try:
        ps = await loop.run_in_executor(None, functools.partial(
            subprocess.run,
            ["docker", "ps", "-a", "--filter", "name=hn_", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=10,
        ))
        containers = [c.strip() for c in ps.stdout.splitlines() if c.strip()]
        if containers:
            await loop.run_in_executor(None, functools.partial(
                subprocess.run,
                ["docker", "rm", "-f"] + containers,
                capture_output=True, timeout=30,
            ))
            logger.info("Force-removed %d hn_ containers", len(containers))
    except Exception as e:
        logger.warning("docker container cleanup failed: %s", e)

    # Remove only hn_ prefixed networks (framework-created networks)
    try:
        ns = await loop.run_in_executor(None, functools.partial(
            subprocess.run,
            ["docker", "network", "ls", "--format", "{{.Name}}"],
            capture_output=True, text=True, timeout=10,
        ))
        removable = [n.strip() for n in ns.stdout.splitlines()
                     if n.strip() and n.strip().startswith("hn_")]
        if removable:
            # Remove in batches to avoid argument-list limits
            for i in range(0, len(removable), 20):
                batch = removable[i:i + 20]
                await loop.run_in_executor(None, functools.partial(
                    subprocess.run,
                    ["docker", "network", "rm"] + batch,
                    capture_output=True, timeout=30,
                ))
            logger.info("Force-removed %d Docker networks", len(removable))
    except Exception as e:
        logger.warning("docker network cleanup failed: %s", e)


async def _run_single_scenario(
    scenario_id: str,
    difficulty: str,
    prompt_variant: str,
    prompt: str,
    config: OrchestratorConfig,
    scenario_ref: dict | None = None,
) -> BenchmarkResult:
    """Run a single scenario through the full deploy pipeline."""
    t0 = time.monotonic()
    result = BenchmarkResult(
        scenario_id=scenario_id,
        difficulty=difficulty,
        prompt_variant=prompt_variant,
        prompt_text=prompt,
    )

    try:
        orchestrator = HoneynetOrchestrator(config)
        deploy_result = await orchestrator.deploy(prompt)

        result.duration_s = round(time.monotonic() - t0, 2)
        result.deploy_status = deploy_result.status.value if isinstance(deploy_result.status, DeploymentStatus) else str(deploy_result.status)
        result.deploy_success = deploy_result.status in (DeploymentStatus.DEPLOYED, DeploymentStatus.RUNTIME_DEGRADED)
        result.failure_stage = deploy_result.failure_stage or ""
        result.errors = list(deploy_result.errors or [])

        # Extract metrics
        metrics = deploy_result.metrics or {}
        dep = metrics.get("deployability", {})
        scen = metrics.get("scenario_fit", {})

        result.containers_planned = int(dep.get("planned_containers", 0) or dep.get("expected_containers", 0))
        result.containers_running = int(dep.get("running_containers", 0))
        result.containers_dropped = int(dep.get("dropped_containers", 0))

        result.planned_service_coverage = float(scen.get("planned_service_coverage", 0))
        result.planned_zone_coverage = float(scen.get("planned_zone_coverage", 0))
        result.planned_dep_coverage = float(scen.get("planned_dep_coverage", 0))
        result.benchmark_dep_required = int(scen.get("benchmark_dep_required", 0))
        result.placement_violations = int(scen.get("placement_violations", 0))
        result.benchmark_pass = bool(scen.get("benchmark_pass", False))

        result.running_service_coverage = float(scen.get("running_service_coverage", 0))
        result.running_zone_coverage = float(scen.get("running_zone_coverage", 0))
        result.running_dep_coverage = float(scen.get("running_dep_coverage", 0))

        result.scenario_fit_score = float(scen.get("scenario_fit_score", 0))
        obs = metrics.get("observability", {})
        result.stage_durations = dict(obs.get("stage_durations", {}))
        result.error_class = str(obs.get("error_class", ""))

        # Extract scenario complexity from the benchmark reference
        ref = (scenario_ref or {}).get("reference", {})
        if isinstance(ref, dict):
            result.scenario_complexity = {
                "required_services_count": len(ref.get("required_services", [])),
                "required_zones_count": len(ref.get("required_zones", [])),
                "required_deps_count": len(ref.get("required_dependencies", [])),
            }

    except Exception as e:
        result.duration_s = round(time.monotonic() - t0, 2)
        result.errors = [str(e)]
        result.failure_stage = "exception"
        logger.error("Scenario %s failed with exception: %s", scenario_id, e)

    return result


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _aggregate_results(results: list[BenchmarkResult]) -> CorpusReport:
    """Compute aggregate statistics from benchmark results."""
    report = CorpusReport(results=results)
    report.total_scenarios = len(results)

    if not results:
        return report

    deployed = [r for r in results if not r.dry_run]
    dry_runs = [r for r in results if r.dry_run]

    report.scenarios_passed = sum(1 for r in deployed if r.benchmark_pass and r.deploy_success)
    report.scenarios_failed = sum(1 for r in deployed if not (r.benchmark_pass and r.deploy_success))
    report.scenarios_skipped = len(dry_runs)

    if deployed:
        report.mean_planned_service_coverage = _mean([r.planned_service_coverage for r in deployed])
        report.mean_planned_zone_coverage = _mean([r.planned_zone_coverage for r in deployed])
        report.mean_planned_dep_coverage = _mean([r.planned_dep_coverage for r in deployed])
        report.mean_running_service_coverage = _mean([r.running_service_coverage for r in deployed])
        report.mean_scenario_fit_score = _mean([r.scenario_fit_score for r in deployed])
        report.deploy_success_rate = sum(1 for r in deployed if r.deploy_success) / len(deployed)
        report.benchmark_pass_rate = sum(1 for r in deployed if r.benchmark_pass) / len(deployed)

    # Per-difficulty breakdown
    for diff in ("easy", "medium", "hard"):
        diff_results = [r for r in deployed if r.difficulty == diff]
        if not diff_results:
            continue
        report.by_difficulty[diff] = {
            "count": len(diff_results),
            "passed": sum(1 for r in diff_results if r.benchmark_pass and r.deploy_success),
            "deploy_success_rate": _mean([1.0 if r.deploy_success else 0.0 for r in diff_results]),
            "mean_planned_service_coverage": _mean([r.planned_service_coverage for r in diff_results]),
            "mean_planned_zone_coverage": _mean([r.planned_zone_coverage for r in diff_results]),
            "mean_running_service_coverage": _mean([r.running_service_coverage for r in diff_results]),
            "mean_scenario_fit_score": _mean([r.scenario_fit_score for r in diff_results]),
            "mean_duration_s": _mean([r.duration_s for r in diff_results]),
        }

    # Per-complexity breakdown (small: <10, medium: 10-19, large: 20+)
    def _complexity_bucket(r: BenchmarkResult) -> str:
        total = sum(r.scenario_complexity.get(k, 0) for k in (
            "required_services_count", "required_zones_count", "required_deps_count",
        ))
        if total < 10:
            return "small"
        elif total < 20:
            return "medium"
        return "large"

    for bucket in ("small", "medium", "large"):
        bucket_results = [r for r in deployed if _complexity_bucket(r) == bucket]
        if not bucket_results:
            continue
        report.by_complexity[bucket] = {
            "count": len(bucket_results),
            "passed": sum(1 for r in bucket_results if r.benchmark_pass and r.deploy_success),
            "deploy_success_rate": _mean([1.0 if r.deploy_success else 0.0 for r in bucket_results]),
            "mean_planned_service_coverage": _mean([r.planned_service_coverage for r in bucket_results]),
            "mean_running_service_coverage": _mean([r.running_service_coverage for r in bucket_results]),
            "mean_scenario_fit_score": _mean([r.scenario_fit_score for r in bucket_results]),
            "mean_duration_s": _mean([r.duration_s for r in bucket_results]),
        }

    return report


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def _esc(v: object) -> str:
    return _html.escape(str(v), quote=True)


def _pct(v: float) -> str:
    return f"{v * 100:.0f}%"


def _dep_pct(coverage: float, required: int) -> str:
    """Format dependency coverage: show 'n/a' when no deps are required (0/0)."""
    if required == 0:
        return '<span style="color:var(--muted)">n/a</span>'
    return _pct(coverage)


def _badge(ok: bool) -> str:
    if ok:
        return '<span style="color:#16a34a;font-weight:bold">PASS</span>'
    return '<span style="color:#dc2626;font-weight:bold">FAIL</span>'


def _diff_color(diff: str) -> str:
    return {"easy": "#22c55e", "medium": "#f59e0b", "hard": "#ef4444"}.get(diff, "#6b7280")


def _render_complexity_section(report: CorpusReport) -> str:
    """Render the 'Results by complexity' HTML section."""
    if not report.by_complexity:
        return ""
    _complexity_colors = {"small": "#22c55e", "medium": "#f59e0b", "large": "#ef4444"}
    rows = []
    for bucket in ("small", "medium", "large"):
        d = report.by_complexity.get(bucket, {})
        if not d:
            continue
        color = _complexity_colors.get(bucket, "#6b7280")
        rows.append(
            f"<tr>"
            f"<td><span style='color:{color};font-weight:bold'>{bucket}</span></td>"
            f"<td>{d['count']}</td>"
            f"<td>{d['passed']}/{d['count']}</td>"
            f"<td>{_pct(d['deploy_success_rate'])}</td>"
            f"<td>{d.get('mean_scenario_fit_score', 0):.2f}</td>"
            f"<td>{_pct(d['mean_planned_service_coverage'])}</td>"
            f"<td>{_pct(d['mean_running_service_coverage'])}</td>"
            f"<td>{d['mean_duration_s']:.0f}s</td>"
            f"</tr>"
        )
    if not rows:
        return ""
    return f"""<div class="section panel">
  <h2>Results by complexity</h2>
  <p class="muted">Scenarios grouped by total requirement count (services + zones + dependencies).
  Small: &lt;10, Medium: 10\u201319, Large: 20+.</p>
  <table>
    <thead>
    <tr><th>Complexity</th><th>Scenarios</th><th>Passed</th><th>Deploy Rate</th>
        <th>Fit Score</th><th>Svc Cov (Plan)</th><th>Svc Cov (Running)</th><th>Avg Duration</th></tr>
    </thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
</div>"""


def generate_benchmark_report(report: CorpusReport, output_dir: Path) -> Path:
    """Generate HTML + JSON benchmark report."""
    from .reporting_html import _common_css, _status_banner, _kpi_card

    output_dir.mkdir(parents=True, exist_ok=True)

    # JSON export
    json_path = output_dir / "benchmark_report.json"
    atomic_write_text(json_path, json.dumps(report.to_dict(), indent=2))

    # --- Status banner ---
    deployed = [r for r in report.results if not r.dry_run]
    n_deployed = len(deployed)
    n_pass = report.scenarios_passed
    n_deploy_ok = sum(1 for r in deployed if r.deploy_success)
    if n_deployed == 0:
        banner = _status_banner("warn", "No scenarios were deployed. Check dry-run results below.")
    elif n_pass == n_deployed:
        banner = _status_banner("ok",
            f"Benchmark passed: {n_pass}/{n_deployed} scenarios met all requirements.")
    elif n_deploy_ok >= n_deployed * 0.7:
        banner = _status_banner("warn",
            f"Benchmark partial: {n_pass}/{n_deployed} passed. "
            f"{n_deploy_ok - n_pass} deployed but failed coverage checks.")
    else:
        banner = _status_banner("bad",
            f"Benchmark failed: only {n_deploy_ok}/{n_deployed} scenarios deployed successfully.")

    # --- KPI cards ---
    def _lvl(v: float, ok: float, warn: float) -> str:
        return "ok" if v >= ok else "warn" if v >= warn else "bad"

    kpi_cards = "".join([
        _kpi_card(
            "Scenarios Passed",
            "Deployed AND met 100% of benchmark service/zone/dependency requirements",
            f"{n_pass}/{report.total_scenarios}",
            f"{report.scenarios_skipped} dry-run" if report.scenarios_skipped else "",
            _lvl(n_pass / max(n_deployed, 1), 0.8, 0.5),
        ),
        _kpi_card(
            "Deploy Success",
            "Scenarios where the infrastructure actually started running",
            _pct(report.deploy_success_rate),
            f"{n_deploy_ok}/{n_deployed} scenarios",
            _lvl(report.deploy_success_rate, 0.8, 0.5),
        ),
        _kpi_card(
            "Benchmark Coverage (Plan)",
            "Average service coverage across all scenarios at plan level",
            _pct(report.mean_planned_service_coverage),
            "",
            _lvl(report.mean_planned_service_coverage, 0.9, 0.6),
        ),
        _kpi_card(
            "Running Coverage",
            "Average service coverage of actually running containers",
            _pct(report.mean_running_service_coverage),
            "",
            _lvl(report.mean_running_service_coverage, 0.9, 0.6),
        ),
        _kpi_card(
            "Scenario Fit Score",
            "Composite score (0-1) averaging service, zone, and dependency coverage",
            f"{report.mean_scenario_fit_score:.2f}",
            "",
            _lvl(report.mean_scenario_fit_score, 0.8, 0.5),
        ),
    ])

    # --- Scenario detail rows ---
    rows = []
    for r in report.results:
        if r.dry_run:
            rows.append(
                f"<tr>"
                f"<td>{_esc(r.scenario_id)}</td>"
                f"<td><span style='color:{_diff_color(r.difficulty)}'>{_esc(r.difficulty)}</span></td>"
                f"<td colspan='10'>{'VALID' if r.dry_run_valid else 'INVALID: ' + _esc('; '.join(r.errors))}</td>"
                f"</tr>"
            )
            continue
        _fail_cell = _esc(r.failure_stage)
        if r.error_class and r.failure_stage:
            _fail_cell = f"{_esc(r.failure_stage)} <small style='color:var(--muted)'>({_esc(r.error_class)})</small>"
        rows.append(
            f"<tr>"
            f"<td>{_esc(r.scenario_id)}</td>"
            f"<td><span style='color:{_diff_color(r.difficulty)}'>{_esc(r.difficulty)}</span></td>"
            f"<td>{_badge(r.deploy_success)}</td>"
            f"<td>{_badge(r.benchmark_pass)}</td>"
            f"<td>{r.scenario_fit_score:.2f}</td>"
            f"<td>{_pct(r.planned_service_coverage)}</td>"
            f"<td>{_pct(r.planned_zone_coverage)}</td>"
            f"<td>{_dep_pct(r.planned_dep_coverage, r.benchmark_dep_required)}</td>"
            f"<td>{r.placement_violations}</td>"
            f"<td>{r.containers_running}/{r.containers_planned}"
            f"{'  (' + str(r.containers_dropped) + ' dropped)' if r.containers_dropped else ''}</td>"
            f"<td>{_pct(r.running_service_coverage)}</td>"
            f"<td>{r.duration_s:.0f}s</td>"
            f"<td>{_fail_cell}</td>"
            f"</tr>"
        )

    # --- Difficulty breakdown rows ---
    diff_rows = []
    for diff in ("easy", "medium", "hard"):
        d = report.by_difficulty.get(diff, {})
        if not d:
            continue
        diff_rows.append(
            f"<tr>"
            f"<td><span style='color:{_diff_color(diff)};font-weight:bold'>{diff}</span></td>"
            f"<td>{d['count']}</td>"
            f"<td>{d['passed']}/{d['count']}</td>"
            f"<td>{_pct(d['deploy_success_rate'])}</td>"
            f"<td>{d.get('mean_scenario_fit_score', 0):.2f}</td>"
            f"<td>{_pct(d['mean_planned_service_coverage'])}</td>"
            f"<td>{_pct(d['mean_planned_zone_coverage'])}</td>"
            f"<td>{_pct(d['mean_running_service_coverage'])}</td>"
            f"<td>{d['mean_duration_s']:.0f}s</td>"
            f"</tr>"
        )

    css = _common_css()
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Honeynet Benchmark Report</title>
<style>{css}
  tr:hover td {{ background: rgba(56,189,248,0.06); }}
</style>
</head>
<body>
<h1>Honeynet Benchmark Report</h1>
<p class="muted">Evaluates deployment scenarios against formal benchmark references.
Each scenario defines required services, zones, and dependencies that must be present.</p>

{banner}

<div class="section">
  <h2>How did the benchmark perform?</h2>
  <div class="kpi-grid">{kpi_cards}</div>
</div>

<div class="section panel">
  <h2>Results by difficulty</h2>
  <p class="muted">Scenarios are grouped into easy, medium, and hard based on the number of
  required services, zones, and dependencies. Higher difficulty means more requirements to satisfy.</p>
  <table>
    <thead>
    <tr><th>Difficulty</th><th>Scenarios</th><th>Passed</th><th>Deploy Rate</th>
        <th title="Mean composite scenario-fit score">Fit Score</th>
        <th title="Average fraction of required services present in the deployment plan">Svc Cov (Plan)</th>
        <th title="Average fraction of required zones present in the deployment plan">Zone Cov (Plan)</th>
        <th title="Average fraction of required services actually running post-deploy">Svc Cov (Running)</th>
        <th>Avg Duration</th></tr>
    </thead>
    <tbody>{''.join(diff_rows) if diff_rows else "<tr><td colspan='9'>No deployed scenarios.</td></tr>"}</tbody>
  </table>
</div>

{_render_complexity_section(report)}

<div class="section panel">
  <h2>Scenario details</h2>
  <table>
    <thead>
    <tr>
      <th title="Unique scenario identifier from the benchmark corpus">Scenario</th>
      <th title="easy / medium / hard based on requirement complexity">Difficulty</th>
      <th title="Did containers start running?">Deploy</th>
      <th title="All coverage = 100% and zero placement violations">Benchmark</th>
      <th title="Composite scenario-fit score (0-1)">Score</th>
      <th title="Fraction of required services present in the deployment plan">Svc (Plan)</th>
      <th title="Fraction of required network zones present in the deployment plan">Zone (Plan)</th>
      <th title="Fraction of required service-to-service dependencies in the plan">Dep (Plan)</th>
      <th title="Services placed in zones where they are forbidden">Violations</th>
      <th title="Running containers out of planned containers">Containers</th>
      <th title="Fraction of required services actually running after deployment">Svc (Running)</th>
      <th>Duration</th>
      <th title="Pipeline stage where this scenario failed (empty = success)">Failed At</th>
    </tr>
    </thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
</div>

<div class="section panel">
  <h2>What do these metrics mean?</h2>
  <dl class="glossary">
    <dt>Service Coverage</dt>
    <dd>How many services required by the benchmark reference are present in the deployment.
    100% means all required services (e.g., database, web server, cache) were generated by the LLM.</dd>
    <dt>Zone Coverage</dt>
    <dd>How many network zones required by the benchmark are present. Zones define network
    isolation boundaries (e.g., DMZ for public-facing services, internal for databases).</dd>
    <dt>Dependency Coverage</dt>
    <dd>Whether required connections between services exist in the deployment plan.
    For example, a web server must depend on its database so they start in the correct order.
    Shows <code>n/a</code> when the benchmark defines no dependency requirements.</dd>
    <dt>Placement Violations</dt>
    <dd>Services deployed in zones where they are forbidden by the benchmark reference.
    For example, a database exposed in the DMZ instead of the internal zone. Zero is the target.</dd>
    <dt>Benchmark Pass</dt>
    <dd>True only when ALL of the above are satisfied: 100% service coverage, 100% zone coverage,
    100% dependency coverage, and zero placement violations.</dd>
    <dt>Scenario Fit Score</dt>
    <dd>Composite score (0.0\u20131.0) computed as the equal-weight average of service, zone, and
    dependency coverage. Halved when placement violations exist. Useful for ranking partially
    correct results.</dd>
    <dt>Error Class</dt>
    <dd>Coarse classification of the failure cause (e.g., port_conflict, invalid_image, llm_schema_error).
    Shown in parentheses after the failure stage.</dd>
    <dt>Plan vs Running</dt>
    <dd>"Plan" metrics evaluate the deployment model before containers start. "Running" metrics
    evaluate against actually running containers. Running coverage can be lower if containers
    crash on startup.</dd>
  </dl>
</div>

<p class="muted" style="margin-top:2rem;">Generated by Honeynet Framework Benchmark Runner</p>
</body>
</html>"""

    html_path = output_dir / "benchmark_report.html"
    atomic_write_text(html_path, html)
    return html_path
