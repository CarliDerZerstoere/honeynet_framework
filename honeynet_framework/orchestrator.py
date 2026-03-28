"""
Honeynet Orchestrator - Main pipeline for generating and deploying honeynets.

Pipeline:
1. LLM Extraction (single call) → WorldModel
2. Optional catalog snapshot resolution (`deploy.catalog_archetype` → image from JSON) if `catalog_snapshot_path` is set
3. Structural Validation
4. Image Validation + LLM Repair Loop
5. Optional semantic judge (after repair, before compile)
6. Compile → DeployProjection
7. Render + Deploy
"""

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Optional

import yaml

from .llm import LLMConfig, LLMProvider, OllamaProvider, create_llm_provider
from .extraction import WorldModelExtractor
from .validator import validate
from .failure_report import (
    FailureReport,
    FailureSeverity,
    classify_validation_error,
    from_validation_result as classify_validation,
)
from .image_resolver import ImageResolver
from .qa_deception import run_deception_qa, check_suspicious_uniformity
from .deploy_compiler import DeployCompiler, CompilerConfig
from .tofu_renderer import TofuRenderer
from .deployer import DeployerConfig, TerraformDeployer, find_tofu_binary, serialize_stage_results
from .enums_pipeline import FailureStage
from .telemetry_events import append_event
from .semantic_judge import run_semantic_judge
from .models import (
    DeploymentResult,
    DeploymentStatus,
    RuntimeSummary,
    ValidationResult,
    WorldModel,
)
from .qa import QARunner, filter_canary_checks
from .metrics import DeploymentMetrics
from .utils import atomic_write_text
from .catalog import CatalogResolutionError, apply_catalog_resolution, load_catalog_snapshot
from .catalog.startup_probe import probe_startup_profile
from .plugins.registry import discover_catalog_packs, load_repair_strategy_callables, PluginLoadError
from .path_a_diagnostics import collect_path_a_apply_failure_diagnostics
from .scenario_fit_formal import compute_formal_scenario_fit, load_benchmark_reference
from .repair_depends import try_repair_depends_edges
from .repair_incident import RepairIncident, append_repair_incident, excerpt_hash
from .pipeline_artifacts import ArtifactWriter
from .repair_types import FailureContext, RepairProposal, RepairDecision
from .failure_analyzer import enrich_failing_list
from .loop_guard import LoopGuard
from .repair_scorer import score_proposals

logger = logging.getLogger(__name__)


def _remove_systems_and_clean_deps(
    world_model: "WorldModel", to_remove: set[str],
) -> None:
    """Remove systems from the WorldModel and clean up depends_on references."""
    for sys_name in to_remove:
        world_model.systems.pop(sys_name, None)
        logger.info("Removed unrepairable system '%s' from deployment", sys_name)
    for sys in world_model.systems.values():
        if sys.deploy and sys.deploy.depends_on:
            sys.deploy.depends_on = [
                d for d in sys.deploy.depends_on if d not in to_remove
            ]


def _heal_missing_zones(world_model: "WorldModel") -> list[str]:
    """Assign a default internal zone to systems with deploy but no zone.

    Returns list of system names that were healed.
    """
    if not world_model.zones:
        return []

    # Prefer the first internal zone; fall back to any zone
    internal = [
        name for name, z in world_model.zones.items()
        if z.deploy and getattr(z.deploy, "internal", True)
    ]
    fallback = internal[0] if internal else next(iter(world_model.zones))

    healed: list[str] = []
    for sys_name, system in world_model.systems.items():
        if not system.deploy:
            continue
        zone_val = (system.deploy.zone or "").strip()
        if not zone_val or zone_val not in world_model.zones:
            system.deploy.zone = fallback
            healed.append(sys_name)
    return healed


def _try_deterministic_repair(
    probe_failure: dict,
    system: object,
    image_profile: object | None,
) -> bool:
    """Attempt a quick fix from the probe error text before falling back to LLM.

    Returns True if the system was patched (caller should re-probe), False
    if LLM repair is needed.
    """
    import re as _re

    logs = str(probe_failure.get("docker_logs", "") or "")
    if not logs or not system or not getattr(system, "deploy", None):
        return False

    # --- Pattern 1: Missing env var ---
    # Matches: "not set. Did you forget to add -e SLAPD_PASSWORD=..."
    #          "SLAPD_DOMAIN not set"
    #          "X is required"
    env_match = _re.search(
        r'(?:Did you forget to add -e |(?:^|\s))(\w+)(?:\s+(?:not set|is required|must be set))',
        logs,
        _re.IGNORECASE,
    )
    if env_match:
        var_name = env_match.group(1)
        existing_keys = {
            e.split("=", 1)[0] for e in (system.deploy.env or []) if "=" in e
        }
        if var_name not in existing_keys:
            if system.deploy.env is None:
                system.deploy.env = []
            system.deploy.env.append(f"{var_name}=changeme")
            logger.info(
                "Deterministic repair: added missing env %s to '%s'",
                var_name, probe_failure.get("system_name", "?"),
            )
            return True

    # --- Pattern 2: Usage/help text (image needs subcommand) ---
    if ("USAGE:" in logs.upper() or "Usage:" in logs) and not probe_failure.get("running"):
        # Try to infer the correct subcommand from OCI Cmd + usage output.
        # Example: minio's Cmd is ["minio"] but it needs "server /data";
        # the usage text often contains "COMMAND" hints.
        inferred_cmd: list[str] | None = None
        if image_profile and hasattr(image_profile, "cmd") and image_profile.cmd:
            base_cmd = list(image_profile.cmd)
            # Check if the usage output mentions a "server" subcommand — common
            # for services that require a subcommand + path (minio, consul, vault).
            server_match = _re.search(
                r'(?:COMMANDS?|Available commands?).*?\b(server)\b',
                logs, _re.IGNORECASE | _re.DOTALL,
            )
            if server_match:
                inferred_cmd = base_cmd + ["server", "/data"]
            else:
                inferred_cmd = base_cmd
        if inferred_cmd:
            system.deploy.command = inferred_cmd
            logger.info(
                "Deterministic repair: set command=%s for '%s' (from usage output)",
                system.deploy.command, probe_failure.get("system_name", "?"),
            )
            return True

    # --- Pattern 3: File not found / module not found ---
    if _re.search(r"Cannot find module|No such file or directory|ModuleNotFoundError", logs):
        if image_profile and getattr(image_profile, "has_daemon_entrypoint", False):
            system.deploy.command = None
            logger.info(
                "Deterministic repair: set command=null for '%s' (file not found, daemon entrypoint)",
                probe_failure.get("system_name", "?"),
            )
            return True

    return False


def _build_scenario_context(scenario_doc: dict) -> str:
    """Convert a benchmark reference into natural-language context for the LLM.

    The output reads like a human-written paragraph describing what the deployment
    should contain, without structured lists or bullet points.
    """
    ref = scenario_doc.get("reference", {})
    if not ref:
        return ""

    # --- Service phrases ---
    svc_phrases = []
    for svc in ref.get("required_services", []):
        sid = str(svc.get("id", "")).replace("_", " ")
        archetypes = svc.get("archetypes_any", [])
        roles = svc.get("roles_any", [])
        kinds = svc.get("kinds_any", [])

        if archetypes:
            tech = " or ".join(archetypes[:2])
            if "database" in kinds:
                svc_phrases.append(f"a {tech} database")
            elif "queue" in kinds:
                svc_phrases.append(f"a {tech} message broker")
            elif "identity" in kinds:
                svc_phrases.append(f"a {tech} identity service")
            elif "monitor" in kinds or "logging" in kinds:
                svc_phrases.append(f"a {tech} logging or monitoring agent")
            elif "storage" in kinds:
                svc_phrases.append(f"a {tech} storage backend")
            else:
                svc_phrases.append(f"a {tech} service")
        elif roles:
            role_str = " and ".join(roles[:2])
            svc_phrases.append(f"a {sid} for {role_str}")
        elif kinds:
            kind_map = {
                "web": f"a {sid} serving web traffic",
                "database": f"a {sid} for data persistence",
                "queue": f"a {sid} message queue",
                "runtime": f"a {sid} backend service",
                "identity": f"an identity or authentication service",
                "monitor": f"a monitoring service",
                "storage": f"a storage service",
                "infra": f"a {sid} infrastructure component",
            }
            svc_phrases.append(kind_map.get(kinds[0], f"a {sid}"))
        else:
            svc_phrases.append(f"a {sid}")

    # --- Zone phrases (include exact id so LLM uses the right name) ---
    zone_phrases = []
    zone_ids = []
    for zone in ref.get("required_zones", []):
        zid_raw = str(zone.get("id", ""))
        zid = zid_raw.replace("_", " ")
        if zid_raw:
            zone_ids.append(zid_raw)
        internal = zone.get("internal", True)
        exposure = zone.get("exposure_any", [])
        if not internal or any(e in ("internet", "public", "dmz") for e in exposure):
            zone_phrases.append(f"a public-facing zone named '{zid_raw}'")
        else:
            zone_phrases.append(f"an internal zone named '{zid_raw}'")

    # --- Dependency phrases ---
    dep_phrases = []
    svc_id_to_phrase = {}
    for svc in ref.get("required_services", []):
        sid = svc.get("id", "")
        svc_id_to_phrase[sid] = sid.replace("_", " ")
    for dep in ref.get("required_dependencies", []):
        src = svc_id_to_phrase.get(dep.get("source_service_id", ""), dep.get("source_service_id", ""))
        tgt = svc_id_to_phrase.get(dep.get("target_service_id", ""), dep.get("target_service_id", ""))
        dep_phrases.append(f"the {src} needs to depend on the {tgt}")

    # --- Forbidden placement phrases ---
    forbidden_phrases = []
    for fp in ref.get("forbidden_placements", []):
        svc = svc_id_to_phrase.get(fp.get("service_id", ""), fp.get("service_id", ""))
        zone = str(fp.get("zone_id", "")).replace("_", " ")
        forbidden_phrases.append(f"the {svc} must not be placed in the {zone}")

    # --- Assemble natural text ---
    parts = []
    if svc_phrases:
        if len(svc_phrases) == 1:
            parts.append(f"The deployment should include {svc_phrases[0]}.")
        else:
            joined = ", ".join(svc_phrases[:-1]) + f", and {svc_phrases[-1]}"
            parts.append(f"The deployment should include {joined}.")

    if zone_phrases:
        parts.append(
            "Organize the infrastructure into "
            + ", ".join(zone_phrases[:-1])
            + (f", and {zone_phrases[-1]}" if len(zone_phrases) > 1 else zone_phrases[0])
            + "."
        )

    if dep_phrases:
        parts.append(" ".join(d.capitalize() + "." if i == 0 else d + "."
                              for i, d in enumerate(dep_phrases[:4])))

    if forbidden_phrases:
        parts.append("For security, " + ", and ".join(forbidden_phrases[:3]) + ".")

    if zone_ids:
        parts.append(
            f"IMPORTANT: Use exactly these zone names: {', '.join(zone_ids)}. "
            "Do not rename or use synonyms."
        )

    return " ".join(parts)


def _classify_error(failure_stage: str, error_text: str) -> str:
    """Classify a pipeline failure into a coarse error bucket."""
    lower = error_text.lower() if error_text else ""
    stage = failure_stage.lower()

    if stage in ("extraction", "world_model_validation"):
        return "llm_schema_error"
    if stage == "image_resolution":
        return "invalid_image"
    if stage == "semantic_judge":
        return "llm_schema_error"
    if stage in ("fmt", "validate"):
        return "tofu_syntax"
    if stage == "plan":
        if "port" in lower and ("conflict" in lower or "already" in lower or "in use" in lower):
            return "port_conflict"
        return "tofu_syntax"
    if stage == "apply":
        if "port" in lower and ("conflict" in lower or "already" in lower or "in use" in lower or "bind" in lower):
            return "port_conflict"
        if "network" in lower or "subnet" in lower:
            return "network_error"
        if "timeout" in lower or "timed out" in lower:
            return "timeout"
        if "depends_on" in lower or "dependency" in lower:
            return "dependency_missing"
        return "unknown"
    if stage == "runtime_verify":
        if "timeout" in lower or "timed out" in lower:
            return "timeout"
        if "network" in lower:
            return "network_error"
        return "unknown"
    return "unknown"


@dataclass
class OrchestratorConfig:
    """Configuration for the honeynet orchestrator."""
    llm_config: LLMConfig = field(default_factory=LLMConfig)
    work_dir: Path = field(default_factory=lambda: Path("output"))
    use_docker_for_tofu: bool = False
    tofu_container: str = "opentofu"
    deploy_timeout: int = 900
    max_image_repair_attempts: int = 3
    preload_ollama_model: bool = True
    cleanup_before_deploy: bool = True
    enable_telemetry_events: bool = False
    # None = auto: enabled when max_apply_repair_attempts > 0 (repair loop runs).
    # Pass True/False explicitly to override the auto-detect logic.
    enable_semantic_judge: Optional[bool] = None
    judge_fail_on_error: bool = False
    judge_prompt_path: Optional[Path] = None
    catalog_snapshot_path: Optional[Path] = None
    benchmark_reference_path: Optional[Path] = None
    image_soft_fail_is_blocking: bool = False
    command_warnings_are_errors: bool = False  # Promote ONE_SHOT_COMMAND/FICTIONAL_SCRIPT to hard errors
    enable_plugins: bool = False  # Plugins disabled by default (supply-chain safety)
    plugin_allowlist: list[str] = field(default_factory=list)  # Allowed plugin names (empty = allow all when enabled)
    # Artifacts: legacy = work_dir root only; per_run = runs/<run_id>/ snapshots; both = both
    run_artifacts_mode: str = "legacy"
    emit_repair_incidents: bool = False
    benchmark_reference_required: bool = False  # If True, deploy fails when no --benchmark-reference
    # QA: optional frozen canary subset (check id prefix or exact match); empty = run all checks
    qa_canary_check_ids: list[str] = field(default_factory=list)
    # Extra TCP connect attempts for flaky services (total attempts = 1 + this value)
    qa_tcp_connect_extra_attempts: int = 1
    fail_on_fmt_error: bool = False  # If True, abort plan/apply when tofu fmt fails
    max_apply_repair_attempts: int = 3  # Number of apply → diagnose → repair → retry cycles
    enable_partial_apply: bool = True  # Deploy surviving containers when repair loop exhausted
    enable_runtime_repair: bool = True  # Repair containers that crash after successful apply
    min_partial_apply_ratio: float = 0.5  # Minimum fraction of containers for partial apply
    # Multi-phase extraction: split WorldModel generation into Architecture → Config → Enrich
    # phases for better quality on complex scenarios (opt-in, single-call remains default)
    multi_phase_extraction: bool = False

    # --- Pre-deploy realism gates --------------------------------------------
    # Minimum fraction of deception QA checks that must pass before deploying.
    # 0.0 = gate disabled.  0.7 = at least 3 of 4 checks must pass.
    min_deception_score: float = 0.7
    # Minimum combined image+port diversity score (0–1).
    # 0.0 = gate disabled.  0.2 = reject honeynets where all containers look identical.
    min_diversity_score: float = 0.2

    # --- Repair observability (Schritt 2/3) ---------------------------------
    # Classify container failures into typed categories (IMAGE_NOT_FOUND etc.)
    enable_failure_analysis: bool = True
    # Track proposal/apply history per run to detect oscillating repairs
    enable_loop_detection: bool = True
    # Persist a JSONL repair history file alongside other run artifacts
    enable_repair_history: bool = True

    # --- WorldModel mutation guards (Schritt 4) ------------------------------
    # Fix missing/dangling zone references and log bare-secret env vars
    enable_consistency_fixer: bool = False
    # Run structural pre-compile validation; errors block compilation
    enable_precompile_validation: bool = False

    # --- Repair scoring (Schritt 5) ------------------------------------------
    # Score LLM repair proposals; low-score proposals get a warning
    enable_repair_scoring: bool = False
    # Proposals below this score trigger a warning in the log
    repair_score_warn_threshold: float = 0.3
    # Proposals below this score are skipped (not applied) when scoring enabled
    repair_score_apply_threshold: float = 0.5

    # --- HITL review (Schritt 6) ---------------------------------------------
    # Pause for human review when repair proposals fall below hitl_trigger_threshold
    enable_hitl: bool = False
    hitl_trigger_threshold: float = 0.3

    def __post_init__(self) -> None:
        # Auto-enable the semantic judge when the apply repair loop is active.
        # If the user explicitly passed True or False, that value is kept.
        if self.enable_semantic_judge is None:
            self.enable_semantic_judge = self.max_apply_repair_attempts > 0


class HoneynetOrchestrator:
    """Main orchestrator for the honeynet generation pipeline."""

    def __init__(self, config: Optional[OrchestratorConfig] = None):
        self.config = config or OrchestratorConfig()
        self._initialized = False
        self._deploy_lock = asyncio.Lock()
        self.llm: Optional[LLMProvider] = None
        self.extractor: Optional[WorldModelExtractor] = None
        self.compiler: Optional[DeployCompiler] = None
        self.renderer: Optional[TofuRenderer] = None
        self.deployer: Optional[TerraformDeployer] = None
        self.image_resolver: Optional[ImageResolver] = None
        self.artifacts = ArtifactWriter(
            run_artifacts_mode=self.config.run_artifacts_mode,
            emit_repair_incidents=self.config.emit_repair_incidents,
        )

    async def initialize(self) -> None:
        """Initialize all components."""
        if self._initialized:
            return

        work_dir = Path(self.config.work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)

        # LLM
        self.llm = create_llm_provider(self.config.llm_config)

        # Preload Ollama model if configured
        if self.config.preload_ollama_model and isinstance(self.llm, OllamaProvider):
            logger.info("Preloading Ollama model %s...", self.config.llm_config.model)
            try:
                await self.llm.preload_model()
                logger.info("Model preloaded successfully")
            except Exception as e:
                logger.warning("Model preload failed: %s", e)

        # Components
        self.extractor = WorldModelExtractor(
            self.llm, multi_phase=self.config.multi_phase_extraction,
        )
        self.compiler = DeployCompiler(CompilerConfig())
        self.renderer = TofuRenderer(work_dir=work_dir)
        self.image_resolver = ImageResolver(timeout=30)

        # Deployer
        deployer_config = DeployerConfig(
            work_dir=work_dir,
            use_docker=self.config.use_docker_for_tofu,
            docker_container=self.config.tofu_container,
            tofu_binary=find_tofu_binary() if not self.config.use_docker_for_tofu else "",
            timeout=self.config.deploy_timeout,
            fail_on_fmt_error=self.config.fail_on_fmt_error,
        )
        self.deployer = TerraformDeployer(deployer_config)

        self._initialized = True
        logger.info("Orchestrator initialized (work_dir=%s)", work_dir)

    def _telemetry(
        self,
        work_dir: Path,
        run_id: str,
        stage: str,
        outcome: str,
        t0: float,
        error_summary: Optional[str] = None,
    ) -> None:
        if not self.config.enable_telemetry_events:
            return
        try:
            append_event(
                work_dir,
                run_id=run_id,
                stage=stage,
                outcome=outcome,
                duration_ms=(time.monotonic() - t0) * 1000.0,
                error_summary=error_summary,
            )
        except OSError as e:
            logger.warning("Telemetry append failed: %s", e)

    async def deploy(self, user_request: str) -> DeploymentResult:
        """Run the full pipeline: extract → validate → images → [judge] → compile → deploy.

        Rejects concurrent calls immediately rather than queuing them.
        """
        # Non-blocking reject: in a single-threaded asyncio loop, checking
        # and setting a bool without an intervening await is atomic.
        if self._deploy_lock.locked():
            return DeploymentResult(
                status=DeploymentStatus.FAILED,
                failure_stage=FailureStage.UNKNOWN.value,
                errors=["Concurrent deploy() call on same orchestrator instance — aborting"],
            )
        # acquire() here is guaranteed to succeed immediately because we are
        # still in the same event-loop tick (no await between locked() and
        # acquire()).  The lock remains held across the entire deploy.
        await self._deploy_lock.acquire()
        try:
            return await self._deploy_impl(user_request)
        finally:
            self._deploy_lock.release()

    async def _deploy_impl(self, user_request: str) -> DeploymentResult:
        """Internal deploy implementation (guarded by _deploy_lock)."""
        await self.initialize()
        work_dir = Path(self.config.work_dir)

        # Cross-process guard: atomic lock file creation (O_CREAT|O_EXCL avoids TOCTOU)
        lockfile = work_dir / ".honeynet.lock"
        run_id = uuid.uuid4().hex

        try:
            fd = os.open(str(lockfile), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                lock_data = json.dumps({"pid": os.getpid(), "run_id": run_id, "timestamp": time.time()})
                os.write(fd, lock_data.encode("utf-8"))
            finally:
                os.close(fd)
        except FileExistsError:
            # Stale-lock detection: check if the locking PID is still alive.
            stale = False
            try:
                lock_info = json.loads(lockfile.read_text(encoding="utf-8"))
                lock_pid = lock_info.get("pid")
                if lock_pid is not None:
                    try:
                        _pid = int(lock_pid)
                        if os.name == "nt":
                            # On Windows, os.kill(pid, 0) may send SIGTERM instead
                            # of a harmless existence check.  Use OpenProcess instead.
                            import ctypes
                            _PROCESS_QUERY_LIMITED = 0x1000
                            _handle = ctypes.windll.kernel32.OpenProcess(
                                _PROCESS_QUERY_LIMITED, False, _pid,
                            )
                            if _handle:
                                ctypes.windll.kernel32.CloseHandle(_handle)
                                # Process is alive — but PID may have been recycled.
                                # Check if the lock timestamp is older than 2 hours;
                                # no legitimate deploy should run that long.
                                _lock_ts = lock_info.get("timestamp")
                                if _lock_ts is not None:
                                    _lock_age = time.time() - float(_lock_ts)
                                    if _lock_age > 7200:  # 2 hours
                                        raise OSError(
                                            f"lock age {_lock_age:.0f}s exceeds 2h — "
                                            f"likely PID recycling"
                                        )
                                else:
                                    # No timestamp in lock file — cannot verify age.
                                    # The process IS alive, so treat the lock as
                                    # active (conservative).  A recycled PID without
                                    # a timestamp is indistinguishable from a live
                                    # deploy, so we must not steal the lock.
                                    logger.warning(
                                        "Lock held by pid=%s has no timestamp — "
                                        "treating as active (cannot verify age).",
                                        _pid,
                                    )
                            else:
                                raise OSError("process not found")
                        else:
                            os.kill(_pid, 0)  # signal 0 = existence check (POSIX)
                    except (OSError, ValueError):
                        # Process is dead — lock is stale
                        stale = True
                        logger.warning(
                            "Stale lock detected: pid=%s is dead (run_id=%s). "
                            "Removing stale lock and proceeding.",
                            lock_pid, lock_info.get("run_id", "?"),
                        )
                if not stale:
                    logger.warning(
                        "Lock file exists (pid=%s, run_id=%s). Another process may be "
                        "deploying to this work_dir. Remove %s if stale.",
                        lock_info.get("pid", "?"),
                        lock_info.get("run_id", "?"),
                        lockfile,
                    )
            except Exception:
                # Can't read lock → assume stale (better than permanent block)
                stale = True
                logger.warning("Unreadable lock file %s — assuming stale, removing.", lockfile)

            if stale:
                try:
                    lockfile.unlink()
                    # Retry lock creation
                    fd = os.open(str(lockfile), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    try:
                        lock_data = json.dumps({"pid": os.getpid(), "run_id": run_id, "timestamp": time.time()})
                        os.write(fd, lock_data.encode("utf-8"))
                    finally:
                        os.close(fd)
                except (OSError, FileExistsError) as retry_err:
                    return DeploymentResult(
                        status=DeploymentStatus.FAILED,
                        failure_stage=FailureStage.UNKNOWN.value,
                        errors=[f"Could not reclaim stale lock {lockfile}: {retry_err}"],
                    )
            else:
                return DeploymentResult(
                    status=DeploymentStatus.FAILED,
                    failure_stage=FailureStage.UNKNOWN.value,
                    errors=[f"Lock file exists: {lockfile} — another deploy may be running. Remove if stale."],
                )
        except OSError as e:
            # Fail-closed: if we cannot create the lock for any OS reason,
            # do NOT proceed — this prevents silent parallel corruption.
            logger.error("Could not create lock file %s: %s", lockfile, e)
            return DeploymentResult(
                status=DeploymentStatus.FAILED,
                failure_stage=FailureStage.UNKNOWN.value,
                errors=[f"Cannot create lock file {lockfile}: {e}"],
            )

        try:
            return await self._deploy_locked(user_request, work_dir, run_id, lockfile)
        finally:
            # Atomic lock cleanup: rename to a temp name, verify ownership, then
            # delete.  If the rename succeeds, no other process can see the lock
            # (avoiding the read-then-unlink TOCTOU window).
            try:
                tmp_lock = lockfile.with_suffix(f".{run_id[:8]}.releasing")
                try:
                    lockfile.rename(tmp_lock)
                except OSError:
                    # Lock already gone or renamed by someone else — nothing to do
                    tmp_lock = None
                if tmp_lock:
                    # rename succeeded → we own the file, no need for exists()
                    try:
                        lock_content = json.loads(tmp_lock.read_text(encoding="utf-8"))
                        if lock_content.get("run_id") == run_id:
                            tmp_lock.unlink()
                        else:
                            # Not ours — put it back
                            logger.warning(
                                "Lock file owned by run_id=%s (ours=%s) — restoring",
                                lock_content.get("run_id", "?"), run_id,
                            )
                            tmp_lock.rename(lockfile)
                    except (OSError, json.JSONDecodeError, ValueError):
                        # Best effort — leave temp file for manual cleanup
                        pass
            except OSError:
                pass

    async def _deploy_locked(
        self, user_request: str, work_dir: Path, run_id: str, lockfile: Path,
    ) -> DeploymentResult:
        """Deploy body after lock acquisition."""

        # === Prerequisite gate: check docker + tofu BEFORE expensive LLM calls ===
        prereq_ok, prereq_msg = await self.deployer.check_prerequisites()
        if not prereq_ok:
            logger.error("Prerequisite check failed: %s", prereq_msg)
            m_pre = DeploymentMetrics(
                run_id=run_id,
                work_dir=str(work_dir.resolve()),
                tofu_version="",
            )
            m_pre.failure_stage = FailureStage.INIT.value
            m_pre.errors.append(f"Prerequisites not met: {prereq_msg}")
            self._save_metrics(m_pre, work_dir, validation_report=None)
            return DeploymentResult(
                status=DeploymentStatus.FAILED,
                failure_stage=FailureStage.INIT.value,
                run_id=run_id,
                errors=[f"Prerequisites not met: {prereq_msg}"],
                metrics=m_pre.to_dict(),
            )
        logger.info("Prerequisites OK: %s", prereq_msg)

        if self.config.benchmark_reference_required and not self.config.benchmark_reference_path:
            m_br = DeploymentMetrics(
                run_id=run_id,
                work_dir=str(work_dir.resolve()),
                tofu_version="",
            )
            m_br.failure_stage = FailureStage.UNKNOWN.value
            m_br.errors.append(
                "benchmark_reference_required: provide --benchmark-reference (or disable this gate)."
            )
            self._save_metrics(m_br, work_dir, validation_report=None)
            return DeploymentResult(
                status=DeploymentStatus.FAILED,
                failure_stage=FailureStage.UNKNOWN.value,
                run_id=run_id,
                errors=["benchmark_reference_required: provide --benchmark-reference (or disable this gate)."],
                metrics=m_br.to_dict(),
            )

        tofu_ver = ""
        try:
            tofu_ver = await self.deployer.tofu_version_line()
        except Exception as e:
            logger.debug("Could not read tofu version: %s", e)

        last_validation_report: dict | None = None

        _pipeline_started_utc = datetime.now(timezone.utc).isoformat()

        def base_metrics() -> DeploymentMetrics:
            m = DeploymentMetrics(
                run_id=run_id,
                work_dir=str(work_dir.resolve()),
                tofu_version=tofu_ver,
                started_at_utc=_pipeline_started_utc,
            )
            return m

        # === Step 1: Extract World Model ===
        logger.info("Step 1/5: Extracting World Model from prompt...")
        t_extract = time.monotonic()
        scenario_context: str | None = None
        if self.config.benchmark_reference_path and self.config.benchmark_reference_path.exists():
            try:
                _ref_doc = load_benchmark_reference(self.config.benchmark_reference_path)
                scenario_context = _build_scenario_context(_ref_doc) or None
            except Exception:
                logger.debug("Could not build scenario context from benchmark reference")
        try:
            world_model = await self.extractor.extract(user_request, scenario_context=scenario_context)
        except Exception as e:
            logger.error("World model extraction failed: %s", e)
            self._telemetry(
                work_dir, run_id, FailureStage.EXTRACTION.value, "failed", t_extract, str(e)
            )
            metrics = base_metrics()
            metrics.failure_stage = FailureStage.EXTRACTION.value
            metrics.errors.append(f"Extraction failed: {e}")
            self._save_metrics(metrics, work_dir, validation_report=last_validation_report)
            return DeploymentResult(
                status=DeploymentStatus.FAILED,
                failure_stage=FailureStage.EXTRACTION.value,
                run_id=run_id,
                errors=[f"Extraction failed: {e}"],
                metrics=metrics.to_dict(),
            )

        self._telemetry(work_dir, run_id, FailureStage.EXTRACTION.value, "ok", t_extract)
        self._save_world_model(world_model, work_dir)
        sys_count = len(world_model.deployable_systems)
        zone_count = len(world_model.zones)
        logger.info("Extracted %d systems in %d zones", sys_count, zone_count)
        metrics = base_metrics()
        metrics.stage_durations["extraction"] = time.monotonic() - t_extract
        metrics.zone_count = zone_count
        metrics.system_count = sys_count
        metrics.planned_containers = sys_count
        # Record deterministic post-processing corrections
        metrics.deterministic_dep_additions = getattr(self.extractor, "last_dep_additions", [])
        metrics.deterministic_placement_fixes = getattr(self.extractor, "last_placement_fixes", [])

        # === Optional: catalog snapshot (P4) — after extract, before structural validation ===
        if self.config.catalog_snapshot_path:
            t_cat = time.monotonic()
            try:
                snap = load_catalog_snapshot(Path(self.config.catalog_snapshot_path))
                if self.config.enable_plugins:
                    from .plugins.registry import set_plugin_allowlist

                    set_plugin_allowlist(self.config.plugin_allowlist)
                plug_objs = [obj for _, obj in discover_catalog_packs()] if self.config.enable_plugins else []
                world_model, cat_applied = apply_catalog_resolution(
                    world_model, snap, plugins=plug_objs
                )
                self._save_catalog_resolution(work_dir, run_id, cat_applied)
                self._save_world_model(world_model, work_dir)
            except (ValueError, CatalogResolutionError, OSError, PluginLoadError) as e:
                logger.error("Catalog resolution failed: %s", e)
                self._telemetry(
                    work_dir,
                    run_id,
                    FailureStage.CATALOG_SNAPSHOT.value,
                    "failed",
                    t_cat,
                    str(e),
                )
                metrics.failure_stage = FailureStage.CATALOG_SNAPSHOT.value
                metrics.errors.append(str(e))
                self._save_metrics(metrics, work_dir, validation_report=last_validation_report)
                return DeploymentResult(
                    status=DeploymentStatus.FAILED,
                    failure_stage=FailureStage.CATALOG_SNAPSHOT.value,
                    run_id=run_id,
                    world_model=world_model,
                    errors=[str(e)],
                    metrics=metrics.to_dict(),
                )
            self._telemetry(work_dir, run_id, FailureStage.CATALOG_SNAPSHOT.value, "ok", t_cat)
            metrics.stage_durations["catalog_snapshot"] = time.monotonic() - t_cat

        # === Step 2: Structural Validation ===
        logger.info("Step 2/5: Validating World Model structure...")
        t_val = time.monotonic()

        # --- Zone healer: assign missing zones before validation ---
        # The LLM (especially companion systems) sometimes omits zone.
        # Rather than failing validation, assign to first internal zone.
        _healed = _heal_missing_zones(world_model)
        if _healed:
            logger.info("Zone healer: assigned zones to %d system(s): %s", len(_healed), _healed)
            self._save_world_model(world_model, work_dir)

        world_model, dep_repaired = try_repair_depends_edges(world_model)
        if dep_repaired:
            self._save_world_model(world_model, work_dir)
            if self.config.emit_repair_incidents:
                try:
                    rid_path = work_dir / "runs" / run_id
                    rid_path.mkdir(parents=True, exist_ok=True)
                    append_repair_incident(
                        rid_path / "repair_attempts.jsonl",
                        RepairIncident(
                            run_id=run_id,
                            phase="depends_on",
                            rule_or_kind="deterministic_repair",
                            outcome="ok",
                            extra={"kind": "alias_or_self_edge"},
                        ),
                    )
                    metrics.repair_incident_count += 1
                    metrics.repair_actions.append({
                        "stage": "depends_on", "action": "deterministic_repair", "target": "alias_or_self_edge",
                    })
                except OSError as e:
                    logger.debug("Repair incident ledger skipped: %s", e)
        validation = validate(world_model)
        metrics.world_model_valid = validation.passed
        metrics.validation_first_pass_success = validation.passed
        metrics.validation_repair_attempted = False
        metrics.validation_repair_success = False
        # Save classified validation report
        failure_report = classify_validation(validation)
        last_validation_report = self._save_validation_report(failure_report.to_dict(), work_dir, run_id)
        if not validation.passed:
            # Try registered repair strategies before giving up.
            # Built-in command-policy repair runs FIRST (always available).
            repaired = False
            strategies: list = [self._builtin_command_repair]
            if self.config.enable_plugins:
                # Apply allowlist before loading plugins
                from .plugins.registry import set_plugin_allowlist
                set_plugin_allowlist(self.config.plugin_allowlist)
                try:
                    strategies.extend(load_repair_strategy_callables())
                except PluginLoadError as e:
                    logger.warning("Failed to load repair strategies: %s", e)
            else:
                logger.debug("Plugins disabled — only built-in repair strategies active")
            for strategy in strategies:
                try:
                    metrics.validation_repair_attempted = True
                    logger.info("Attempting repair strategy: %s", getattr(strategy, "__name__", strategy))
                    world_model = strategy(world_model, failure_report)
                    re_validation = validate(world_model)
                    if re_validation.passed:
                        logger.info("Repair strategy succeeded — validation now passes")
                        validation = re_validation
                        metrics.world_model_valid = True
                        metrics.validation_repair_success = True
                        failure_report = classify_validation(re_validation)
                        last_validation_report = self._save_validation_report(
                            failure_report.to_dict(), work_dir, run_id,
                        )
                        repaired = True
                        break
                    logger.info("Repair strategy applied but validation still fails")
                except Exception as e:
                    logger.warning("Repair strategy raised: %s", e)

            if not repaired:
                error_msgs = [f"{e.rule}: {e.details}" for e in validation.errors]
                logger.error("Validation failed with %d errors", len(error_msgs))
                for msg in error_msgs:
                    logger.error("  %s", msg)
                metrics.errors.extend(error_msgs)
                metrics.failure_stage = FailureStage.WORLD_MODEL_VALIDATION.value
                self._telemetry(
                    work_dir,
                    run_id,
                    FailureStage.WORLD_MODEL_VALIDATION.value,
                    "failed",
                    t_val,
                    error_msgs[0] if error_msgs else None,
                )
                self._save_metrics(metrics, work_dir, validation_report=last_validation_report)
                return DeploymentResult(
                    status=DeploymentStatus.FAILED,
                    failure_stage=FailureStage.WORLD_MODEL_VALIDATION.value,
                    run_id=run_id,
                    world_model=world_model,
                    errors=error_msgs,
                    metrics=metrics.to_dict(),
                )
        self._telemetry(work_dir, run_id, FailureStage.WORLD_MODEL_VALIDATION.value, "ok", t_val)
        metrics.stage_durations["validation"] = time.monotonic() - t_val

        # Optionally promote command-related warnings to hard errors
        warn_rules = self._load_command_warning_rules(work_dir)
        if self.config.command_warnings_are_errors and validation.warnings:
            promoted = [w for w in validation.warnings if w.rule in warn_rules]
            if promoted:
                remaining_warnings = [w for w in validation.warnings if w.rule not in warn_rules]
                error_msgs = [f"{w.rule}: {w.details}" for w in promoted]
                logger.error(
                    "Promoted %d command warning(s) to errors (command_warnings_are_errors=True):",
                    len(promoted),
                )
                for msg in error_msgs:
                    logger.error("  %s", msg)
                metrics.errors.extend(error_msgs)
                metrics.failure_stage = FailureStage.WORLD_MODEL_VALIDATION.value
                promoted_errors = [
                    replace(classify_validation_error(w), severity=FailureSeverity.ERROR)
                    for w in promoted
                ]
                warn_kept = [classify_validation_error(w) for w in remaining_warnings]
                last_validation_report = self._save_validation_report(
                    FailureReport(
                        passed=False,
                        total_errors=len(promoted_errors),
                        total_warnings=len(warn_kept),
                        errors=promoted_errors,
                        warnings=warn_kept,
                    ).to_dict(),
                    work_dir,
                    run_id,
                )
                self._telemetry(
                    work_dir,
                    run_id,
                    FailureStage.WORLD_MODEL_VALIDATION.value,
                    "failed",
                    t_val,
                    error_msgs[0],
                )
                self._save_metrics(metrics, work_dir, validation_report=last_validation_report)
                return DeploymentResult(
                    status=DeploymentStatus.FAILED,
                    failure_stage=FailureStage.WORLD_MODEL_VALIDATION.value,
                    run_id=run_id,
                    world_model=world_model,
                    errors=error_msgs,
                    metrics=metrics.to_dict(),
                )

        # Surface validation warnings (non-blocking but important for debugging)
        if validation.warnings:
            logger.warning(
                "Validation passed with %d warning(s):", len(validation.warnings)
            )
            for w in validation.warnings:
                logger.warning("  [%s] %s", w.rule, w.details)
                if w.fix_hint:
                    logger.warning("    Hint: %s", w.fix_hint)

        # Prompt-derived scenario-fit (always runs — no benchmark file needed)
        try:
            from .prompt_fit import compute_prompt_fit
            prompt_fit = compute_prompt_fit(user_request, world_model)
            metrics.planned_service_coverage = prompt_fit.service_recall
            metrics.planned_zone_coverage = prompt_fit.zone_recall
            if prompt_fit.unmatched or prompt_fit.zone_unmatched:
                logger.warning(
                    "Prompt-fit: services %d/%d (%.0f%%), zones %d/%d (%.0f%%). "
                    "Missing services: %s. Missing zones: %s",
                    len(prompt_fit.matched), len(prompt_fit.requirements),
                    prompt_fit.service_recall * 100,
                    len(prompt_fit.zone_matched), len(prompt_fit.zone_requirements),
                    prompt_fit.zone_recall * 100,
                    prompt_fit.unmatched or "none",
                    prompt_fit.zone_unmatched or "none",
                )
            else:
                logger.info(
                    "Prompt-fit: services %d/%d (100%%), zones %d/%d (100%%)",
                    len(prompt_fit.matched), len(prompt_fit.requirements),
                    len(prompt_fit.zone_matched), len(prompt_fit.zone_requirements),
                )
            metrics.benchmark_status = "measured"
            metrics.benchmark_pass = (
                prompt_fit.service_recall >= 0.7
                and prompt_fit.zone_recall >= 0.5
            )
        except Exception as e:
            logger.debug("Prompt-fit evaluation failed: %s", e)

        # Optional formal scenario-fit benchmark evaluation (reference YAML)
        if self.config.benchmark_reference_path:
            try:
                scenario_doc = load_benchmark_reference(self.config.benchmark_reference_path)
                formal = compute_formal_scenario_fit(world_model, scenario_doc)
                metrics.planned_service_coverage = float(formal.get("planned_service_coverage", 0.0))
                metrics.planned_zone_coverage = float(formal.get("planned_zone_coverage", 0.0))
                metrics.planned_dep_coverage = float(
                    formal.get("planned_dep_coverage", 0.0)
                )
                _dep_details = formal.get("details", {}).get("dependency_matches", {})
                metrics.benchmark_dep_required = int(_dep_details.get("total", 0))
                metrics.placement_violations = int(formal.get("placement_violations", 0))
                metrics.benchmark_id = str(formal.get("benchmark_id", ""))
                metrics.benchmark_ref = str(self.config.benchmark_reference_path)
                if formal.get("vacuous_reference"):
                    metrics.benchmark_pass = False
                    metrics.benchmark_status = "empty_reference"
                else:
                    metrics.benchmark_pass = bool(formal.get("benchmark_pass", False))
                    metrics.benchmark_status = "measured"
                metrics.scenario_fit_score = float(formal.get("scenario_fit_score", 0.0))
                self._save_scenario_fit_report(formal, work_dir)
            except Exception as e:
                logger.warning("Formal scenario-fit evaluation failed: %s", e)
                metrics.errors.append(f"formal_scenario_fit_error: {e}")
                metrics.benchmark_status = "error"
                metrics.benchmark_pass = False

        # === Step 3: Image Validation + Repair Loop ===
        _current_stage = FailureStage.IMAGE_RESOLUTION.value
        logger.info("Step 3/5: Validating Docker images...")
        t_img = time.monotonic()
        world_model, image_errors, img_total, img_first_pass = await self._image_repair_loop(world_model)

        # Compute actual image validation pass rate (first-pass, before repair)
        metrics.image_check_rate = (
            img_first_pass / img_total if img_total > 0 else 0.0
        )

        if image_errors:
            metrics.errors.extend(image_errors)
            metrics.failure_stage = FailureStage.IMAGE_RESOLUTION.value
            self._telemetry(
                work_dir,
                run_id,
                FailureStage.IMAGE_RESOLUTION.value,
                "failed",
                t_img,
                image_errors[0] if image_errors else None,
            )
            self._save_metrics(metrics, work_dir, validation_report=last_validation_report)
            return DeploymentResult(
                status=DeploymentStatus.FAILED,
                failure_stage=FailureStage.IMAGE_RESOLUTION.value,
                run_id=run_id,
                world_model=world_model,
                errors=image_errors,
                metrics=metrics.to_dict(),
            )
        self._telemetry(work_dir, run_id, FailureStage.IMAGE_RESOLUTION.value, "ok", t_img)
        metrics.stage_durations["image_resolution"] = time.monotonic() - t_img

        # === Optional: Semantic Judge (after repair — plan I.3) ===
        _current_stage = FailureStage.SEMANTIC_JUDGE.value
        if self.config.enable_semantic_judge and self.llm:
            logger.info("Semantic judge (post image repair)...")
            t_j = time.monotonic()
            try:
                judge_path = self.config.judge_prompt_path
                jres = await run_semantic_judge(
                    user_request=user_request,
                    world_model=world_model,
                    llm=self.llm,
                    prompt_path=judge_path,
                    model_label=self.config.llm_config.model,
                )
                judge_path_out = work_dir / "semantic_judge.json"
                atomic_write_text(judge_path_out, json.dumps(
                    {
                        **jres.to_dict(),
                        "judge_phase": "post_image_repair",
                    },
                    indent=2,
                    ensure_ascii=False,
                ))
                if not jres.passed and self.config.judge_fail_on_error:
                    metrics.failure_stage = FailureStage.SEMANTIC_JUDGE.value
                    metrics.errors.append(f"Semantic judge: {jres.summary}")
                    self._telemetry(
                        work_dir,
                        run_id,
                        FailureStage.SEMANTIC_JUDGE.value,
                        "failed",
                        t_j,
                        jres.summary,
                    )
                    self._save_metrics(metrics, work_dir, validation_report=last_validation_report)
                    return DeploymentResult(
                        status=DeploymentStatus.FAILED,
                        failure_stage=FailureStage.SEMANTIC_JUDGE.value,
                        run_id=run_id,
                        world_model=world_model,
                        errors=[f"Semantic judge: {jres.summary}"],
                        metrics=metrics.to_dict(),
                    )
                judge_outcome = "ok" if jres.passed else "warn"
                self._telemetry(
                    work_dir,
                    run_id,
                    FailureStage.SEMANTIC_JUDGE.value,
                    judge_outcome,
                    t_j,
                    None if jres.passed else jres.summary,
                )
            except Exception as e:
                logger.error("Semantic judge failed: %s", e)
                self._telemetry(
                    work_dir, run_id, FailureStage.SEMANTIC_JUDGE.value,
                    "error", t_j, str(e),
                )
                if self.config.judge_fail_on_error:
                    metrics.failure_stage = FailureStage.SEMANTIC_JUDGE.value
                    metrics.errors.append(f"Semantic judge error: {e}")
                    self._save_metrics(metrics, work_dir, validation_report=last_validation_report)
                    return DeploymentResult(
                        status=DeploymentStatus.FAILED,
                        failure_stage=FailureStage.SEMANTIC_JUDGE.value,
                        run_id=run_id,
                        world_model=world_model,
                        errors=[f"Semantic judge error: {e}"],
                        metrics=metrics.to_dict(),
                    )
                # Non-fatal: log and continue, but record in metrics for observability
                logger.warning("Semantic judge error (non-fatal, continuing): %s", e)
                metrics.errors.append(f"Semantic judge skipped (non-fatal): {e}")

        projection = None
        tofu_json = ""
        # _current_stage tracks which phase we're in so that unexpected
        # exceptions in the outer try/except get the correct failure_stage.
        # It MUST be updated immediately before each substep.
        _current_stage = FailureStage.DEPLOY_COMPILATION.value

        try:
            # === Step 4: Compile to DeployProjection ===
            _current_stage = FailureStage.DEPLOY_COMPILATION.value
            logger.info("Step 4/5: Compiling deployment projection...")
            t_comp = time.monotonic()
            # Seed compiler with host ports already in use to avoid collisions
            try:
                occupied = await self.deployer.get_used_ports()
                self.compiler.reset_seed()
                if occupied:
                    self.compiler.seed_used_ports(occupied)
                    logger.info("Seeded %d occupied host ports before compile", len(occupied))
            except Exception as e:
                logger.debug("Could not query host ports: %s", e)
            # --- Schritt 4: WorldModel mutation guards (before compile) ------
            # Validate BEFORE mutating to avoid corrupted artifacts on disk
            # when validation fails (Finding 2).
            if self.config.enable_precompile_validation:
                from .precompile_validator import validate as _precompile_validate
                from .repair_types import IssueSeverity
                issues = _precompile_validate(world_model)
                errors = [i for i in issues if i.severity == IssueSeverity.ERROR]
                if errors:
                    msgs = "; ".join(i.message for i in errors)
                    raise ValueError(f"Pre-compile validation failed: {msgs}")

            # Mutate only after validation passed
            if self.config.enable_consistency_fixer:
                from .consistency_fixer import fix_zone_consistency, log_secret_issues
                zone_fixes = fix_zone_consistency(world_model)
                if zone_fixes:
                    logger.info("ConsistencyFixer applied %d zone fix(es)", len(zone_fixes))
                log_secret_issues(world_model)
            elif self.config.enable_precompile_validation:
                from .consistency_fixer import log_secret_issues
                log_secret_issues(world_model)

            projection = self.compiler.compile(world_model)
            logger.info(
                "Compiled: %d containers, %d networks",
                len(projection.containers),
                len(projection.networks),
            )

            # resolve_all mutates containers in-place (sets resolved_digest_ref)
            _resolution_report = await self.image_resolver.resolve_all(projection, set_digest_on_containers=True)
            self._telemetry(work_dir, run_id, "compile_projection", "ok", t_comp)
            metrics.stage_durations["compilation"] = time.monotonic() - t_comp

            # === Pre-deploy realism gates (D + E) ===
            # Run deception QA on the compiled projection before spending time on
            # render + deploy.  Both gates default to 0.0 (disabled) for
            # backward compatibility — set them in OrchestratorConfig to enable.
            if self.config.min_deception_score > 0.0 or self.config.min_diversity_score > 0.0:
                gate_errors: list[str] = []
                try:
                    _dqa = run_deception_qa(world_model, projection)

                    if self.config.min_deception_score > 0.0 and _dqa.pass_rate < self.config.min_deception_score:
                        gate_errors.append(
                            f"Deception QA gate failed: pass_rate {_dqa.pass_rate:.0%} "
                            f"< required {self.config.min_deception_score:.0%} "
                            f"({_dqa.checks_passed}/{_dqa.checks_run} checks passed)"
                        )

                    if self.config.min_diversity_score > 0.0:
                        _unif = check_suspicious_uniformity(projection)
                        diversity = (_unif.details or {}).get("combined_diversity", 1.0)
                        if diversity < self.config.min_diversity_score:
                            gate_errors.append(
                                f"Diversity gate failed: score {diversity:.2f} "
                                f"< required {self.config.min_diversity_score:.2f} "
                                f"(all containers look too uniform)"
                            )
                except Exception as _gate_exc:
                    logger.warning(
                        "Pre-deploy realism gate raised an exception — skipping gates: %s",
                        _gate_exc,
                    )
                    gate_errors = []

                if gate_errors:
                    for msg in gate_errors:
                        logger.warning("Pre-deploy gate: %s", msg)
                    metrics.errors.extend(gate_errors)
                    metrics.failure_stage = FailureStage.DEPLOY_COMPILATION.value
                    self._save_metrics(metrics, work_dir, validation_report=last_validation_report)
                    return DeploymentResult(
                        status=DeploymentStatus.FAILED,
                        failure_stage=FailureStage.DEPLOY_COMPILATION.value,
                        run_id=run_id,
                        world_model=world_model,
                        deploy_projection=projection,
                        errors=gate_errors,
                        metrics=metrics.to_dict(),
                    )

            # === Pre-deploy startup probe gate ===
            _current_stage = FailureStage.STARTUP_PROBE.value
            # Quick-test each container with its env/command BEFORE the expensive
            # tofu apply cycle.  Catches missing env vars, wrong commands, and
            # images that exit immediately — then asks the LLM to fix them.
            # If repair fails, remove the system rather than entering the
            # expensive apply→destroy→repair loop.
            probe_failures = await self._probe_containers_before_deploy(
                world_model, projection,
            )
            if probe_failures:
                logger.warning(
                    "Pre-deploy probe: %d container(s) failed startup — attempting repair",
                    len(probe_failures),
                )
                # Try deterministic repair first (fast, no LLM call)
                from .image_introspector import get_image_profile as _get_profile
                remaining_failures = []
                for pf in probe_failures:
                    sys_name = pf.get("system_name", "")
                    system = world_model.systems.get(sys_name)
                    try:
                        _profile = await _get_profile(pf.get("image", ""))
                    except Exception:
                        _profile = None
                    if _try_deterministic_repair(pf, system, _profile):
                        metrics.repair_actions.append({
                            "stage": "pre_deploy_probe",
                            "action": "deterministic_repair",
                            "target": sys_name,
                        })
                    else:
                        remaining_failures.append(pf)

                import copy as _copy_probe
                _probe_world_model_snapshot = _copy_probe.deepcopy(world_model)
                try:
                    # Only send remaining failures to LLM repair
                    if remaining_failures:
                        world_model = await self.extractor.repair_containers(
                            world_model, remaining_failures,
                        )
                    self._save_world_model(world_model, work_dir)
                    # Recompile and re-probe to verify the fix
                    projection = self.compiler.compile(world_model)
                    _resolution_report = await self.image_resolver.resolve_all(
                        projection, set_digest_on_containers=True,
                    )
                    still_failing = await self._probe_containers_before_deploy(
                        world_model, projection,
                    )
                    if still_failing:
                        # LLM repair didn't fix these — remove them
                        still_failing_names = {f["system_name"] for f in still_failing}
                        logger.warning(
                            "Pre-deploy probe: %d container(s) still failing after repair — "
                            "removing from deployment: %s",
                            len(still_failing_names), still_failing_names,
                        )
                        _remove_systems_and_clean_deps(world_model, still_failing_names)
                        self._save_world_model(world_model, work_dir)
                        projection = self.compiler.compile(world_model)
                        _resolution_report = await self.image_resolver.resolve_all(
                            projection, set_digest_on_containers=True,
                        )
                except Exception as _probe_repair_err:
                    logger.warning("Pre-deploy probe repair failed: %s", _probe_repair_err)
                    world_model = _probe_world_model_snapshot
                    projection = self.compiler.compile(world_model)

            # === Step 5: Render + Deploy ===
            # Cleanup BEFORE rendering so that `tofu destroy` operates on the
            # old config (main.tofu.json) that matches the current state file.
            _current_stage = FailureStage.CLEANUP.value
            if self.config.cleanup_before_deploy:
                await self._cleanup_old_state(work_dir)

            _current_stage = FailureStage.RENDER.value
            logger.info("Step 5/5: Rendering OpenTofu and deploying...")
            t_render = time.monotonic()
            tofu_path = work_dir / "main.tofu.json"
            tofu_json = self.renderer.render(projection, output_path=tofu_path)
            logger.info("Wrote %s", tofu_path)
            self._telemetry(work_dir, run_id, "render", "ok", t_render)
            metrics.stage_durations["render"] = time.monotonic() - t_render

            self._save_world_model(world_model, work_dir)

            _current_stage = FailureStage.INIT.value
            metrics.expected_containers = len(projection.containers)
            metrics.dropped_containers = max(
                0, metrics.planned_containers - metrics.expected_containers,
            )

            total_deps = 0
            satisfied_deps = 0
            for sys in world_model.systems.values():
                if sys.deploy and sys.deploy.depends_on:
                    for dep in sys.deploy.depends_on:
                        total_deps += 1
                        if dep in world_model.systems:
                            satisfied_deps += 1
            metrics.model_dep_rate = satisfied_deps / total_deps if total_deps > 0 else 1.0

            _current_stage = FailureStage.PLAN.value
            t_deploy = time.monotonic()
            plan_result, apply_result = await self.deployer.plan_and_apply()
            stage_results = self.deployer.last_stage_results
            self._save_deployer_stages_snapshot(work_dir, run_id, stage_results)
            fmt_stage = stage_results.get("fmt")
            init_stage = stage_results.get("init")
            validate_stage = stage_results.get("validate")
            plan_stage = stage_results.get("plan")
            apply_stage = stage_results.get("apply")

            if fmt_stage and not fmt_stage.success and self.config.fail_on_fmt_error:
                error = f"tofu fmt failed: {fmt_stage.stderr}"
                metrics.errors.append(error)
                metrics.failure_stage = FailureStage.FMT.value
                self._save_metrics(metrics, work_dir, validation_report=last_validation_report)
                return DeploymentResult(
                    status=DeploymentStatus.FAILED,
                    failure_stage=FailureStage.FMT.value,
                    run_id=run_id,
                    world_model=world_model,
                    deploy_projection=projection,
                    terraform_code=tofu_json,
                    errors=[error],
                    metrics=metrics.to_dict(),
                )

            metrics.config_validation_pass = bool(validate_stage and validate_stage.success)
            metrics.plan_success = bool(plan_stage and plan_stage.success)
            metrics.deploy_success = bool(apply_stage and apply_stage.success)
            if metrics.deploy_success:
                metrics.deploy_completed = True
            metrics.deploy_first_attempt = bool(apply_stage and apply_stage.success)
            metrics.deploy_retried = False
            metrics.deploy_retry_count = 0

            if init_stage and not init_stage.success:
                error = f"tofu init failed: {init_stage.stderr}"
                metrics.errors.append(error)
                metrics.failure_stage = FailureStage.INIT.value
                self._save_metrics(metrics, work_dir, validation_report=last_validation_report)
                return DeploymentResult(
                    status=DeploymentStatus.FAILED,
                    failure_stage=FailureStage.INIT.value,
                    run_id=run_id,
                    world_model=world_model,
                    deploy_projection=projection,
                    terraform_code=tofu_json,
                    errors=[error],
                    metrics=metrics.to_dict(),
                )

            if validate_stage and not validate_stage.success:
                error = f"tofu validate failed: {validate_stage.stderr}"
                metrics.errors.append(error)
                metrics.failure_stage = FailureStage.VALIDATE.value
                self._save_metrics(metrics, work_dir, validation_report=last_validation_report)
                return DeploymentResult(
                    status=DeploymentStatus.FAILED,
                    failure_stage=FailureStage.VALIDATE.value,
                    run_id=run_id,
                    world_model=world_model,
                    deploy_projection=projection,
                    terraform_code=tofu_json,
                    errors=[error],
                    metrics=metrics.to_dict(),
                )

            if plan_stage and not plan_stage.success:
                error = f"tofu plan failed: {plan_stage.stderr}"
                metrics.errors.append(error)
                metrics.failure_stage = FailureStage.PLAN.value
                if self._emit_repair_incident_stage_failure(
                    work_dir, run_id, "plan", plan_stage.stderr or ""
                ):
                    metrics.repair_incident_count += 1
                    metrics.repair_actions.append({
                        "stage": "plan", "action": "stage_failure", "target": (plan_stage.stderr or "")[:120],
                    })
                self._save_metrics(metrics, work_dir, validation_report=last_validation_report)
                return DeploymentResult(
                    status=DeploymentStatus.FAILED,
                    failure_stage=FailureStage.PLAN.value,
                    run_id=run_id,
                    world_model=world_model,
                    deploy_projection=projection,
                    terraform_code=tofu_json,
                    errors=[error],
                    metrics=metrics.to_dict(),
                )

            if apply_stage and not apply_stage.success:
                # Recovery path 1: "already exists" → import recovery (single retry)
                stderr_lower = (apply_stage.stderr or "").lower()
                already_exists = "already exists" in stderr_lower
                if already_exists:
                    metrics.deploy_retried = True
                    logger.warning(
                        "Apply failed with 'already exists' — attempting import recovery..."
                    )
                    import_ok = await self._import_existing_docker_resources(
                        apply_stage.stderr, projection
                    )
                    if import_ok:
                        logger.info("Import succeeded — retrying plan+apply...")
                        metrics.deploy_retry_count = 1
                        retry_plan = await self.deployer.plan()
                        if retry_plan.success:
                            retry_apply = await self.deployer.apply()
                            if retry_apply.success:
                                logger.info("Retry apply succeeded after import recovery")
                                apply_result = retry_apply
                                apply_stage = retry_apply
                                metrics.deploy_success = True
                                metrics.deploy_completed = True

                # Recovery path 2: container crash → apply repair loop
                if apply_stage and not apply_stage.success:
                    container_crash = (
                        "container exited immediately" in stderr_lower
                        or "container failed to be in running state" in stderr_lower
                    )
                    if container_crash and self.config.max_apply_repair_attempts > 0:
                        logger.warning("Apply failed with container crashes — entering repair loop...")
                        # Destroy stale state first
                        try:
                            await self.deployer.destroy()
                        except Exception as e:
                            logger.warning("Pre-repair destroy failed: %s", e)
                        world_model, projection, repair_apply, repair_errors = await self._apply_repair_loop(
                            world_model, projection, work_dir, metrics, run_id,
                        )
                        if repair_apply and repair_apply.success:
                            apply_result = repair_apply
                            apply_stage = repair_apply
                            metrics.deploy_success = True
                            metrics.deploy_completed = True
                        elif repair_errors:
                            # Repair loop exhausted
                            apply_stage = repair_apply  # Update to latest attempt

                # Recovery path 3: Partial Apply — deploy surviving containers
                if (
                    apply_stage
                    and not apply_stage.success
                    and self.config.enable_partial_apply
                    and projection
                ):
                    from .deploy_compiler import _safe_name as _sanitize
                    failing_set = {
                        _sanitize(c["system_name"])
                        for c in self._parse_failing_containers(
                            apply_stage.stderr or "", world_model,
                            container_prefix=self.compiler.config.container_prefix,
                        )
                    }
                    _prefix = self.compiler.config.container_prefix
                    good_containers = [
                        c for c in projection.containers
                        if _sanitize(c.name.removeprefix(_prefix)) not in failing_set
                    ]
                    # Only partial-apply if >50% of containers would survive
                    if good_containers and len(good_containers) / len(projection.containers) >= self.config.min_partial_apply_ratio:
                        logger.warning(
                            "Attempting partial apply: %d/%d containers (excluding %s)",
                            len(good_containers), len(projection.containers),
                            list(failing_set),
                        )
                        try:
                            await self.deployer.destroy()
                        except Exception as e:
                            logger.warning("Pre-partial-apply destroy failed: %s", e)
                        targets: list[str] = []
                        for c in good_containers:
                            safe = self.renderer._safe_resource_name(c.name)
                            targets.append(f"docker_container.{safe}")
                            targets.append(f"docker_image.{safe}")
                        for net in projection.networks:
                            safe = self.renderer._safe_resource_name(net.name)
                            targets.append(f"docker_network.{safe}")
                        try:
                            # Re-init required after destroy
                            await self.deployer._run_tofu(["init"])
                            partial_result = await self.deployer.apply_targets(targets)
                            if partial_result.success:
                                logger.info(
                                    "Partial apply succeeded: %d/%d containers deployed",
                                    len(good_containers), len(projection.containers),
                                )
                                apply_result = partial_result
                                apply_stage = partial_result
                                # Partial apply: tofu exited 0, but only a subset of containers
                                # was targeted — do NOT set apply_success (that means full apply).
                                metrics.deploy_completed = True
                                metrics.partial_deploy = True
                        except Exception as pe:
                            logger.warning("Partial apply failed: %s", pe)

                # Final failure: collect diagnostics and return
                if apply_stage and not apply_stage.success:
                    error = f"tofu apply failed: {apply_stage.stderr}"
                    metrics.errors.append(error)
                    metrics.failure_stage = FailureStage.APPLY.value
                    try:
                        diag = await collect_path_a_apply_failure_diagnostics(
                            deployer=self.deployer,
                            projection=projection,
                            work_dir=work_dir,
                            apply_stderr=apply_stage.stderr or "",
                            run_id=run_id,
                        )
                        metrics.path_a_diagnostics_collected = bool(diag.get("collected"))
                        artifact_path = diag.get("artifact_path")
                        metrics.path_a_diagnostics_artifact = str(artifact_path) if artifact_path else ""
                        metrics.path_a_failing_resources_count = int(diag.get("failing_resources_count", 0))
                        metrics.path_a_docker_targets_count = int(diag.get("docker_targets_count", 0))
                        metrics.path_a_error_class = str(diag.get("error_class", "") or "")
                    except Exception as diag_err:
                        logger.warning(
                            "Path A apply-failure diagnostics collection failed: %s",
                            diag_err,
                        )
                    if self._emit_repair_incident_stage_failure(
                        work_dir, run_id, "apply", apply_stage.stderr or ""
                    ):
                        metrics.repair_incident_count += 1
                        metrics.repair_actions.append({
                            "stage": "apply", "action": "stage_failure", "target": (apply_stage.stderr or "")[:120],
                        })
                    self._save_metrics(metrics, work_dir, validation_report=last_validation_report)
                    return DeploymentResult(
                        status=DeploymentStatus.FAILED,
                        failure_stage=FailureStage.APPLY.value,
                        run_id=run_id,
                        world_model=world_model,
                        deploy_projection=projection,
                        terraform_code=tofu_json,
                        errors=[error],
                        metrics=metrics.to_dict(),
                    )

            if not (plan_result and apply_result and apply_result.success):
                error = "Deployment aborted before apply completed"
                metrics.errors.append(error)
                metrics.failure_stage = FailureStage.APPLY.value
                if self._emit_repair_incident_stage_failure(work_dir, run_id, "apply", ""):
                    metrics.repair_incident_count += 1
                    metrics.repair_actions.append({
                        "stage": "apply", "action": "aborted", "target": "",
                    })
                self._save_metrics(metrics, work_dir, validation_report=last_validation_report)
                return DeploymentResult(
                    status=DeploymentStatus.FAILED,
                    failure_stage=FailureStage.APPLY.value,
                    run_id=run_id,
                    world_model=world_model,
                    deploy_projection=projection,
                    terraform_code=tofu_json,
                    errors=[error],
                    metrics=metrics.to_dict(),
                )

            metrics.stage_durations["deploy"] = time.monotonic() - t_deploy

            expected = [c.name for c in projection.containers]
            t_runtime = time.monotonic()
            verify = await self.deployer.verify_deployment(expected)
            running = verify.get("running", {})
            missing = verify.get("missing", [])
            unhealthy = verify.get("unhealthy", [])
            failed_runtime = list(dict.fromkeys([*missing, *unhealthy]))

            runtime_summary = RuntimeSummary(
                total_expected=len(expected),
                total_running=len(running),
                failed_containers=failed_runtime,
            )

            metrics.running_containers = len(running)
            metrics.container_start_rate = len(running) / len(expected) if expected else 0.0
            metrics.runtime_verification_success = len(failed_runtime) == 0
            metrics.container_health_status = "measured"
            if failed_runtime:
                metrics.failure_stage = FailureStage.RUNTIME_VERIFY.value

            # --- Runtime repair: containers that crashed after successful apply -
            # The kreuzwerker/docker provider marks a container as "created" the
            # moment `docker start` returns — it does not wait to confirm the
            # process stays alive.  Containers with a bad command or missing
            # config therefore pass apply but exit seconds later.  The standard
            # apply-repair loop never fires for them.  We detect them here via
            # verify_deployment() and feed them into the repair loop directly.
            if (
                (missing or unhealthy)                       # exited or unhealthy containers
                and self.config.enable_runtime_repair
                and self.extractor is not None
            ):
                _prefix = self.compiler.config.container_prefix
                runtime_failing: list[dict] = []
                _runtime_candidates = list(dict.fromkeys([*missing, *unhealthy]))
                for _cname in _runtime_candidates:
                    # Map container name back to system name via the authoritative
                    # container_to_system mapping produced by the compiler.  The
                    # old manual loop was wrong for any system name that contains
                    # characters sanitised away by _safe_name() (e.g. uppercase,
                    # hyphens), because it compared against the raw system name.
                    _sys_name = projection.container_to_system.get(_cname)
                    if _sys_name and world_model.systems.get(_sys_name) and world_model.systems[_sys_name].deploy:
                        _sys = world_model.systems[_sys_name]
                        _role = (_sys.simulate.role if _sys.simulate else "") or ""
                        _logs = await self.deployer.get_container_logs(_cname, tail=80)
                        runtime_failing.append({
                            "system_name": _sys_name,
                            "container_name": _cname,  # actual Docker container name
                            "image": _sys.deploy.image or "",
                            "command": _sys.deploy.command,
                            "env": list(_sys.deploy.env) if _sys.deploy.env else [],
                            "zone": _sys.deploy.zone or "",
                            "role": _role,
                            "error": "container exited after successful apply",
                            "docker_logs": _logs,
                        })

                if runtime_failing:
                    logger.warning(
                        "Runtime repair triggered for %d crashed container(s): %s",
                        len(runtime_failing),
                        [c["system_name"] for c in runtime_failing],
                    )
                    # Remove only the failing containers (not all) to preserve
                    # data in healthy containers' volumes.
                    _failing_cnames = [_fc["container_name"] for _fc in runtime_failing]
                    try:
                        if _failing_cnames:
                            # Remove failing containers via Docker + Terraform state
                            await self.deployer._run_docker_cmd(["rm", "-f", *_failing_cnames])
                            # Bundle all state rm operations into a single call
                            # to avoid partial state corruption if one fails.
                            # Use renderer._safe_resource_name() on the actual
                            # container name — the same transform the renderer used
                            # when it wrote the .tf.json resources.
                            _state_rm_addresses = []
                            for _fc_cname in _failing_cnames:
                                _safe = self.renderer._safe_resource_name(_fc_cname)
                                _state_rm_addresses.append(f"docker_container.{_safe}")
                                _state_rm_addresses.append(f"docker_image.{_safe}")
                            if _state_rm_addresses:
                                await self.deployer._run_tofu(["state", "rm", *_state_rm_addresses])
                            logger.info(
                                "Runtime repair: removed %d failing container(s) from state: %s",
                                len(_failing_cnames), _failing_cnames,
                            )
                    except Exception as _rt_err:
                        logger.warning(
                            "Runtime repair: targeted removal failed, falling back to full destroy: %s",
                            _rt_err,
                        )
                        try:
                            await self.deployer.destroy()
                        except Exception as _destroy_err:
                            logger.error(
                                "Runtime repair: fallback destroy() also failed: %s",
                                _destroy_err,
                            )

                    world_model, projection, _rt_apply, _ = await self._apply_repair_loop(
                        world_model, projection, work_dir, metrics, run_id,
                        pre_seeded_failing=runtime_failing,
                    )

                    if not (_rt_apply and _rt_apply.success):
                        # Repair loop failed — remove the crashing systems and
                        # re-deploy the surviving ones.  The destroy() above
                        # wiped ALL containers, so we must re-apply.
                        _crashed_names = {c["system_name"] for c in runtime_failing}
                        logger.warning(
                            "Runtime repair failed — removing %s and re-deploying survivors",
                            _crashed_names,
                        )
                        _remove_systems_and_clean_deps(world_model, _crashed_names)
                        self._save_world_model(world_model, work_dir)
                        projection = self.compiler.compile(world_model)
                        _resolution_report = await self.image_resolver.resolve_all(
                            projection, set_digest_on_containers=True,
                        )
                        self.renderer.render(projection, output_path=tofu_path)
                        tofu_json = tofu_path.read_text(encoding="utf-8")
                        metrics.expected_containers = len(projection.containers)
                        try:
                            _plan_res, _recovery_apply = await self.deployer.plan_and_apply()
                            if _recovery_apply and _recovery_apply.success:
                                logger.info("Survivor re-deploy succeeded")
                            else:
                                logger.warning(
                                    "Survivor re-deploy plan_and_apply did not succeed "
                                    "(plan=%s, apply=%s)",
                                    _plan_res.success if _plan_res else None,
                                    _recovery_apply.success if _recovery_apply else None,
                                )
                        except Exception as _redeploy_err:
                            logger.warning("Survivor re-deploy failed: %s", _redeploy_err)

                    # Refresh tofu_json so DeploymentResult reflects post-repair state.
                    if tofu_path.exists():
                        tofu_json = tofu_path.read_text(encoding="utf-8")

                    # Always re-verify after runtime repair (whether it succeeded or
                    # we fell back to survivor-only re-deploy).
                    expected = [c.name for c in projection.containers]
                    verify = await self.deployer.verify_deployment(expected)
                    running = verify.get("running", {})
                    missing = verify.get("missing", [])
                    unhealthy = verify.get("unhealthy", [])
                    failed_runtime = list(dict.fromkeys([*missing, *unhealthy]))
                    runtime_summary = RuntimeSummary(
                        total_expected=len(expected),
                        total_running=len(running),
                        failed_containers=failed_runtime,
                    )
                    metrics.running_containers = len(running)
                    metrics.container_start_rate = (
                        len(running) / len(expected) if expected else 0.0
                    )
                    metrics.runtime_verification_success = len(failed_runtime) == 0
                    if not failed_runtime:
                        metrics.failure_stage = FailureStage.COMPLETED.value
                    if _rt_apply and _rt_apply.success:
                        # Record the containers that were originally failing (before
                        # re-verify), not the post-repair list which may be empty.
                        _repaired_names = [c["system_name"] for c in runtime_failing]
                        metrics.repair_incident_count += 1
                        metrics.repair_actions.append({
                            "stage": "runtime_repair", "action": "container_restart", "target": ", ".join(_repaired_names[:5]),
                        })

            qa_runner = QARunner(tcp_connect_extra_attempts=self.config.qa_tcp_connect_extra_attempts)
            qa_checks = qa_runner.build_checks(projection)
            qa_checks = filter_canary_checks(qa_checks, self.config.qa_canary_check_ids)
            # After partial deploys, skip QA checks for containers that were never
            # deployed so the report reflects actual deployment state, not intent.
            _running_set = set(running.keys()) if isinstance(running, dict) else set()
            if _running_set and len(_running_set) < len(expected):
                qa_checks = [
                    c for c in qa_checks
                    if not c.target or c.target in _running_set
                    or c.params.get("container", "") in _running_set
                ]
            qa_report = await qa_runner.run_checks(qa_checks)

            metrics.health_checks_total = qa_report.checks_run
            metrics.health_checks_passed = qa_report.checks_passed
            metrics.health_check_rate = qa_report.pass_rate

            # Compute per-type QA pass rates
            from collections import defaultdict
            _type_total: dict[str, int] = defaultdict(int)
            _type_passed: dict[str, int] = defaultdict(int)
            for r in qa_report.results:
                ct = r.check_type or "unknown"
                _type_total[ct] += 1
                if r.passed:
                    _type_passed[ct] += 1
            metrics.health_check_rate_by_type = {
                ct: _type_passed[ct] / _type_total[ct]
                for ct in sorted(_type_total)
            }

            logger.info(qa_report.summary())

            qa_saved = self._save_qa_report(qa_report.to_dict(), work_dir, run_id)
            metrics.health_check_status = (
                "not_run" if qa_report.checks_run == 0 else "measured"
            )
            qa_ok = qa_report.checks_run > 0 and qa_report.checks_failed == 0

            # Re-verify if verify_deployment had found unhealthy/missing containers
            # earlier.  Containers marked as 'starting' (healthcheck pending) often
            # recover by the time QA finishes.  Always re-verify to get the accurate
            # final state — not just when QA is 100%.
            if failed_runtime:
                verify_final = await self.deployer.verify_deployment(expected)
                running = verify_final.get("running", {})
                missing = verify_final.get("missing", [])
                unhealthy = verify_final.get("unhealthy", [])
                failed_runtime = list(dict.fromkeys([*missing, *unhealthy]))
                runtime_summary = RuntimeSummary(
                    total_expected=len(expected),
                    total_running=len(running),
                    failed_containers=failed_runtime,
                )
                metrics.running_containers = len(running)
                metrics.container_start_rate = (
                    len(running) / len(expected) if expected else 0.0
                )
                metrics.runtime_verification_success = len(failed_runtime) == 0

            # --- Post-deployment scenario-fit metrics ---
            # Re-evaluate prompt fit against the FINAL WorldModel (after drops)
            # and the actually running containers to give honest recall/coverage.
            try:
                from .prompt_fit import compute_prompt_fit as _post_fit
                post_fit = _post_fit(user_request, world_model)
                metrics.running_service_coverage = post_fit.service_recall
                metrics.running_zone_coverage = post_fit.zone_recall
            except Exception as _pf_err:
                logger.debug("Post-deployment prompt-fit failed: %s", _pf_err)

            # Deployed dependency satisfaction: count deps where the target
            # container is actually running (not just exists in WorldModel).
            from .deploy_compiler import _safe_name as _dep_safe_name
            _prefix = self.compiler.config.container_prefix
            _running_names = set(running.keys()) if isinstance(running, dict) else set()
            _dep_total = 0
            _dep_running = 0
            for sys in world_model.systems.values():
                if sys.deploy and sys.deploy.depends_on:
                    for dep in sys.deploy.depends_on:
                        _dep_total += 1
                        # Sanitize the dependency name the same way the compiler does
                        dep_container = f"{_prefix}{_dep_safe_name(dep)}"
                        if dep_container in _running_names or dep in _running_names:
                            _dep_running += 1
            metrics.running_dep_coverage = (
                _dep_running / _dep_total if _dep_total > 0 else 0.0
            )

            # Formal benchmark fit against running containers (if benchmark configured)
            deployed_formal: dict = {}
            if self.config.benchmark_reference_path:
                try:
                    from .scenario_fit_formal import compute_deployed_scenario_fit
                    _running_set = set(running.keys()) if isinstance(running, dict) else set()
                    deployed_formal = compute_deployed_scenario_fit(
                        world_model,
                        load_benchmark_reference(self.config.benchmark_reference_path),
                        _running_set,
                        container_prefix=self.compiler.config.container_prefix,
                    )
                    # Override prompt-fit values with formal values when available
                    if deployed_formal.get("evaluation_scope") == "deployed":
                        metrics.running_service_coverage = float(
                            deployed_formal.get("running_service_coverage", metrics.running_service_coverage)
                        )
                        metrics.running_zone_coverage = float(
                            deployed_formal.get("running_zone_coverage", metrics.running_zone_coverage)
                        )
                        metrics.running_dep_coverage = float(
                            deployed_formal.get("running_dep_coverage", metrics.running_dep_coverage)
                        )
                except Exception as _dformal_err:
                    logger.debug("Deployed formal scenario-fit failed: %s", _dformal_err)

            metrics.stage_durations["runtime_verify"] = time.monotonic() - t_runtime

            # Store scenario_fit_score from the formal evaluation (use deployed if available)
            if deployed_formal.get("scenario_fit_score") is not None:
                metrics.scenario_fit_score = float(deployed_formal["scenario_fit_score"])

            if failed_runtime or not qa_ok:
                metrics.failure_stage = FailureStage.RUNTIME_VERIFY.value
            elif metrics.partial_deploy:
                metrics.failure_stage = FailureStage.PARTIAL_COMPLETED.value
            else:
                metrics.failure_stage = FailureStage.COMPLETED.value
            self._save_metrics(
                metrics,
                work_dir,
                qa_report=qa_saved,
                validation_report=last_validation_report,
            )

            status = (
                DeploymentStatus.DEPLOYED
                if not failed_runtime and qa_ok
                else DeploymentStatus.RUNTIME_DEGRADED
            )

            return DeploymentResult(
                status=status,
                failure_stage=metrics.failure_stage,
                run_id=run_id,
                world_model=world_model,
                deploy_projection=projection,
                terraform_code=tofu_json,
                running_containers=len(running),
                expected_containers=len(expected),
                runtime_summary=runtime_summary,
                summary=runtime_summary.human_summary(),
                metrics=metrics.to_dict(),
                qa_report=qa_report.to_dict(),
            )

        except Exception as e:
            logger.error("Deployment failed at stage %s: %s", _current_stage, e)
            metrics.errors.append(str(e))
            metrics.failure_stage = _current_stage
            try:
                self._save_metrics(metrics, work_dir, validation_report=last_validation_report)
            except OSError:
                pass
            return DeploymentResult(
                status=DeploymentStatus.FAILED,
                failure_stage=_current_stage,
                run_id=run_id,
                world_model=world_model,
                deploy_projection=projection,
                terraform_code=tofu_json or None,
                errors=[str(e)],
                metrics=metrics.to_dict(),
            )

    @staticmethod
    def _builtin_command_repair(world_model: WorldModel, failure_report: object) -> WorldModel:
        """Built-in repair strategy: fix invalid commands using the command policy.

        This runs BEFORE plugin strategies and handles INVALID_COMMAND errors
        raised by the ``_rule_invalid_command`` validation rule.
        """
        from .command_policy import repair_world_model_commands

        repairs = repair_world_model_commands(world_model)
        if repairs:
            logger.info(
                "Command-policy repair: fixed %d system(s): %s",
                len(repairs),
                "; ".join(repairs),
            )
        return world_model

    async def generate(self, user_request: str) -> WorldModel:
        """Generate a World Model without deploying."""
        await self.initialize()
        world_model = await self.extractor.extract(user_request)
        self._save_world_model(world_model, Path(self.config.work_dir))
        return world_model

    async def _apply_repair_loop(
        self,
        world_model: WorldModel,
        projection: "DeployProjection",
        work_dir: Path,
        metrics: DeploymentMetrics,
        run_id: str,
        *,
        pre_seeded_failing: "list[dict] | None" = None,
    ) -> tuple[WorldModel, "DeployProjection", object, list[str]]:
        """Apply → diagnose → repair → retry loop (IaCGen pattern).

        When ``pre_seeded_failing`` is provided the first iteration skips
        ``plan_and_apply()`` and uses those containers as the starting failure
        list (runtime-crash path: apply succeeded but containers exited
        afterward).  Subsequent iterations run the normal apply→check→repair
        cycle.

        Returns (world_model, projection, apply_result, errors).
        errors is empty on success.
        """
        cfg = self.config
        max_attempts = cfg.max_apply_repair_attempts
        # With pre_seeded_failing, attempt 0 is consumed by the seed (no apply).
        # We need at least 2 attempts so the repair can run and be followed by
        # an apply on attempt 1.
        if pre_seeded_failing is not None and max_attempts < 2:
            logger.info(
                "Bumping max_apply_repair_attempts from %d to 2 "
                "(minimum for runtime repair with pre-seeded failures)",
                max_attempts,
            )
            max_attempts = 2
        tofu_path = work_dir / "main.tofu.json"
        last_apply = None

        # --- Schritt 2/3 observability helpers (created once per run) --------
        guard = LoopGuard(mode="hybrid") if cfg.enable_loop_detection else None

        # RepairHistory writer — imported lazily so it's only loaded when needed
        history_writer = None
        if cfg.enable_repair_history:
            from .repair_history import RepairHistory
            history_writer = RepairHistory(run_id=run_id, work_dir=work_dir)

        # HITL reviewer — imported lazily
        hitl = None
        if cfg.enable_hitl:
            from .hitl import HITLReviewer
            hitl = HITLReviewer(trigger_threshold=cfg.hitl_trigger_threshold)

        _last_decision: RepairDecision | None = None

        for attempt in range(max_attempts):
            # --- Apply (skipped on attempt 0 when seeded from runtime crash) -
            if attempt == 0 and pre_seeded_failing is not None:
                # The deployment is already live; containers crashed post-apply.
                # Skip plan_and_apply and go straight to LLM repair.
                logger.warning(
                    "Runtime repair attempt %d/%d — %d container(s) crashed "
                    "after successful apply: %s",
                    attempt + 1, max_attempts, len(pre_seeded_failing),
                    [c["system_name"] for c in pre_seeded_failing],
                )
                failing = list(pre_seeded_failing)
                # Logs already embedded by the caller; skip startup-probe enrichment.
                skip_probe = True
            else:
                logger.info("Apply attempt %d/%d...", attempt + 1, max_attempts)
                plan_result, apply_result = await self.deployer.plan_and_apply()
                last_apply = apply_result

                # Back-fill the previous decision with the outcome of this apply
                if _last_decision is not None:
                    _last_decision.apply_succeeded_after = bool(
                        apply_result and apply_result.success
                    )

                if apply_result and apply_result.success:
                    logger.info("Apply succeeded on attempt %d", attempt + 1)
                    metrics.deploy_retry_count = attempt + 1
                    return world_model, projection, apply_result, []

                if not apply_result:
                    return world_model, projection, apply_result, ["Apply did not produce a result"]

                stderr = apply_result.stderr or ""
                failing = self._parse_failing_containers(
                    stderr, world_model,
                    container_prefix=self.compiler.config.container_prefix,
                )
                if not failing:
                    logger.warning("Apply failed but could not identify failing containers")
                    break

                logger.warning(
                    "Apply attempt %d failed — %d containers crashed: %s",
                    attempt + 1, len(failing),
                    [c["system_name"] for c in failing],
                )
                skip_probe = False

            # Don't repair on the very last attempt — there would be no further
            # apply to benefit from the repair.  With max_attempts=N the loop
            # runs attempts 0…N-1; repair is skipped only on attempt N-1.
            if attempt >= max_attempts - 1:
                if last_apply is None:
                    logger.warning(
                        "Repair loop exiting with last_apply=None — "
                        "plan_and_apply was never called (pre_seeded_failing with "
                        "max_attempts=%d). Failing containers: %s",
                        max_attempts,
                        [c["system_name"] for c in failing],
                    )
                break

            # --- Enrich with docker logs (startup probe) ---------------------
            if not skip_probe:
                failing = await self._enrich_failing_containers_with_logs(failing)

            # --- Failure analysis (Schritt 3) --------------------------------
            failure_contexts: list[FailureContext] = []
            if cfg.enable_failure_analysis:
                failure_contexts = enrich_failing_list(failing, attempt=attempt)
                # Attach classified type back onto the raw dicts for the LLM prompt
                fc_by_name = {fc.system_name: fc for fc in failure_contexts}
                for d in failing:
                    fc = fc_by_name.get(d["system_name"])
                    if fc:
                        d["failure_type"] = fc.failure_type.value
                        d["diagnosis"] = fc.diagnosis

            # --- Loop detection: record current images -----------------------
            if guard:
                guard.record_contexts(failure_contexts or [
                    FailureContext(
                        system_name=d["system_name"],
                        image=d.get("image", ""),
                        command=d.get("command"),
                        zone=d.get("zone", ""),
                        role=d.get("role", ""),
                    )
                    for d in failing
                ])

            # --- Ask LLM to repair -------------------------------------------
            metrics.deploy_retried = True
            import copy as _copy
            _world_model_snapshot = _copy.deepcopy(world_model)
            proposals: list[RepairProposal] = []
            if self.extractor:
                try:
                    world_model = await self.extractor.repair_containers(
                        world_model, failing,
                    )
                    # Build proposal list from before/after comparison
                    for d in failing:
                        sn = d["system_name"]
                        sys = world_model.systems.get(sn)
                        new_img = sys.deploy.image if sys and sys.deploy else d.get("image", "")
                        proposals.append(RepairProposal(
                            system_name=sn,
                            original_image=d.get("image", ""),
                            proposed_image=new_img,
                            proposed_command=sys.deploy.command if sys and sys.deploy else d.get("command"),
                        ))
                except Exception as e:
                    logger.warning("Container repair LLM call failed: %s", e)
                    # Record the failed attempt in repair history before breaking
                    if history_writer:
                        from .utils import utc_now_iso
                        _fail_decision = RepairDecision(
                            run_id=run_id,
                            attempt=attempt,
                            failing_containers=failure_contexts,
                            proposals=[],
                            loop_detected=False,
                            loop_reason=f"LLM call failed: {e}",
                            timestamp=utc_now_iso(),
                        )
                        history_writer.record(_fail_decision)
                    world_model = _world_model_snapshot
                    break

            # --- Guard: reject config-required images without volumes ---------
            # Images like haproxy/traefik/envoy/prometheus need a mounted config
            # Guard: use ImageProfile to check if the proposed image has a daemon
            # entrypoint.  If not (e.g. base runtime), mark the proposal for skip
            # rather than silently substituting a stand-in.
            if proposals:
                from .image_introspector import get_image_profile as _get_profile
                for prop in proposals:
                    if prop.proposed_image == prop.original_image:
                        continue
                    try:
                        _profile = await _get_profile(prop.proposed_image, timeout=10)
                        if _profile.available and not _profile.has_daemon_entrypoint:
                            logger.warning(
                                "Repair proposed non-daemon image '%s' for '%s' — "
                                "skipping proposal (image has no daemon entrypoint)",
                                prop.proposed_image, prop.system_name,
                            )
                            prop.applied = False
                            prop.skip_reason = "proposed image has no daemon entrypoint"
                            # Roll back image to original
                            sys = world_model.systems.get(prop.system_name)
                            if sys and sys.deploy:
                                sys.deploy.image = prop.original_image
                    except Exception as _prof_err:
                        logger.debug("Profile check for %s failed: %s", prop.proposed_image, _prof_err)

            # --- Repair scoring (Schritt 5) ----------------------------------
            if cfg.enable_repair_scoring and proposals and failure_contexts:
                proposals = score_proposals(failure_contexts, proposals)
                for prop in proposals:
                    if prop.score is not None and prop.score < cfg.repair_score_warn_threshold:
                        logger.warning(
                            "Low-confidence repair for %s (score=%.2f): %s",
                            prop.system_name, prop.score, prop.score_reason,
                        )
                    if (
                        prop.score is not None
                        and prop.score < cfg.repair_score_apply_threshold
                    ):
                        logger.warning(
                            "Skipping low-score repair for %s (%.2f < threshold %.2f)",
                            prop.system_name, prop.score, cfg.repair_score_apply_threshold,
                        )
                        prop.applied = False
                        prop.skip_reason = f"score {prop.score:.2f} below threshold {cfg.repair_score_apply_threshold}"
                        # Roll back ALL deploy changes using snapshot
                        _snap_sys = _world_model_snapshot.systems.get(prop.system_name)
                        sys = world_model.systems.get(prop.system_name)
                        if _snap_sys and _snap_sys.deploy and sys and sys.deploy:
                            sys.deploy.image = _snap_sys.deploy.image
                            sys.deploy.command = _snap_sys.deploy.command
                            sys.deploy.env = list(_snap_sys.deploy.env) if _snap_sys.deploy.env else None
                            sys.deploy.ports = list(_snap_sys.deploy.ports) if _snap_sys.deploy.ports else []
                            sys.deploy.depends_on = list(_snap_sys.deploy.depends_on) if _snap_sys.deploy.depends_on else []
                            sys.deploy.zone = _snap_sys.deploy.zone
                            sys.deploy.healthcheck = _snap_sys.deploy.healthcheck

            # --- Loop detection: check proposals ----------------------------
            loop_detected = False
            loop_reason = ""
            if guard and proposals:
                loop_detected, loop_reason = guard.check(proposals, attempt=attempt)
                if loop_detected:
                    logger.warning(
                        "Repair loop detected at attempt %d — aborting repair: %s",
                        attempt + 1, loop_reason,
                    )

            # --- HITL review (Schritt 6) ------------------------------------
            if hitl and proposals and not loop_detected:
                proposals = await hitl.review(proposals, failure_contexts)
                # Roll back ALL changes (image, command, env) for operator-
                # skipped proposals using the pre-LLM snapshot — consistent
                # with the scorer rollback above (lines 1783-1789).
                for prop in proposals:
                    if prop.applied is False:
                        _snap_sys = _world_model_snapshot.systems.get(prop.system_name)
                        sys = world_model.systems.get(prop.system_name)
                        if _snap_sys and _snap_sys.deploy and sys and sys.deploy:
                            sys.deploy.image = _snap_sys.deploy.image
                            sys.deploy.command = _snap_sys.deploy.command
                            sys.deploy.env = list(_snap_sys.deploy.env) if _snap_sys.deploy.env else None
                            sys.deploy.ports = list(_snap_sys.deploy.ports) if _snap_sys.deploy.ports else []
                            sys.deploy.depends_on = list(_snap_sys.deploy.depends_on) if _snap_sys.deploy.depends_on else []
                            sys.deploy.zone = _snap_sys.deploy.zone
                            sys.deploy.healthcheck = _snap_sys.deploy.healthcheck
                            logger.info(
                                "HITL: rolled back %s to snapshot (image=%s)",
                                prop.system_name, _snap_sys.deploy.image,
                            )

            # --- Write repair history ----------------------------------------
            # Always record the attempt, even when proposals is empty (LLM failed).
            if history_writer:
                from .utils import utc_now_iso
                decision = RepairDecision(
                    run_id=run_id,
                    attempt=attempt,
                    failing_containers=failure_contexts,
                    proposals=proposals,
                    loop_detected=loop_detected,
                    loop_reason=loop_reason,
                    timestamp=utc_now_iso(),
                )
                history_writer.record(decision)
                _last_decision = decision

            if loop_detected:
                # Restore the WorldModel to the pre-LLM state so that any
                # subsequent partial-apply or downstream steps don't use the
                # LLM's rejected mutations.
                world_model = _world_model_snapshot
                break

            # Record proposals AFTER the loop check so the loop-detection
            # history is not contaminated before check() runs.
            # Only record actually-applied proposals — scorer-rejected and
            # HITL-skipped proposals must NOT pollute the history, otherwise
            # the guard will flag them as "already tried" on the next attempt.
            if guard and proposals:
                applied_proposals = [p for p in proposals if p.applied is not False]
                if applied_proposals:
                    guard.record_proposals(applied_proposals)

            # --- Command-policy repair (deterministic, profile-based) ----------
            from .command_policy import repair_world_model_commands
            from .image_introspector import get_image_profiles as _get_profiles
            try:
                _imgs = [s.deploy.image for s in world_model.systems.values() if s.deploy and s.deploy.image]
                _repair_profiles = await _get_profiles(_imgs)
            except Exception:
                _repair_profiles = {}
            cmd_repairs = repair_world_model_commands(world_model, profiles=_repair_profiles)
            if cmd_repairs:
                logger.info("Command-policy also fixed: %s", cmd_repairs)

            # --- Re-validate after repair to catch structural errors -----------
            # The LLM repair may have introduced invalid ports, circular deps,
            # missing zones, or other structural issues that would cause tofu
            # apply to fail confusingly.
            try:
                _post_repair_val = validate(world_model)
                if _post_repair_val.errors:
                    _err_strs = [e.details for e in _post_repair_val.errors[:5]]
                    logger.warning(
                        "Post-repair validation found %d error(s): %s — rolling back",
                        len(_post_repair_val.errors), "; ".join(_err_strs),
                    )
                    world_model = _world_model_snapshot
                    break
            except Exception as _val_err:
                logger.debug("Post-repair validation failed: %s", _val_err)

            # --- Recompile + re-render ---------------------------------------
            # Refresh the port seed: the destroy() above freed honeynet ports,
            # so re-querying gives an accurate picture of what the host occupies.
            try:
                occupied = await self.deployer.get_used_ports()
                self.compiler.reset_seed()
                if occupied:
                    self.compiler.seed_used_ports(occupied)
            except Exception as _seed_err:
                logger.debug("Could not refresh port seed before recompile: %s", _seed_err)
            try:
                projection = self.compiler.compile(world_model)
                _res_report = await self.image_resolver.resolve_all(
                    projection, set_digest_on_containers=True,
                )
                self.renderer.render(projection, output_path=tofu_path)
                self._save_world_model(world_model, work_dir)
            except Exception as e:
                logger.error("Recompile after repair failed: %s — rolling back world model", e)
                world_model = _world_model_snapshot
                break

            # Destroy old state before retry
            try:
                await self.deployer.destroy()
            except Exception as e:
                logger.warning("Destroy before retry failed: %s", e)

        # All repair attempts exhausted
        succeeded = bool(last_apply and last_apply.success)
        any_loop = history_writer.any_loop_detected() if history_writer else False
        if history_writer:
            try:
                history_writer.finalize(
                    total_attempts=max_attempts,
                    succeeded=succeeded,
                    loop_detected=any_loop,
                )
            except (IOError, OSError) as e:
                logger.warning("Failed to write repair history summary: %s", e)

        # If the loop exited without a successful apply (break on no-containers-
        # identified, loop-detected, recompile-failed, or last-attempt exhausted)
        # any partial Docker state from the last failed apply is still running.
        # Tear it down now so callers start from a clean slate.  Recovery path 3
        # (partial apply) calls destroy() itself, so a no-op here is harmless.
        if not succeeded:
            try:
                await self.deployer.destroy()
            except Exception as _destroy_err:
                logger.warning(
                    "Post-repair-loop cleanup destroy failed: %s", _destroy_err
                )

        errors = []
        if last_apply and not last_apply.success:
            errors.append(f"tofu apply failed after {max_attempts} repair attempts: {last_apply.stderr}")
        return world_model, projection, last_apply, errors

    @staticmethod
    def _parse_failing_containers(
        stderr: str, world_model: WorldModel,
        *, container_prefix: str = "hn_",
    ) -> list[dict]:
        """Extract failing container names from tofu apply stderr and enrich
        with WorldModel metadata for the repair prompt."""
        import re as _re

        # Pattern: "with docker_container.hn_something," or "container exited immediately"
        resource_pattern = _re.compile(
            r'with\s+docker_container\.(\S+?),',
        )
        failing_names: list[str] = []
        for match in resource_pattern.finditer(stderr):
            name = match.group(1)
            if name not in failing_names:
                failing_names.append(name)

        if not failing_names:
            return []

        # Map resource names back to system names
        # Resource names are like "hn_job_scheduler" → system names are like "job_scheduler"
        result: list[dict] = []
        for resource_name in failing_names:
            # Try to find matching system (strip container prefix and sanitize)
            sys_name = None
            for sn in world_model.systems:
                # The renderer prefixes and sanitizes system names
                from .deploy_compiler import _safe_name
                if resource_name == f"{container_prefix}{_safe_name(sn)}" or resource_name == sn:
                    sys_name = sn
                    break
            # Also try without prefix normalization
            if not sys_name:
                clean = resource_name.removeprefix(container_prefix)
                if clean in world_model.systems:
                    sys_name = clean

            if sys_name and world_model.systems[sys_name].deploy:
                sys = world_model.systems[sys_name]
                role = ""
                if sys.simulate:
                    role = getattr(sys.simulate, "role", "") or ""
                result.append({
                    "system_name": sys_name,
                    "image": sys.deploy.image or "",
                    "command": sys.deploy.command,
                    "env": list(sys.deploy.env) if sys.deploy.env else [],
                    "zone": sys.deploy.zone or "",
                    "role": role,
                    "error": "container exited immediately or failed to be in running state",
                })

        return result

    async def _probe_containers_before_deploy(
        self,
        world_model: WorldModel,
        projection: "DeployProjection",
    ) -> list[dict]:
        """Quick-start each container to detect startup failures before tofu apply.

        Returns a list of failing-container dicts (same format as
        _parse_failing_containers) that can be fed directly to repair_containers.
        Only probes containers whose images are available locally or pullable.
        """
        loop = asyncio.get_running_loop()
        failures: list[dict] = []

        from .image_introspector import get_image_profile as _probe_get_profile

        # Images that need a config file mounted to start — probing them bare
        # always fails and wastes time.  Detect via OCI entrypoint heuristics.
        _CONFIG_DEPENDENT_ENTRYPOINTS = frozenset({
            "haproxy", "envoy", "squid", "varnishd", "lighttpd",
        })

        for sys_name, system in world_model.systems.items():
            if not system.deploy or not system.deploy.image:
                continue
            image = system.deploy.image
            command = system.deploy.command if isinstance(system.deploy.command, list) else []
            env_dict: dict[str, str] = {}
            for pair in system.deploy.env or []:
                if "=" in pair:
                    k, _, v = pair.partition("=")
                    env_dict[k] = v

            # Skip probing images that cannot run standalone:
            # 1. Base runtimes (node, python) without a command — they exit immediately
            # 2. Config-dependent images (haproxy, envoy) — need mounted config files
            try:
                _pre_profile = await _probe_get_profile(image, timeout=8)
            except Exception:
                _pre_profile = None
            if _pre_profile and _pre_profile.available:
                # Base runtime without command → guaranteed to exit immediately
                if _pre_profile.is_base_runtime and not command:
                    logger.debug(
                        "Pre-deploy probe skipped for '%s' (%s): base runtime without command",
                        sys_name, image,
                    )
                    continue
                # Config-dependent daemon → probe always fails without mounted config
                if _pre_profile.entrypoint:
                    ep_basename = _pre_profile.entrypoint[-1].rsplit("/", 1)[-1].lower()
                    if ep_basename in _CONFIG_DEPENDENT_ENTRYPOINTS:
                        logger.debug(
                            "Pre-deploy probe skipped for '%s' (%s): config-dependent image",
                            sys_name, image,
                        )
                        continue

            try:
                result = await loop.run_in_executor(
                    None,
                    lambda img=image, cmd=command, env=env_dict: probe_startup_profile(
                        img, cmd, env, timeout_seconds=12,
                    ),
                )
                if not result.success:
                    role = ""
                    if system.simulate:
                        role = getattr(system.simulate, "role", "") or ""
                    diagnostic = result.logs or result.error or ""
                    failures.append({
                        "system_name": sys_name,
                        "image": image,
                        "command": system.deploy.command,
                        "env": list(system.deploy.env) if system.deploy.env else [],
                        "zone": system.deploy.zone or "",
                        "role": role,
                        "error": f"pre-deploy probe failed: {result.error or 'container exited'}",
                        "docker_logs": diagnostic[-1500:] if diagnostic else "",
                    })
                    logger.info(
                        "Pre-deploy probe FAILED for '%s' (%s): %s",
                        sys_name, image, (diagnostic[:200] if diagnostic else result.error),
                    )
                else:
                    logger.debug("Pre-deploy probe OK for '%s' (%s)", sys_name, image)
            except Exception as exc:
                logger.debug("Pre-deploy probe skipped for '%s': %s", sys_name, exc)

        return failures

    async def _enrich_failing_containers_with_logs(
        self, failing: list[dict],
    ) -> list[dict]:
        """Run each failing container via startup probe and capture diagnostic output.

        Uses ``probe_startup_profile`` which polls ``docker inspect`` to determine
        whether the container actually stays alive as a daemon.  Containers that exit
        immediately have their startup logs (up to 80 lines) captured and attached to
        the failing-container dict so the LLM repair prompt receives concrete error
        messages (e.g. "can't open file 'app.py'") instead of generic crash signals.

        This is best-effort: probe failures are silently ignored and the original
        failing dict is returned unchanged.
        """
        loop = asyncio.get_running_loop()
        enriched: list[dict] = []
        for c in failing:
            entry = dict(c)
            image = c.get("image", "")
            command = c.get("command")
            if not image:
                enriched.append(entry)
                continue

            cmd_list: list[str] = command if isinstance(command, list) else []
            # Collect env vars from the world model entry (stored as "KEY=VAL" strings)
            raw_env: list[str] = c.get("env", []) or []
            env_dict: dict[str, str] = {}
            for pair in raw_env:
                if "=" in pair:
                    k, _, v = pair.partition("=")
                    env_dict[k] = v

            try:
                result = await loop.run_in_executor(
                    None,
                    lambda img=image, cmd=cmd_list, env=env_dict: probe_startup_profile(
                        img, cmd, env, timeout_seconds=10,
                    ),
                )
                if result.success:
                    # Container is healthy — no log enrichment needed
                    logger.debug(
                        "Pre-repair probe: '%s' is running fine — may be a transient failure",
                        c.get("system_name", "?"),
                    )
                else:
                    diagnostic = result.logs or result.error or ""
                    if diagnostic:
                        entry["docker_logs"] = diagnostic[-1500:]
                        logger.info(
                            "Pre-repair probe for '%s' (failed, status=%r): %s",
                            c.get("system_name", "?"),
                            result.error,
                            diagnostic[:200],
                        )
            except Exception as exc:
                logger.debug(
                    "Pre-repair probe failed for '%s': %s",
                    c.get("system_name", "?"), exc,
                )

            enriched.append(entry)
        return enriched

    async def _image_repair_loop(
        self, world_model: WorldModel,
    ) -> tuple[WorldModel, list[str], int, int]:
        """Validate images and ask LLM to fix any that don't exist.

        Returns (world_model, errors, total_images, first_pass_resolved).
        errors is empty on success.
        total_images and first_pass_resolved track how many images resolved
        on the FIRST attempt (before any repair), for accurate metrics.

        Includes ping-pong detection: if the LLM suggests the same failing
        namespace twice, the system auto-substitutes with a generic stand-in
        image instead of looping forever.
        """
        max_attempts = self.config.max_image_repair_attempts
        first_pass_total = 0
        first_pass_resolved = 0
        # Track which image namespaces have failed across attempts (ping-pong detection)
        seen_failures: dict[str, int] = {}  # base_name → failure count

        for attempt in range(max_attempts):
            images = [
                s.deploy.image
                for s in world_model.systems.values()
                if s.deploy and s.deploy.image
            ]

            if not images:
                return world_model, ["No deployable systems with images found"], 0, 0

            report = await self.image_resolver.resolve_images(images)
            promote_soft = self.config.image_soft_fail_is_blocking
            failed = report.blocking_unresolved(promote_soft=promote_soft)
            soft = report.soft_unresolved()
            if soft and not promote_soft:
                for s in soft:
                    logger.warning("Soft image failure (non-blocking): %s — %s", s.image_ref, s.message)

            # Capture first-pass stats before any repair
            if attempt == 0:
                first_pass_total = len(images)
                first_pass_resolved = len(images) - len(failed)

            if not failed:
                logger.info("All %d images validated successfully", len(images))
                return world_model, [], first_pass_total, first_pass_resolved

            # Track failure count per base image namespace
            for e in failed:
                base = e.image_ref.split(":")[0] if ":" in e.image_ref else e.image_ref
                seen_failures[base] = seen_failures.get(base, 0) + 1

            # Ping-pong detection: if any base name has failed 2+ times,
            # remove the systems using that image instead of substituting a stand-in.
            systems_to_skip: set[str] = set()
            pingpong_bases: set[str] = set()
            for e in failed:
                base = e.image_ref.split(":")[0] if ":" in e.image_ref else e.image_ref
                if seen_failures.get(base, 0) >= 2:
                    logger.warning(
                        "Image namespace '%s' failed %d times (ping-pong detected) — "
                        "removing systems using this namespace",
                        base, seen_failures[base],
                    )
                    pingpong_bases.add(base)
            if pingpong_bases:
                for sys_name, sys in list(world_model.systems.items()):
                    if sys.deploy and sys.deploy.image:
                        sys_base = sys.deploy.image.split(":")[0] if ":" in sys.deploy.image else sys.deploy.image
                        if sys_base in pingpong_bases:
                            systems_to_skip.add(sys_name)

            if systems_to_skip:
                _remove_systems_and_clean_deps(world_model, systems_to_skip)
                continue  # Re-validate without removed systems

            failed_info = [
                {
                    "image": e.image_ref,
                    "status": e.status.value,
                    "message": e.message,
                }
                for e in failed
            ]

            logger.warning(
                "Attempt %d/%d: %d images failed validation: %s",
                attempt + 1,
                max_attempts,
                len(failed),
                [f["image"] for f in failed_info],
            )

            if attempt < max_attempts - 1:
                world_model = await self.extractor.repair_images(
                    world_model, failed_info
                )

        # Final check
        images = [s.deploy.image for s in world_model.systems.values() if s.deploy and s.deploy.image]
        if not images:
            return world_model, ["No deployable systems with images found"], first_pass_total, first_pass_resolved

        report = await self.image_resolver.resolve_images(images)
        still_failed = report.blocking_unresolved(promote_soft=self.config.image_soft_fail_is_blocking)

        if still_failed:
            # Last resort: remove systems with unrepairable images
            systems_to_remove: set[str] = set()
            for e in still_failed:
                logger.warning(
                    "Final fallback: removing systems with unrepairable image '%s'",
                    e.image_ref,
                )
                for sys_name, sys in list(world_model.systems.items()):
                    if sys.deploy and sys.deploy.image == e.image_ref:
                        systems_to_remove.add(sys_name)

            _remove_systems_and_clean_deps(world_model, systems_to_remove)

            # Re-validate after removals
            images = [s.deploy.image for s in world_model.systems.values() if s.deploy and s.deploy.image]
            if not images:
                return world_model, ["All systems removed — no deployable images remain"], first_pass_total, first_pass_resolved
            report = await self.image_resolver.resolve_images(images)
            still_failed = report.blocking_unresolved(promote_soft=self.config.image_soft_fail_is_blocking)
            if still_failed:
                errors = [
                    f"Image '{e.image_ref}' not found ({e.status.value}): {e.message}"
                    for e in still_failed
                ]
                return world_model, errors, first_pass_total, first_pass_resolved

        return world_model, [], first_pass_total, first_pass_resolved

    async def _import_existing_docker_resources(
        self, apply_stderr: str, projection: "DeployProjection"
    ) -> bool:
        """Attempt to ``tofu import`` Docker resources that already exist.

        Parses the apply stderr for "already exists" errors, maps them to
        resource addresses from the projection, and runs ``tofu import`` for each.
        Returns True if at least one import succeeded.
        """
        import re as _re

        # Extract resource names from error messages like:
        #   "with docker_network.my_network," or "with docker_container.my_container,"
        # Only match resource types we can actually import (network + container).
        # docker_image and docker_volume are not supported by this recovery path.
        resource_pattern = _re.compile(
            r'with\s+(docker_(?:network|container)\.(\S+?)),',
        )
        resources_to_import: list[tuple[str, str]] = []  # (tf_address, docker_name)
        for match in resource_pattern.finditer(apply_stderr):
            tf_address = match.group(1)
            resources_to_import.append((tf_address, match.group(2)))

        if not resources_to_import:
            return False

        # Build a mapping from TF resource address → Docker resource ID
        # For networks: the Docker name is the network name
        # For containers: the Docker name is the container name
        net_map: dict[str, str] = {}
        for net in projection.networks:
            safe = self.renderer._safe_resource_name(net.name) if self.renderer else net.name
            net_map[f"docker_network.{safe}"] = net.name

        container_map: dict[str, str] = {}
        for container in projection.containers:
            safe = self.renderer._safe_resource_name(container.name) if self.renderer else container.name
            container_map[f"docker_container.{safe}"] = container.name

        any_imported = False
        for tf_address, _ in resources_to_import:
            docker_id = net_map.get(tf_address) or container_map.get(tf_address)
            if not docker_id:
                # Try to use the resource name directly
                parts = tf_address.split(".", 1)
                docker_id = parts[1] if len(parts) == 2 else None
            if not docker_id:
                logger.debug("Cannot determine Docker ID for %s — skipping import", tf_address)
                continue

            logger.info("Importing %s (docker_id=%s)...", tf_address, docker_id)
            result = await self.deployer._run_tofu(["import", tf_address, docker_id])
            if result.success:
                any_imported = True
                logger.info("Successfully imported %s", tf_address)
            else:
                logger.warning("Import failed for %s: %s", tf_address, result.stderr[:200])

        return any_imported

    async def _cleanup_old_state(self, work_dir: Path) -> None:
        """Destroy existing Docker resources and remove old terraform state files.

        If a terraform state file exists, run ``tofu destroy`` first to tear down
        the actual Docker networks/containers before deleting the state. If destroy
        fails, state files are **preserved** so the user can inspect / retry manually
        — deleting state after a failed destroy would orphan live Docker resources
        with no way to manage them through OpenTofu.

        If no state file exists but orphaned Docker networks/containers from a
        previous run are still present (matching the project name prefix), those
        are removed directly via ``docker`` CLI. This covers the common case where
        state was deleted manually or a previous deploy crashed after ``apply``
        but before state was written.
        """
        state_file = work_dir / "terraform.tfstate"
        tf_dir = work_dir / ".terraform"

        destroy_ok = True
        if state_file.exists():
            # State file references live Docker resources — must destroy before
            # deleting state, regardless of whether .terraform/ exists.
            # If .terraform/ is missing, run init first to reconstruct it.
            logger.info("Destroying previous deployment before re-deploy...")
            try:
                if not tf_dir.exists():
                    logger.info(".terraform missing — running init to reconstruct provider state...")
                await self.deployer.init()
                result = await self.deployer.destroy()
                if result.success:
                    logger.info("Previous deployment destroyed successfully")
                else:
                    destroy_ok = False
                    logger.warning(
                        "tofu destroy failed — keeping state files to avoid orphaning "
                        "Docker resources. Run 'honeynet destroy' manually first.\n%s",
                        result.stderr[:500],
                    )
            except Exception as e:
                destroy_ok = False
                logger.warning(
                    "Could not destroy previous deployment — keeping state files: %s", e
                )

        if not destroy_ok:
            # Only remove transient artifacts, keep state so user can retry destroy
            for name in ["tfplan"]:
                path = work_dir / name
                if path.exists():
                    try:
                        path.unlink()
                    except OSError as e:
                        logger.warning("Could not remove %s: %s", path.name, e)
            return

        # Clean up orphaned Docker resources that survived state deletion
        await self._remove_orphaned_docker_resources()

        for name in ["terraform.tfstate", "terraform.tfstate.backup", "tfplan", ".terraform.lock.hcl"]:
            path = work_dir / name
            if path.exists():
                try:
                    path.unlink()
                except OSError as e:
                    logger.warning("Could not remove %s: %s", path.name, e)
        if tf_dir.exists():
            import shutil
            shutil.rmtree(tf_dir, ignore_errors=True)

    async def _remove_orphaned_docker_resources(self) -> None:
        """Remove Docker containers and networks left over from a previous deploy.

        Scoped to the current project: containers are matched by the project-
        specific ``hn_`` prefix AND the project name derived from the saved
        world model.  This prevents accidentally deleting resources belonging
        to a different honeynet project on the same host.

        If no project name can be determined (no world_model.json), cleanup
        is skipped entirely to avoid removing foreign resources.
        """
        import re as _re
        import subprocess

        loop = asyncio.get_running_loop()

        # Determine project name for scoped cleanup
        project_name = None
        wm_json = Path(self.config.work_dir) / "world_model.json"
        wm_yaml = Path(self.config.work_dir) / "world_model.yaml"
        for wm_path in (wm_json, wm_yaml):
            if not wm_path.exists():
                continue
            try:
                if wm_path.suffix == ".json":
                    wm_data = json.loads(wm_path.read_text(encoding="utf-8"))
                else:
                    import yaml
                    wm_data = yaml.safe_load(wm_path.read_text(encoding="utf-8"))
                org = wm_data.get("organization") if isinstance(wm_data, dict) else None
                org_name = org.get("name", "") if isinstance(org, dict) else ""
                if org_name:
                    project_name = _re.sub(r"[^a-z0-9]+", "_", org_name.lower()).strip("_")
                    break
            except Exception:
                continue

        if not project_name:
            logger.debug(
                "Orphan cleanup skipped: cannot determine project name from %s",
                self.config.work_dir,
            )
            return

        # --- containers scoped to this project ---
        # Preferred: filter by Docker label ``honeynet.project=<project_name>``
        # (set by TofuRenderer on every managed container).
        # Fallback: prefix-based filter with strict startswith() check.
        container_prefix = self.compiler.config.container_prefix if self.compiler else "hn_"
        try:
            # Try label-based filter first (most precise)
            result = await loop.run_in_executor(
                None,
                lambda pn=project_name: subprocess.run(
                    [
                        "docker", "ps", "-a",
                        "--filter", f"label=honeynet.project={pn}",
                        "--format", "{{.Names}}",
                    ],
                    capture_output=True, text=True, timeout=15,
                ),
            )
            label_containers: list[str] = []
            if result.returncode == 0 and result.stdout.strip():
                label_containers = result.stdout.strip().split("\n")

            if not label_containers:
                # Fallback: prefix-based filter with strict startswith check.
                # Docker ``--filter name=hn_`` uses *substring* matching, so a
                # container named ``other_hn_foo`` would match too.
                result = await loop.run_in_executor(
                    None,
                    lambda pfx=container_prefix: subprocess.run(
                        ["docker", "ps", "-a", "--filter", f"name={pfx}", "--format", "{{.Names}}"],
                        capture_output=True, text=True, timeout=15,
                    ),
                )
                if result.returncode == 0 and result.stdout.strip():
                    all_containers = result.stdout.strip().split("\n")
                    label_containers = [c for c in all_containers if c.startswith(container_prefix)]

            if label_containers:
                logger.info(
                    "Removing %d orphaned containers (project=%s): %s",
                    len(label_containers), project_name, label_containers,
                )
                await loop.run_in_executor(
                    None,
                    lambda cs=label_containers: subprocess.run(
                        ["docker", "rm", "-f", *cs],
                        capture_output=True, text=True, timeout=30,
                    ),
                )
        except Exception as e:
            logger.debug("Orphan container cleanup skipped: %s", e)

        # --- networks matching project name prefix ---
        try:
            result = await loop.run_in_executor(
                None,
                lambda pn=project_name: subprocess.run(
                    ["docker", "network", "ls", "--filter", f"name={pn}_", "--format", "{{.Name}}"],
                    capture_output=True, text=True, timeout=15,
                ),
            )
            if result.returncode == 0 and result.stdout.strip():
                networks = result.stdout.strip().split("\n")
                logger.info("Removing %d orphaned networks: %s", len(networks), networks)
                for net in networks:
                    try:
                        await loop.run_in_executor(
                            None,
                            lambda n=net: subprocess.run(
                                ["docker", "network", "rm", n],
                                capture_output=True, text=True, timeout=15,
                            ),
                        )
                    except Exception as e:
                        logger.debug("Could not remove network %s: %s", net, e)
        except Exception as e:
            logger.debug("Orphan network cleanup skipped: %s", e)

    # ── Thin delegation to ArtifactWriter ────────────────────────────────
    # These keep backward compatibility for existing callers while the real
    # logic lives in pipeline_artifacts.py.

    def _load_command_warning_rules(self, work_dir: Path) -> set[str]:
        return ArtifactWriter.load_command_warning_rules(work_dir)

    def _save_deployer_stages_snapshot(self, work_dir: Path, run_id: str, stage_results: Any) -> None:
        self.artifacts.save_deployer_stages_snapshot(work_dir, run_id, stage_results)

    def _emit_repair_incident_stage_failure(
        self, work_dir: Path, run_id: str, phase: str, stderr_text: str,
    ) -> bool:
        return self.artifacts.emit_repair_incident_stage_failure(work_dir, run_id, phase, stderr_text)

    def _save_metrics(
        self, metrics: DeploymentMetrics, work_dir: Path, *,
        qa_report: dict | None = None, validation_report: dict | None = None,
    ) -> None:
        # Auto-classify error_class if a failure occurred and it's not yet set.
        # Skip classification for success stages — they are not errors.
        _success_stages = {"completed", "partial_completed"}
        if metrics.failure_stage and metrics.failure_stage not in _success_stages and not metrics.error_class:
            error_text = metrics.errors[-1] if metrics.errors else ""
            metrics.error_class = _classify_error(metrics.failure_stage, error_text)
        self.artifacts.save_metrics(metrics, work_dir, qa_report=qa_report, validation_report=validation_report)

    def _save_validation_report(self, report: dict, work_dir: Path, run_id: str = "") -> dict:
        return self.artifacts.save_validation_report(report, work_dir, run_id)

    def _save_qa_report(self, qa_report: dict, work_dir: Path, run_id: str = "") -> dict:
        return self.artifacts.save_qa_report(qa_report, work_dir, run_id)

    def _save_scenario_fit_report(self, report: dict, work_dir: Path) -> None:
        self.artifacts.save_scenario_fit_report(report, work_dir)

    def _save_catalog_resolution(self, work_dir: Path, run_id: str, applied: list[dict[str, str]]) -> None:
        self.artifacts.save_catalog_resolution(work_dir, run_id, applied)

    def _save_world_model(self, world_model: WorldModel, work_dir: Path) -> None:
        self.artifacts.save_world_model(world_model, work_dir)

        logger.info("Saved world model to %s", work_dir)
