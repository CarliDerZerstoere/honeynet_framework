"""
Tests for previously untested modules:
- prompt_fit.py
- loop_guard.py
- repair_scorer.py
- consistency_fixer.py
- precompile_validator.py
- scenario_fit_formal.py
- New metrics fields (dropped_containers, deployed_* recall)
"""

from pathlib import Path

import pytest
from honeynet_framework.models import (
    System, SystemDeploy, SystemSimulate, SystemKind,
    WorldModel, WorldZone, ZoneDeploy,
)


def _make_world_model(
    systems: dict[str, tuple[str, str, str]] | None = None,
    zones: list[str] | None = None,
) -> WorldModel:
    """Helper: systems = {name: (image, zone, kind)}, zones = [name, ...]"""
    wm = WorldModel()
    for zn in (zones or ["internal"]):
        wm.zones[zn] = WorldZone(
            name=zn,
            deploy=ZoneDeploy(network_name=zn, subnet=f"172.30.1.0/24"),
        )
    for name, (image, zone, kind) in (systems or {}).items():
        wm.systems[name] = System(
            name=name,
            kind=SystemKind.coerce(kind),
            deploy=SystemDeploy(image=image, zone=zone, ports=[80]),
            simulate=SystemSimulate(role=name.replace("_", " ")),
        )
    return wm


# ===========================================================================
# prompt_fit.py
# ===========================================================================

class TestPromptFit:
    def test_extract_tech_patterns(self):
        from honeynet_framework.prompt_fit import extract_prompt_requirements
        reqs = extract_prompt_requirements("Deploy a PostgreSQL database with Redis caching")
        assert "postgres" in reqs
        assert "redis" in reqs

    def test_extract_concept_patterns(self):
        from honeynet_framework.prompt_fit import extract_prompt_requirements
        reqs = extract_prompt_requirements("Build an SSO portal with DNS and SMTP email")
        assert "sso" in reqs
        assert "dns" in reqs
        assert "smtp" in reqs
        assert "portal" in reqs

    def test_extract_empty_prompt(self):
        from honeynet_framework.prompt_fit import extract_prompt_requirements
        reqs = extract_prompt_requirements("")
        assert reqs == []

    def test_extract_zone_concepts(self):
        from honeynet_framework.prompt_fit import extract_zone_concepts
        zones = extract_zone_concepts("A DMZ with public-facing web and internal database")
        assert "dmz" in zones
        assert "public" in zones
        assert "internal" in zones

    def test_extract_zone_compute_storage(self):
        from honeynet_framework.prompt_fit import extract_zone_concepts
        zones = extract_zone_concepts("GPU compute grid with petabyte dataset storage")
        assert "compute" in zones
        assert "storage" in zones

    def test_compute_prompt_fit_perfect_match(self):
        from honeynet_framework.prompt_fit import compute_prompt_fit
        wm = _make_world_model(
            systems={
                "postgres_db": ("postgres:16", "internal", "database"),
                "redis_cache": ("redis:7", "internal", "database"),
            },
            zones=["internal"],
        )
        result = compute_prompt_fit("PostgreSQL and Redis setup", wm)
        assert result.service_recall == 1.0
        assert "postgres" in result.matched
        assert "redis" in result.matched
        assert result.unmatched == []

    def test_compute_prompt_fit_partial_match(self):
        from honeynet_framework.prompt_fit import compute_prompt_fit
        wm = _make_world_model(
            systems={"web_server": ("nginx:latest", "dmz", "web")},
            zones=["dmz"],
        )
        result = compute_prompt_fit("Nginx with Redis and Elasticsearch", wm)
        assert result.service_recall < 1.0
        assert "nginx" in result.matched
        assert "redis" in result.unmatched
        assert "elasticsearch" in result.unmatched

    def test_compute_prompt_fit_zone_recall(self):
        from honeynet_framework.prompt_fit import compute_prompt_fit
        wm = _make_world_model(
            systems={"web": ("nginx:latest", "dmz", "web")},
            zones=["dmz"],
        )
        result = compute_prompt_fit("DMZ with internal services", wm)
        assert "dmz" in result.zone_matched
        assert "internal" in result.zone_unmatched
        assert result.zone_recall == 0.5

    def test_prompt_fit_no_requirements_returns_1(self):
        from honeynet_framework.prompt_fit import compute_prompt_fit
        wm = _make_world_model(
            systems={"x": ("custom:1", "z", "web")},
            zones=["z"],
        )
        result = compute_prompt_fit("Something completely generic", wm)
        assert result.service_recall == 1.0  # no requirements → vacuously satisfied

    def test_service_recall_matches_via_image_name(self):
        from honeynet_framework.prompt_fit import compute_prompt_fit
        wm = _make_world_model(
            systems={"metadata_store": ("bitnami/elasticsearch:8", "data", "database")},
            zones=["data"],
        )
        result = compute_prompt_fit("Elasticsearch index", wm)
        assert "elasticsearch" in result.matched


# ===========================================================================
# loop_guard.py
# ===========================================================================

class TestLoopGuard:
    def _make_ctx(self, name, image):
        from honeynet_framework.repair_types import FailureContext
        return FailureContext(system_name=name, image=image, command=None, zone="z", role="r")

    def _make_prop(self, name, orig, proposed):
        from honeynet_framework.repair_types import RepairProposal
        return RepairProposal(system_name=name, original_image=orig, proposed_image=proposed, proposed_command=None)

    def test_no_loop_on_first_attempt(self):
        from honeynet_framework.loop_guard import LoopGuard
        guard = LoopGuard(mode="hybrid")
        guard.record_contexts([self._make_ctx("web", "nginx:bad")])
        prop = self._make_prop("web", "nginx:bad", "nginx:good")
        detected, reason = guard.check([prop], attempt=0)
        assert not detected

    def test_detect_no_change(self):
        from honeynet_framework.loop_guard import LoopGuard
        guard = LoopGuard(mode="no_change")
        guard.record_contexts([self._make_ctx("web", "nginx:bad")])
        # LLM proposes same image that just failed
        prop = self._make_prop("web", "nginx:bad", "nginx:bad")
        detected, reason = guard.check([prop], attempt=1)
        assert detected
        assert "no-change" in reason

    def test_detect_image_repeat(self):
        from honeynet_framework.loop_guard import LoopGuard
        guard = LoopGuard(mode="image_repeat")
        # Attempt 0: nginx:1 failed
        guard.record_contexts([self._make_ctx("web", "nginx:1")])
        prop_1 = self._make_prop("web", "nginx:1", "nginx:2")
        guard.check([prop_1], attempt=0)
        guard.record_proposals([prop_1])
        # Attempt 1: nginx:2 failed
        guard.record_contexts([self._make_ctx("web", "nginx:2")])
        # LLM proposes nginx:1 again (oscillation)
        prop_2 = self._make_prop("web", "nginx:2", "nginx:1")
        detected, reason = guard.check([prop_2], attempt=1)
        assert detected
        assert "previously-tried" in reason

    def test_detect_exhausted_system(self):
        from honeynet_framework.loop_guard import LoopGuard
        guard = LoopGuard(mode="hybrid", max_failures_per_system=2)
        guard.record_contexts([self._make_ctx("web", "a")])
        guard.record_contexts([self._make_ctx("web", "b")])
        prop = self._make_prop("web", "b", "c")
        detected, reason = guard.check([prop], attempt=1)
        assert detected
        assert "exhausted" in reason

    def test_hybrid_catches_both(self):
        from honeynet_framework.loop_guard import LoopGuard
        guard = LoopGuard(mode="hybrid")
        guard.record_contexts([self._make_ctx("web", "nginx:1")])
        prop = self._make_prop("web", "nginx:1", "nginx:1")
        detected, _ = guard.check([prop], attempt=1)
        assert detected  # no-change in hybrid mode

    def test_invalid_mode_raises(self):
        from honeynet_framework.loop_guard import LoopGuard
        with pytest.raises(ValueError, match="Invalid LoopGuard mode"):
            LoopGuard(mode="invalid")

    def test_reset_clears_history(self):
        from honeynet_framework.loop_guard import LoopGuard
        guard = LoopGuard(mode="hybrid")
        guard.record_contexts([self._make_ctx("web", "a")])
        guard.record_proposals([self._make_prop("web", "a", "b")])
        guard.reset()
        assert guard.summary() == {"failed": {}, "proposed": {}}

    def test_min_attempts_respected(self):
        from honeynet_framework.loop_guard import LoopGuard
        guard = LoopGuard(mode="no_change", min_attempts=3)
        guard.record_contexts([self._make_ctx("web", "a")])
        prop = self._make_prop("web", "a", "a")
        detected, _ = guard.check([prop], attempt=2)
        assert not detected  # attempt < min_attempts


# ===========================================================================
# repair_scorer.py
# ===========================================================================

class TestRepairScorer:
    def _make_ctx(self, name, image, failure_type="image_not_found"):
        from honeynet_framework.repair_types import FailureContext, FailureType
        return FailureContext(
            system_name=name, image=image, command=None, zone="z", role="r",
            failure_type=FailureType(failure_type),
        )

    def _make_prop(self, name, orig, proposed):
        from honeynet_framework.repair_types import RepairProposal
        return RepairProposal(system_name=name, original_image=orig, proposed_image=proposed, proposed_command=None)

    def test_identical_image_scores_low(self):
        from honeynet_framework.repair_scorer import score_proposal
        ctx = self._make_ctx("web", "nginx:bad")
        score, reason = score_proposal(ctx, "nginx:bad", None)
        assert score <= 0.15
        assert "identical" in reason.lower()

    def test_different_image_scores_higher(self):
        from honeynet_framework.repair_scorer import score_proposal
        ctx = self._make_ctx("web", "nginx:bad")
        score_same, _ = score_proposal(ctx, "nginx:bad", None)
        score_diff, _ = score_proposal(ctx, "nginx:1.25-alpine", None)
        assert score_diff > score_same

    def test_same_base_different_tag_bonus(self):
        from honeynet_framework.repair_scorer import score_proposal
        ctx = self._make_ctx("db", "postgres:99-nonexistent")
        score, reason = score_proposal(ctx, "postgres:16-alpine", None)
        assert "same image name" in reason

    def test_standin_penalty(self):
        from honeynet_framework.repair_scorer import score_proposal
        from honeynet_framework.repair_types import FailureType
        ctx = self._make_ctx("scheduler", "custom-scheduler:1.0", "bad_command")
        score, reason = score_proposal(ctx, "nginx:latest", None)
        assert "stand-in" in reason

    def test_loop_detected_scores_very_low(self):
        from honeynet_framework.repair_scorer import score_proposal
        ctx = self._make_ctx("web", "nginx:1", "loop_detected")
        score, _ = score_proposal(ctx, "nginx:2", None)
        assert score <= 0.15

    def test_score_proposals_batch(self):
        from honeynet_framework.repair_scorer import score_proposals
        contexts = [self._make_ctx("web", "nginx:bad")]
        proposals = [self._make_prop("web", "nginx:bad", "nginx:1.25")]
        result = score_proposals(contexts, proposals)
        assert len(result) == 1
        assert result[0].score is not None
        assert result[0].score_reason is not None

    def test_unmatched_proposal_gets_neutral_score(self):
        from honeynet_framework.repair_scorer import score_proposals
        proposals = [self._make_prop("orphan", "x:1", "x:2")]
        result = score_proposals([], proposals)
        assert result[0].score == 0.4
        assert "no failure context" in result[0].score_reason


# ===========================================================================
# consistency_fixer.py
# ===========================================================================

class TestConsistencyFixer:
    def test_auto_creates_missing_zone(self):
        from honeynet_framework.consistency_fixer import fix_zone_consistency
        wm = _make_world_model(
            systems={"web": ("nginx:latest", "missing_zone", "web")},
            zones=["existing"],
        )
        fixes = fix_zone_consistency(wm)
        assert len(fixes) == 1
        assert "missing_zone" in wm.zones
        assert "Auto-created" in fixes[0]

    def test_existing_zone_not_touched(self):
        from honeynet_framework.consistency_fixer import fix_zone_consistency
        wm = _make_world_model(
            systems={"web": ("nginx:latest", "existing", "web")},
            zones=["existing"],
        )
        fixes = fix_zone_consistency(wm)
        assert fixes == []

    def test_subnet_collision_avoided(self):
        from honeynet_framework.consistency_fixer import fix_zone_consistency
        wm = _make_world_model(
            systems={
                "a": ("nginx:latest", "zone_a", "web"),
                "b": ("nginx:latest", "zone_b", "web"),
            },
            zones=[],
        )
        fix_zone_consistency(wm)
        subnets = [z.deploy.subnet for z in wm.zones.values()]
        assert len(set(subnets)) == len(subnets), "Subnets should be unique"

    def test_log_secret_issues_detects_passwords(self):
        from honeynet_framework.consistency_fixer import log_secret_issues
        wm = WorldModel()
        wm.zones["z"] = WorldZone(name="z", deploy=ZoneDeploy(network_name="z", subnet="172.30.1.0/24"))
        wm.systems["db"] = System(
            name="db",
            kind=SystemKind.DATABASE,
            deploy=SystemDeploy(
                image="postgres:16", zone="z", ports=[5432],
                env=["POSTGRES_PASSWORD=hunter2", "POSTGRES_DB=mydb"],
            ),
        )
        warnings = log_secret_issues(wm)
        assert len(warnings) == 1
        assert "POSTGRES_PASSWORD" in warnings[0]

    def test_secret_template_ref_not_flagged(self):
        from honeynet_framework.consistency_fixer import log_secret_issues
        wm = WorldModel()
        wm.zones["z"] = WorldZone(name="z", deploy=ZoneDeploy(network_name="z", subnet="172.30.1.0/24"))
        wm.systems["db"] = System(
            name="db",
            kind=SystemKind.DATABASE,
            deploy=SystemDeploy(
                image="postgres:16", zone="z", ports=[5432],
                env=["POSTGRES_PASSWORD=${DB_SECRET}", "PORT=5432"],
            ),
        )
        warnings = log_secret_issues(wm)
        assert warnings == []

    def test_scrub_env_for_artifact(self):
        from honeynet_framework.consistency_fixer import scrub_env_for_artifact
        env = ["POSTGRES_PASSWORD=hunter2", "PORT=5432", "API_KEY=abc123"]
        scrubbed = scrub_env_for_artifact(env)
        assert scrubbed[0] == "POSTGRES_PASSWORD=[REDACTED]"
        assert scrubbed[1] == "PORT=5432"
        assert scrubbed[2] == "API_KEY=[REDACTED]"


# ===========================================================================
# precompile_validator.py
# ===========================================================================

class TestPrecompileValidator:
    def test_empty_systems_is_error(self):
        from honeynet_framework.precompile_validator import validate
        from honeynet_framework.repair_types import IssueSeverity
        wm = WorldModel()
        issues = validate(wm)
        errors = [i for i in issues if i.severity == IssueSeverity.ERROR]
        assert len(errors) == 1
        assert "no deployable systems" in errors[0].message

    def test_missing_image_is_error(self):
        from honeynet_framework.precompile_validator import validate
        from honeynet_framework.repair_types import IssueSeverity
        wm = _make_world_model(zones=["z"])
        wm.systems["web"] = System(
            name="web",
            kind=SystemKind.WEB,
            deploy=SystemDeploy(image="", zone="z", ports=[80]),
        )
        issues = validate(wm)
        errors = [i for i in issues if i.severity == IssueSeverity.ERROR]
        assert any("no Docker image" in e.message for e in errors)

    def test_missing_zone_is_error(self):
        from honeynet_framework.precompile_validator import validate
        from honeynet_framework.repair_types import IssueSeverity
        wm = _make_world_model(zones=["z"])
        wm.systems["web"] = System(
            name="web",
            kind=SystemKind.WEB,
            deploy=SystemDeploy(image="nginx:latest", zone="", ports=[80]),
        )
        issues = validate(wm)
        errors = [i for i in issues if i.severity == IssueSeverity.ERROR]
        assert any("no zone assigned" in e.message for e in errors)

    def test_dangling_depends_on_is_error(self):
        from honeynet_framework.precompile_validator import validate
        from honeynet_framework.repair_types import IssueSeverity
        wm = _make_world_model(
            systems={"web": ("nginx:latest", "z", "web")},
            zones=["z"],
        )
        wm.systems["web"].deploy.depends_on = ["nonexistent_db"]
        issues = validate(wm)
        errors = [i for i in issues if i.severity == IssueSeverity.ERROR]
        assert any("nonexistent_db" in e.message for e in errors)

    def test_unknown_zone_ref_is_error(self):
        from honeynet_framework.precompile_validator import validate
        from honeynet_framework.repair_types import IssueSeverity
        wm = _make_world_model(zones=["internal"])
        wm.systems["web"] = System(
            name="web",
            kind=SystemKind.WEB,
            deploy=SystemDeploy(image="nginx:latest", zone="dmz", ports=[80]),
        )
        issues = validate(wm)
        errors = [i for i in issues if i.severity == IssueSeverity.ERROR]
        assert any("dmz" in e.message for e in errors)

    def test_valid_model_has_no_issues(self):
        from honeynet_framework.precompile_validator import validate
        wm = _make_world_model(
            systems={"web": ("nginx:latest", "internal", "web")},
            zones=["internal"],
        )
        issues = validate(wm)
        assert issues == []


# ===========================================================================
# scenario_fit_formal.py
# ===========================================================================

class TestScenarioFitFormal:
    def test_none_scenario_returns_vacuous(self):
        from honeynet_framework.scenario_fit_formal import compute_formal_scenario_fit
        wm = _make_world_model()
        result = compute_formal_scenario_fit(wm, None)
        assert result["vacuous_reference"] is True
        assert result["benchmark_pass"] is False
        assert result["reference_status"] == "not_loaded"

    def test_empty_reference_returns_empty_status(self):
        from honeynet_framework.scenario_fit_formal import compute_formal_scenario_fit
        wm = _make_world_model()
        result = compute_formal_scenario_fit(wm, {"id": "test", "reference": {}})
        assert result["reference_status"] == "empty"
        assert result["benchmark_pass"] is False

    def test_service_recall_computed_correctly(self):
        from honeynet_framework.scenario_fit_formal import compute_formal_scenario_fit
        wm = _make_world_model(
            systems={
                "postgres_db": ("postgres:16", "internal", "database"),
                "nginx_web": ("nginx:latest", "dmz", "web"),
            },
            zones=["internal", "dmz"],
        )
        scenario = {
            "id": "test",
            "reference": {
                "required_services": [
                    {"id": "db", "archetypes_any": ["postgres"]},
                    {"id": "web", "archetypes_any": ["nginx"]},
                    {"id": "cache", "archetypes_any": ["redis"]},
                ],
            },
        }
        result = compute_formal_scenario_fit(wm, scenario)
        # 2 of 3 services matched → ~0.67
        assert result["planned_service_coverage"] == pytest.approx(2 / 3, abs=0.01)
        assert result["benchmark_pass"] is False  # not all services found

    def test_perfect_fit_passes(self):
        from honeynet_framework.scenario_fit_formal import compute_formal_scenario_fit
        wm = _make_world_model(
            systems={
                "postgres_db": ("postgres:16", "internal", "database"),
                "nginx_web": ("nginx:latest", "dmz", "web"),
            },
            zones=["internal", "dmz"],
        )
        wm.systems["nginx_web"].deploy.depends_on = ["postgres_db"]
        scenario = {
            "id": "test",
            "reference": {
                "required_services": [
                    {"id": "db", "archetypes_any": ["postgres"]},
                    {"id": "web", "archetypes_any": ["nginx"]},
                ],
                "required_zones": [
                    {"id": "internal", "names_any": ["internal"]},
                    {"id": "dmz", "names_any": ["dmz"]},
                ],
                "required_dependencies": [
                    {"source_service_id": "web", "target_service_id": "db"},
                ],
            },
        }
        result = compute_formal_scenario_fit(wm, scenario)
        assert result["planned_service_coverage"] == 1.0
        assert result["planned_zone_coverage"] == 1.0
        assert result["planned_dep_coverage"] == 1.0
        assert result["placement_violations"] == 0
        assert result["benchmark_pass"] is True

    def test_forbidden_placement_detected(self):
        from honeynet_framework.scenario_fit_formal import compute_formal_scenario_fit
        wm = _make_world_model(
            systems={"db": ("postgres:16", "dmz", "database")},
            zones=["dmz"],
        )
        scenario = {
            "id": "test",
            "reference": {
                "required_services": [{"id": "db", "archetypes_any": ["postgres"]}],
                "required_zones": [{"id": "dmz", "names_any": ["dmz"]}],
                "forbidden_placements": [
                    {"service_id": "db", "zone_id": "dmz"},
                ],
            },
        }
        result = compute_formal_scenario_fit(wm, scenario)
        assert result["placement_violations"] == 1
        assert result["benchmark_pass"] is False

    def test_string_requirement_coerced(self):
        from honeynet_framework.scenario_fit_formal import compute_formal_scenario_fit
        wm = _make_world_model(
            systems={"nginx_web": ("nginx:latest", "z", "web")},
            zones=["z"],
        )
        scenario = {
            "id": "test",
            "reference": {"required_services": ["nginx"]},
        }
        result = compute_formal_scenario_fit(wm, scenario)
        # String "nginx" coerced to {"id": "nginx"} — no archetypes_any so it
        # matches by system name containing "nginx"
        assert result["planned_service_coverage"] == 1.0


# ===========================================================================
# New metrics fields
# ===========================================================================

class TestNewMetricsFields:
    def test_dropped_containers_in_output(self):
        from honeynet_framework.metrics import DeploymentMetrics
        m = DeploymentMetrics(
            expected_containers=27,
            planned_containers=29,
            dropped_containers=2,
            running_containers=27,
        )
        d = m.to_dict()
        dep = d["deployability"]
        assert dep["planned_containers"] == 29
        assert dep["dropped_containers"] == 2
        assert dep["expected_containers"] == 27

    def test_running_coverage_in_scenario_fit(self):
        from honeynet_framework.metrics import DeploymentMetrics
        m = DeploymentMetrics(
            planned_service_coverage=0.9,
            running_service_coverage=0.8,
            planned_zone_coverage=1.0,
            running_zone_coverage=0.75,
            running_dep_coverage=0.9,
        )
        d = m.to_dict()
        sf = d["scenario_fit"]
        assert sf["planned_service_coverage"] == 0.9
        assert sf["running_service_coverage"] == 0.8
        assert sf["planned_zone_coverage"] == 1.0
        assert sf["running_zone_coverage"] == 0.75
        assert sf["running_dep_coverage"] == 0.9

    def test_zero_defaults(self):
        from honeynet_framework.metrics import DeploymentMetrics
        m = DeploymentMetrics()
        d = m.to_dict()
        assert d["deployability"]["planned_containers"] == 0
        assert d["deployability"]["dropped_containers"] == 0
        assert d["scenario_fit"]["running_service_coverage"] == 0.0
        assert d["scenario_fit"]["running_zone_coverage"] == 0.0
        assert d["scenario_fit"]["running_dep_coverage"] == 0.0


# ===========================================================================
# Benchmark runner
# ===========================================================================

class TestBenchmarkRunner:
    def test_load_manifest(self):
        from honeynet_framework.benchmark_runner import load_manifest
        manifest = load_manifest(Path("benchmarks/corpus_v1"))
        assert "scenarios" in manifest
        assert len(manifest["scenarios"]) == 30

    def test_filter_by_difficulty(self):
        from honeynet_framework.benchmark_runner import load_manifest, filter_scenarios
        manifest = load_manifest(Path("benchmarks/corpus_v1"))
        easy = filter_scenarios(manifest, difficulty="easy")
        assert len(easy) == 10
        assert all(s["difficulty"] == "easy" for s in easy)

    def test_filter_by_scenario_id(self):
        from honeynet_framework.benchmark_runner import load_manifest, filter_scenarios
        manifest = load_manifest(Path("benchmarks/corpus_v1"))
        result = filter_scenarios(manifest, scenario_id="e01_web_db")
        assert len(result) == 1
        assert result[0]["id"] == "e01_web_db"

    def test_load_scenario(self):
        from honeynet_framework.benchmark_runner import load_manifest, load_scenario
        manifest = load_manifest(Path("benchmarks/corpus_v1"))
        entry = manifest["scenarios"][0]
        scenario = load_scenario(Path("benchmarks/corpus_v1"), entry)
        assert scenario["id"] == "e01_web_db"
        assert "prompts" in scenario
        assert "reference" in scenario

    def test_get_prompt_concise(self):
        from honeynet_framework.benchmark_runner import load_manifest, load_scenario, get_prompt
        manifest = load_manifest(Path("benchmarks/corpus_v1"))
        scenario = load_scenario(Path("benchmarks/corpus_v1"), manifest["scenarios"][0])
        prompt = get_prompt(scenario, "concise")
        assert "honeynet" in prompt.lower() or "web" in prompt.lower()

    def test_get_prompt_fallback(self):
        from honeynet_framework.benchmark_runner import get_prompt
        scenario = {"prompts": [{"id": "narrative", "text": "Fallback prompt"}]}
        prompt = get_prompt(scenario, "nonexistent")
        assert prompt == "Fallback prompt"

    def test_validate_scenario_yaml_valid(self):
        from honeynet_framework.benchmark_runner import load_manifest, load_scenario, validate_scenario_yaml
        manifest = load_manifest(Path("benchmarks/corpus_v1"))
        scenario = load_scenario(Path("benchmarks/corpus_v1"), manifest["scenarios"][0])
        issues = validate_scenario_yaml(scenario)
        assert issues == []

    def test_validate_all_corpus_scenarios(self):
        """Every scenario in corpus_v1 should be structurally valid."""
        from honeynet_framework.benchmark_runner import load_manifest, load_scenario, validate_scenario_yaml
        manifest = load_manifest(Path("benchmarks/corpus_v1"))
        for entry in manifest["scenarios"]:
            scenario = load_scenario(Path("benchmarks/corpus_v1"), entry)
            issues = validate_scenario_yaml(scenario)
            assert issues == [], f"Scenario {entry['id']} has issues: {issues}"

    def test_aggregate_empty(self):
        from honeynet_framework.benchmark_runner import _aggregate_results
        report = _aggregate_results([])
        assert report.total_scenarios == 0
        assert report.mean_planned_service_coverage == 0.0

    def test_aggregate_results(self):
        from honeynet_framework.benchmark_runner import BenchmarkResult, _aggregate_results
        results = [
            BenchmarkResult(scenario_id="a", difficulty="easy", prompt_variant="concise",
                            planned_service_coverage=1.0, planned_zone_coverage=1.0, deploy_success=True, benchmark_pass=True),
            BenchmarkResult(scenario_id="b", difficulty="easy", prompt_variant="concise",
                            planned_service_coverage=0.5, planned_zone_coverage=0.5, deploy_success=False, benchmark_pass=False),
        ]
        report = _aggregate_results(results)
        assert report.total_scenarios == 2
        assert report.scenarios_passed == 1
        assert report.mean_planned_service_coverage == 0.75
        assert report.deploy_success_rate == 0.5

    def test_report_generation(self, tmp_path):
        from honeynet_framework.benchmark_runner import BenchmarkResult, CorpusReport, generate_benchmark_report
        results = [
            BenchmarkResult(scenario_id="test", difficulty="easy", prompt_variant="concise",
                            planned_service_coverage=0.8, deploy_success=True, benchmark_pass=True),
        ]
        report = CorpusReport(
            total_scenarios=1, scenarios_passed=1, results=results,
            mean_planned_service_coverage=0.8, deploy_success_rate=1.0, benchmark_pass_rate=1.0,
        )
        path = generate_benchmark_report(report, tmp_path)
        assert path.exists()
        html = path.read_text(encoding="utf-8")
        assert "test" in html
        assert "80%" in html
        # JSON should also exist
        assert (tmp_path / "benchmark_report.json").exists()


# ===========================================================================
# Word-boundary matching fix
# ===========================================================================

class TestWordBoundaryMatching:
    def test_go_does_not_match_mongo(self):
        from honeynet_framework.scenario_fit_formal import _matches_any
        assert not _matches_any("mongodb", ["go"])

    def test_postgres_matches_postgresql(self):
        from honeynet_framework.scenario_fit_formal import _matches_any
        assert _matches_any("postgresql_db", ["postgres"])

    def test_redis_matches_redis(self):
        from honeynet_framework.scenario_fit_formal import _matches_any
        assert _matches_any("redis_cache", ["redis"])

    def test_sql_matches_mysql(self):
        from honeynet_framework.scenario_fit_formal import _matches_any
        # "sql" should match at a word boundary in "mysql" — "my" + "sql"
        # This depends on whether we want this. Currently \b fires before "sql" in "mysql"
        # because "y" and "s" are both word chars — no boundary there.
        # This is actually the CORRECT behavior: "sql" should NOT match "mysql"
        assert not _matches_any("mysql", ["sql"])

    def test_empty_patterns_matches_everything(self):
        from honeynet_framework.scenario_fit_formal import _matches_any
        assert _matches_any("anything", [])
        assert _matches_any("anything", None)

    def test_kafka_matches_kafka(self):
        from honeynet_framework.scenario_fit_formal import _matches_any
        assert _matches_any("bitnami/kafka:3.6", ["kafka"])


# ===========================================================================
# Deployed scenario fit
# ===========================================================================

class TestDeployedScenarioFit:
    def test_deployed_fit_filters_to_running(self):
        from honeynet_framework.scenario_fit_formal import compute_deployed_scenario_fit
        wm = _make_world_model(
            systems={
                "web": ("nginx:latest", "dmz", "web"),
                "db": ("postgres:16", "internal", "database"),
            },
            zones=["dmz", "internal"],
        )
        scenario = {
            "id": "test",
            "reference": {
                "required_services": [
                    {"id": "web", "archetypes_any": ["nginx"]},
                    {"id": "db", "archetypes_any": ["postgres"]},
                ],
            },
        }
        # Only web is running, db crashed
        result = compute_deployed_scenario_fit(
            wm, scenario, running_containers={"hn_web"}, container_prefix="hn_",
        )
        assert result["planned_service_coverage"] == 0.5  # 1 of 2
        assert result["evaluation_scope"] == "deployed"
        assert result["running_system_count"] == 1

    def test_deployed_fit_all_running(self):
        from honeynet_framework.scenario_fit_formal import compute_deployed_scenario_fit
        wm = _make_world_model(
            systems={"web": ("nginx:latest", "z", "web")},
            zones=["z"],
        )
        scenario = {
            "id": "test",
            "reference": {"required_services": [{"id": "w", "archetypes_any": ["nginx"]}]},
        }
        result = compute_deployed_scenario_fit(
            wm, scenario, running_containers={"hn_web"}, container_prefix="hn_",
        )
        assert result["planned_service_coverage"] == 1.0

    def test_deployed_fit_none_scenario(self):
        from honeynet_framework.scenario_fit_formal import compute_deployed_scenario_fit
        wm = _make_world_model()
        result = compute_deployed_scenario_fit(wm, None, running_containers=set())
        assert result["vacuous_reference"] is True
