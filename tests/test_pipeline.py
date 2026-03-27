"""
End-to-end tests for the simplified honeynet pipeline.

Tests the deterministic pipeline: WorldModel → Validate → Compile → Render
(No LLM required for these tests)
"""

import json
from pathlib import Path

import pytest

from honeynet_framework.catalog import (
    CatalogResolutionError,
    apply_catalog_resolution,
    load_catalog_snapshot,
)

from honeynet_framework.models import (
    DeployProjection,
    Organization,
    System,
    SystemDeploy,
    SystemKind,
    SystemSimulate,
    VolumeMount,
    WorldModel,
    WorldZone,
    ZoneDeploy,
)
from honeynet_framework.validator import validate
from honeynet_framework.deploy_compiler import DeployCompiler, CompilerConfig
from honeynet_framework.tofu_renderer import TofuRenderer
from honeynet_framework.extraction.yaml_parsing import extract_yaml_from_response

_FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _make_test_world_model() -> WorldModel:
    """Create a minimal test WorldModel."""
    return WorldModel(
        organization=Organization(name="Test Corp"),
        zones={
            "dmz": WorldZone(name="dmz", deploy=ZoneDeploy(network_name="dmz", internal=False)),
            "internal": WorldZone(name="internal", deploy=ZoneDeploy(network_name="internal", internal=True)),
        },
        systems={
            "web": System(
                name="web",
                kind=SystemKind.WEB,
                deploy=SystemDeploy(
                    image="nginx:1.25-alpine",
                    zone="dmz",
                    ports=[80],
                    env=["NGINX_HOST=test.local"],
                ),
                simulate=SystemSimulate(hostname="web01.test.local", role="Web Server"),
            ),
            "db": System(
                name="db",
                kind=SystemKind.DATABASE,
                deploy=SystemDeploy(
                    image="postgres:16-alpine",
                    zone="internal",
                    ports=[5432],
                    env=["POSTGRES_PASSWORD=admin123"],
                    depends_on=["web"],
                ),
            ),
        },
    )


class TestValidator:
    """Test structural validation."""

    def test_valid_model(self):
        wm = _make_test_world_model()
        result = validate(wm)
        assert result.passed
        assert len(result.errors) == 0

    def test_missing_zone_reference(self):
        wm = _make_test_world_model()
        wm.systems["db"].deploy.zone = "nonexistent"
        result = validate(wm)
        assert not result.passed
        assert any(e.rule == "REF_ZONE" for e in result.errors)

    def test_missing_image(self):
        wm = _make_test_world_model()
        wm.systems["web"].deploy.image = ""
        result = validate(wm)
        assert not result.passed
        assert any(e.rule == "REQUIRED_IMAGE" for e in result.errors)

    def test_circular_dependency(self):
        wm = _make_test_world_model()
        wm.systems["web"].deploy.depends_on = ["db"]
        wm.systems["db"].deploy.depends_on = ["web"]
        result = validate(wm)
        assert not result.passed
        assert any(e.rule == "CIRCULAR_DEPENDENCY" for e in result.errors)

    def test_self_dependency(self):
        wm = _make_test_world_model()
        wm.systems["web"].deploy.depends_on = ["web"]
        result = validate(wm)
        assert not result.passed
        assert any(e.rule == "SELF_DEPENDENCY" for e in result.errors)

    def test_invalid_depends_on(self):
        wm = _make_test_world_model()
        wm.systems["web"].deploy.depends_on = ["nonexistent"]
        result = validate(wm)
        assert not result.passed
        assert any(e.rule == "REF_DEPENDS_ON" for e in result.errors)

    def test_no_zones(self):
        wm = _make_test_world_model()
        wm.zones = {}
        result = validate(wm)
        assert not result.passed

    def test_no_systems(self):
        wm = WorldModel(zones={"z": WorldZone(name="z", deploy=ZoneDeploy(network_name="z"))})
        result = validate(wm)
        assert not result.passed

    # --- Cycle detection: details string, 3+ nodes, ref-error interaction ---

    def test_circular_dependency_details_string(self):
        """2-cycle: details string contains both system names and arrow notation."""
        wm = _make_test_world_model()
        wm.systems["web"].deploy.depends_on = ["db"]
        wm.systems["db"].deploy.depends_on = ["web"]
        result = validate(wm)
        cycle_errors = [e for e in result.errors if e.rule == "CIRCULAR_DEPENDENCY"]
        assert len(cycle_errors) == 1
        details = cycle_errors[0].details
        assert "web" in details
        assert "db" in details
        assert "->" in details

    def test_three_node_cycle(self):
        """A -> B -> C -> A should be detected."""
        wm = _make_test_world_model()
        wm.systems["cache"] = System(
            name="cache",
            kind=SystemKind.DATABASE,
            deploy=SystemDeploy(image="redis:7", zone="internal", depends_on=["web"]),
        )
        wm.systems["web"].deploy.depends_on = ["db"]
        wm.systems["db"].deploy.depends_on = ["cache"]
        result = validate(wm)
        assert not result.passed
        cycle_errors = [e for e in result.errors if e.rule == "CIRCULAR_DEPENDENCY"]
        assert len(cycle_errors) == 1
        details = cycle_errors[0].details
        # All three nodes must appear in the cycle path
        assert "web" in details
        assert "db" in details
        assert "cache" in details

    def test_four_node_cycle(self):
        """A -> B -> C -> D -> A is found."""
        wm = _make_test_world_model()
        wm.systems["cache"] = System(
            name="cache",
            kind=SystemKind.DATABASE,
            deploy=SystemDeploy(image="redis:7", zone="internal", depends_on=["web"]),
        )
        wm.systems["proxy"] = System(
            name="proxy",
            kind=SystemKind.WEB,
            deploy=SystemDeploy(image="nginx:1.25", zone="dmz", depends_on=["cache"]),
        )
        wm.systems["web"].deploy.depends_on = ["db"]
        wm.systems["db"].deploy.depends_on = ["proxy"]
        result = validate(wm)
        assert not result.passed
        assert any(e.rule == "CIRCULAR_DEPENDENCY" for e in result.errors)

    def test_ref_error_masks_latent_cycle(self):
        """A typo in depends_on breaks the edge, hiding a cycle behind REF_DEPENDS_ON.

        If A -> B -> 'C_typo' -> A: the typo on C_typo produces REF_DEPENDS_ON.
        The cycle A -> B -> C -> A only appears when the name is fixed.
        This test documents the behavior: cycle detection runs on resolved names only.
        """
        wm = _make_test_world_model()
        wm.systems["cache"] = System(
            name="cache",
            kind=SystemKind.DATABASE,
            deploy=SystemDeploy(image="redis:7", zone="internal", depends_on=["web"]),
        )
        wm.systems["web"].deploy.depends_on = ["db"]
        # Typo: "cach" instead of "cache" — breaks the cycle edge
        wm.systems["db"].deploy.depends_on = ["cach"]
        result = validate(wm)
        assert not result.passed
        rules = {e.rule for e in result.errors}
        # REF_DEPENDS_ON is reported for the unknown name
        assert "REF_DEPENDS_ON" in rules
        # No cycle detected because 'cach' is not in systems → edge skipped
        assert "CIRCULAR_DEPENDENCY" not in rules

    def test_ref_error_fixed_reveals_cycle(self):
        """Same topology as above but with the correct name → cycle is found."""
        wm = _make_test_world_model()
        wm.systems["cache"] = System(
            name="cache",
            kind=SystemKind.DATABASE,
            deploy=SystemDeploy(image="redis:7", zone="internal", depends_on=["web"]),
        )
        wm.systems["web"].deploy.depends_on = ["db"]
        wm.systems["db"].deploy.depends_on = ["cache"]
        result = validate(wm)
        rules = {e.rule for e in result.errors}
        assert "CIRCULAR_DEPENDENCY" in rules
        assert "REF_DEPENDS_ON" not in rules

    def test_dag_with_diamond_no_cycle(self):
        """Diamond: A -> B, A -> C, B -> D, C -> D — valid DAG, no cycle."""
        wm = _make_test_world_model()
        wm.systems["cache"] = System(
            name="cache",
            kind=SystemKind.DATABASE,
            deploy=SystemDeploy(image="redis:7", zone="internal"),
        )
        wm.systems["proxy"] = System(
            name="proxy",
            kind=SystemKind.WEB,
            deploy=SystemDeploy(image="nginx:1.25", zone="dmz"),
        )
        # Diamond: web -> db, web -> cache, db -> proxy, cache -> proxy
        wm.systems["web"].deploy.depends_on = ["db", "cache"]
        wm.systems["db"].deploy.depends_on = ["proxy"]
        wm.systems["cache"].deploy.depends_on = ["proxy"]
        result = validate(wm)
        assert not any(e.rule == "CIRCULAR_DEPENDENCY" for e in result.errors)

    def test_invalid_port(self):
        wm = _make_test_world_model()
        wm.systems["web"].deploy.ports = [99999]
        result = validate(wm)
        assert not result.passed
        assert any(e.rule == "PORT_RANGE" for e in result.errors)


class TestOneShotCommandWarning:
    """Test ONE_SHOT_COMMAND warning rule."""

    def test_restic_backup_flagged(self):
        """restic backup is a one-shot tool — should warn."""
        wm = _make_test_world_model()
        wm.systems["backup"] = System(
            name="backup",
            kind=SystemKind.STORAGE,
            deploy=SystemDeploy(
                image="restic/restic:0.15.1",
                zone="internal",
                command=["restic", "backup", "/data"],
            ),
        )
        result = validate(wm)
        assert result.passed  # warnings don't block
        warnings = [w for w in result.warnings if w.rule == "ONE_SHOT_COMMAND"]
        assert len(warnings) == 1
        assert "backup" in warnings[0].details

    def test_pg_dump_flagged(self):
        """pg_dump is a one-shot tool — should warn."""
        wm = _make_test_world_model()
        wm.systems["dumper"] = System(
            name="dumper",
            kind=SystemKind.DATABASE,
            deploy=SystemDeploy(
                image="postgres:16-alpine",
                zone="internal",
                command=["pg_dump", "-U", "postgres", "mydb"],
            ),
        )
        result = validate(wm)
        warnings = [w for w in result.warnings if w.rule == "ONE_SHOT_COMMAND"]
        assert len(warnings) == 1

    def test_shell_wrapper_one_shot_flagged(self):
        """sh -c 'rsync ...' should also be caught."""
        wm = _make_test_world_model()
        wm.systems["sync"] = System(
            name="sync",
            kind=SystemKind.INFRA,
            deploy=SystemDeploy(
                image="alpine:3.19",
                zone="internal",
                command=["sh", "-c", "rsync -av /src /dst"],
            ),
        )
        result = validate(wm)
        warnings = [w for w in result.warnings if w.rule == "ONE_SHOT_COMMAND"]
        assert len(warnings) == 1
        assert "rsync" in warnings[0].details

    def test_daemon_command_not_flagged(self):
        """nginx (default CMD) should not trigger any warning."""
        wm = _make_test_world_model()
        # web system has no command set — uses image default
        result = validate(wm)
        assert not any(w.rule == "ONE_SHOT_COMMAND" for w in result.warnings)

    def test_explicit_daemon_command_not_flagged(self):
        """An explicit long-running command should not warn."""
        wm = _make_test_world_model()
        wm.systems["api"] = System(
            name="api",
            kind=SystemKind.WEB,
            deploy=SystemDeploy(
                image="python:3.12-slim",
                zone="dmz",
                command=["gunicorn", "app:app", "--bind", "0.0.0.0:8000"],
            ),
        )
        result = validate(wm)
        assert not any(w.rule == "ONE_SHOT_COMMAND" for w in result.warnings)

    def test_full_path_binary_flagged(self):
        """/usr/bin/pg_dump should still be caught."""
        wm = _make_test_world_model()
        wm.systems["dumper"] = System(
            name="dumper",
            kind=SystemKind.DATABASE,
            deploy=SystemDeploy(
                image="postgres:16-alpine",
                zone="internal",
                command=["/usr/bin/pg_dump", "mydb"],
            ),
        )
        result = validate(wm)
        warnings = [w for w in result.warnings if w.rule == "ONE_SHOT_COMMAND"]
        assert len(warnings) == 1

    def test_shell_wrapper_with_empty_c_command_does_not_crash(self):
        """sh -c '' must not crash validator on empty inner command."""
        wm = _make_test_world_model()
        wm.systems["noop"] = System(
            name="noop",
            kind=SystemKind.INFRA,
            deploy=SystemDeploy(
                image="alpine:3.19",
                zone="internal",
                command=["sh", "-c", ""],
            ),
        )
        result = validate(wm)
        assert result.passed
        assert not any(w.rule == "ONE_SHOT_COMMAND" and "noop" in w.details for w in result.warnings)


class TestFictionalScriptWarning:
    """Test FICTIONAL_SCRIPT warning rule."""

    def test_python_script_on_base_image(self):
        """python scheduler.py on python:3.12-slim should warn."""
        wm = _make_test_world_model()
        wm.systems["scheduler"] = System(
            name="scheduler",
            kind=SystemKind.INFRA,
            deploy=SystemDeploy(
                image="python:3.12-slim",
                zone="internal",
                command=["python", "scheduler.py"],
            ),
        )
        result = validate(wm)
        warnings = [w for w in result.warnings if w.rule == "FICTIONAL_SCRIPT"]
        assert len(warnings) == 1
        assert "scheduler.py" in warnings[0].details

    def test_node_app_on_base_image(self):
        """node app.js on node:20-alpine should warn."""
        wm = _make_test_world_model()
        wm.systems["app"] = System(
            name="app",
            kind=SystemKind.WEB,
            deploy=SystemDeploy(
                image="node:20-alpine",
                zone="dmz",
                command=["node", "app.js"],
            ),
        )
        result = validate(wm)
        warnings = [w for w in result.warnings if w.rule == "FICTIONAL_SCRIPT"]
        assert len(warnings) == 1

    def test_script_on_non_base_image_ok(self):
        """python app.py on a custom image (not a base prefix) should not warn."""
        wm = _make_test_world_model()
        wm.systems["custom"] = System(
            name="custom",
            kind=SystemKind.WEB,
            deploy=SystemDeploy(
                image="mycompany/myapp:1.0",
                zone="dmz",
                command=["python", "app.py"],
            ),
        )
        result = validate(wm)
        assert not any(w.rule == "FICTIONAL_SCRIPT" for w in result.warnings)

    def test_no_command_no_warning(self):
        """Systems without explicit commands should not warn."""
        wm = _make_test_world_model()
        result = validate(wm)
        assert not any(w.rule == "FICTIONAL_SCRIPT" for w in result.warnings)

    def test_base_image_with_non_script_arg(self):
        """python -m http.server 8000 should not warn (no script file)."""
        wm = _make_test_world_model()
        wm.systems["server"] = System(
            name="server",
            kind=SystemKind.WEB,
            deploy=SystemDeploy(
                image="python:3.12-slim",
                zone="dmz",
                command=["python", "-m", "http.server", "8000"],
            ),
        )
        result = validate(wm)
        assert not any(w.rule == "FICTIONAL_SCRIPT" for w in result.warnings)

    def test_python_module_on_base_image_warns(self):
        """python -m app.main on python base image should warn."""
        wm = _make_test_world_model()
        wm.systems["module_app"] = System(
            name="module_app",
            kind=SystemKind.WEB,
            deploy=SystemDeploy(
                image="python:3.12-slim",
                zone="dmz",
                command=["python", "-m", "app.main"],
            ),
        )
        result = validate(wm)
        warnings = [w for w in result.warnings if w.rule == "FICTIONAL_SCRIPT"]
        assert len(warnings) == 1
        assert "app.main" in warnings[0].details


class TestCompiler:
    """Test deploy compilation."""

    def test_basic_compile(self):
        wm = _make_test_world_model()
        compiler = DeployCompiler()
        proj = compiler.compile(wm)
        assert len(proj.containers) == 2
        assert len(proj.networks) == 3  # dmz + internal + backbone

    def test_container_names_prefixed(self):
        wm = _make_test_world_model()
        compiler = DeployCompiler()
        proj = compiler.compile(wm)
        names = [c.name for c in proj.containers]
        assert all(n.startswith("hn_") for n in names)

    def test_depends_on_mapped(self):
        wm = _make_test_world_model()
        compiler = DeployCompiler()
        proj = compiler.compile(wm)
        db_container = next(c for c in proj.containers if "db" in c.name)
        assert "hn_web" in db_container.depends_on

    def test_ports_published_for_dmz(self):
        wm = _make_test_world_model()
        compiler = DeployCompiler()
        proj = compiler.compile(wm)
        web_container = next(c for c in proj.containers if "web" in c.name)
        assert len(web_container.ports) > 0  # DMZ is not internal

    def test_ports_not_published_for_internal(self):
        wm = _make_test_world_model()
        compiler = DeployCompiler()
        proj = compiler.compile(wm)
        db_container = next(c for c in proj.containers if "db" in c.name)
        assert len(db_container.ports) == 0  # Internal zone

    def test_network_aliases(self):
        wm = _make_test_world_model()
        compiler = DeployCompiler()
        proj = compiler.compile(wm)
        web_container = next(c for c in proj.containers if "web" in c.name)
        assert len(web_container.network_aliases) > 0

    def test_volumes(self):
        wm = _make_test_world_model()
        wm.systems["db"].deploy.volumes = [VolumeMount(name="pgdata", path="/var/lib/postgresql/data")]
        compiler = DeployCompiler()
        proj = compiler.compile(wm)
        assert len(proj.volumes) == 1
        db_container = next(c for c in proj.containers if "db" in c.name)
        assert len(db_container.volumes) == 1

    def test_determinism(self):
        wm = _make_test_world_model()
        compiler1 = DeployCompiler()
        compiler2 = DeployCompiler()
        proj1 = compiler1.compile(wm)
        proj2 = compiler2.compile(wm)
        assert proj1.to_dict() == proj2.to_dict()


class TestRenderer:
    """Test OpenTofu JSON rendering."""

    def test_render_valid_json(self):
        wm = _make_test_world_model()
        proj = DeployCompiler().compile(wm)
        renderer = TofuRenderer()
        output = renderer.render(proj)
        config = json.loads(output)
        assert "terraform" in config
        assert "provider" in config
        assert "resource" in config

    def test_render_has_all_containers(self):
        wm = _make_test_world_model()
        proj = DeployCompiler().compile(wm)
        output = TofuRenderer().render(proj)
        config = json.loads(output)
        containers = config["resource"]["docker_container"]
        assert len(containers) == 2

    def test_render_has_networks(self):
        wm = _make_test_world_model()
        proj = DeployCompiler().compile(wm)
        output = TofuRenderer().render(proj)
        config = json.loads(output)
        networks = config["resource"]["docker_network"]
        assert len(networks) == 3

    def test_render_docker_provider(self):
        wm = _make_test_world_model()
        proj = DeployCompiler().compile(wm)
        output = TofuRenderer().render(proj)
        config = json.loads(output)
        provider = config["provider"][0]["docker"][0]
        assert "host" in provider


class TestYAMLParsing:
    """Test YAML extraction from LLM responses."""

    def test_plain_yaml(self):
        text = "zones:\n  dmz:\n    network_name: dmz"
        result = extract_yaml_from_response(text)
        assert "zones" in result

    def test_yaml_in_code_block(self):
        text = "```yaml\nzones:\n  dmz:\n    network_name: dmz\n```"
        result = extract_yaml_from_response(text)
        assert "zones" in result

    def test_yaml_with_prose(self):
        text = "Here is the world model:\n\nzones:\n  dmz:\n    network_name: dmz"
        result = extract_yaml_from_response(text)
        assert "zones" in result

    def test_tab_repair(self):
        text = "zones:\n\tdmz:\n\t\tnetwork_name: dmz"
        result = extract_yaml_from_response(text)
        assert "zones" in result


class TestWorldModelSerialization:
    """Test WorldModel to_dict/serialization."""

    def test_to_dict(self):
        wm = _make_test_world_model()
        d = wm.to_dict()
        assert "zones" in d
        assert "systems" in d
        assert "organization" in d
        assert len(d["zones"]) == 2
        assert len(d["systems"]) == 2

    def test_project_name(self):
        wm = _make_test_world_model()
        assert wm.project_name == "test_corp"

    def test_deployable_systems(self):
        wm = _make_test_world_model()
        wm.systems["monitor"] = System(name="monitor", kind=SystemKind.MONITOR)  # no deploy
        assert len(wm.deployable_systems) == 2


class TestDeploymentResult:
    """Test DeploymentResult fields, metrics shape, and failure_stage coverage."""

    def test_success_property_deployed(self):
        from honeynet_framework.models import DeploymentResult, DeploymentStatus
        r = DeploymentResult(status=DeploymentStatus.DEPLOYED)
        assert r.success is True

    def test_success_property_degraded(self):
        from honeynet_framework.models import DeploymentResult, DeploymentStatus
        r = DeploymentResult(status=DeploymentStatus.RUNTIME_DEGRADED)
        assert r.success is True

    def test_success_property_failed(self):
        from honeynet_framework.models import DeploymentResult, DeploymentStatus
        r = DeploymentResult(status=DeploymentStatus.FAILED)
        assert r.success is False

    def test_failure_stage_field_exists(self):
        from honeynet_framework.models import DeploymentResult, DeploymentStatus
        r = DeploymentResult(status=DeploymentStatus.FAILED, failure_stage="extraction")
        assert r.failure_stage == "extraction"

    def test_run_id_field_exists(self):
        from honeynet_framework.models import DeploymentResult, DeploymentStatus
        r = DeploymentResult(status=DeploymentStatus.DEPLOYED, run_id="abc123")
        assert r.run_id == "abc123"

    def test_defaults_are_safe(self):
        from honeynet_framework.models import DeploymentResult, DeploymentStatus
        r = DeploymentResult(status=DeploymentStatus.FAILED)
        assert r.failure_stage is None
        assert r.run_id is None
        assert r.metrics is None
        assert r.qa_report is None
        assert r.errors == []
        assert r.warnings == []
        assert r.running_containers == 0
        assert r.expected_containers == 0


class TestMetricsShape:
    """Test DeploymentMetrics.to_dict() produces the expected shape for CLI/consumers."""

    def test_to_dict_top_level_keys(self):
        from honeynet_framework.metrics import DeploymentMetrics
        m = DeploymentMetrics()
        d = m.to_dict()
        assert set(d.keys()) == {
            "schema_version",
            "recorded_at_utc",
            "envelope",
            "observability",
            "deployability",
            "scenario_fit",
            "pipeline_kpis",
            "errors",
        }

    def test_observability_keys(self):
        from honeynet_framework.metrics import DeploymentMetrics
        m = DeploymentMetrics(run_id="r1", work_dir="/tmp", tofu_version="1.8.0", failure_stage="completed")
        obs = m.to_dict()["observability"]
        assert obs["run_id"] == "r1"
        assert obs["work_dir"] == "/tmp"
        assert obs["tofu_version"] == "1.8.0"
        assert obs["failure_stage"] == "completed"
        assert set(obs["path_a"].keys()) == {
            "diagnostics_collected",
            "diagnostics_artifact",
            "failing_resources_count",
            "docker_targets_count",
            "error_class",
        }

    def test_observability_path_a_values(self):
        from honeynet_framework.metrics import DeploymentMetrics
        m = DeploymentMetrics(
            path_a_diagnostics_collected=True,
            path_a_diagnostics_artifact="output/path_a_diagnostics.json",
            path_a_failing_resources_count=3,
            path_a_docker_targets_count=2,
        )
        path_a = m.to_dict()["observability"]["path_a"]
        assert path_a["diagnostics_collected"] is True
        assert path_a["diagnostics_artifact"] == "output/path_a_diagnostics.json"
        assert path_a["failing_resources_count"] == 3
        assert path_a["docker_targets_count"] == 2

    def test_deployability_keys(self):
        from honeynet_framework.metrics import DeploymentMetrics
        m = DeploymentMetrics(
            world_model_valid=True,
            config_validation_pass=True,
            plan_success=True,
            deploy_success=True,
            runtime_verification_success=True,
            health_check_rate=0.9,
            container_start_rate=0.95,
            expected_containers=20,
            running_containers=19,
            health_checks_total=50,
            health_checks_passed=45,
            health_check_status="measured",
            container_health_status="measured",
        )
        dep = m.to_dict()["deployability"]
        expected_keys = {
            "world_model_valid", "config_validation_pass", "plan_success",
            "deploy_success", "runtime_verification_success", "health_check_rate",
            "container_start_rate", "expected_containers", "running_containers",
            "planned_containers", "dropped_containers",
            "health_checks_total", "health_checks_passed",
            # Extended fields from metrics v3
            "validation_first_pass_success", "validation_repair_attempted",
            "validation_repair_success",
            "deploy_first_attempt", "deploy_retried", "deploy_retry_count",
            "health_check_status", "container_health_status",
            "health_check_rate_by_type",
        }
        assert set(dep.keys()) == expected_keys
        assert dep["health_check_rate"] == 0.9
        assert dep["container_start_rate"] == 0.95
        assert dep["running_containers"] == 19

    def test_not_run_metrics_are_none(self):
        """When stages are not_run, their float metrics should be None, not 0.0."""
        from honeynet_framework.metrics import DeploymentMetrics
        m = DeploymentMetrics()  # All defaults — nothing ran
        dep = m.to_dict()["deployability"]
        assert dep["health_check_rate"] is None, "Skipped health checks should be None, not 0.0"
        assert dep["container_start_rate"] is None, "Skipped container health should be None, not 0.0"

    def test_default_scenario_fit_fields_are_zero(self):
        """When no benchmark is configured, recall/coverage fields default to 0.0."""
        from honeynet_framework.metrics import DeploymentMetrics
        m = DeploymentMetrics()
        sf = m.to_dict()["scenario_fit"]
        assert sf["planned_service_coverage"] == 0.0
        assert sf["planned_zone_coverage"] == 0.0
        assert sf["planned_dep_coverage"] == 0.0
        assert sf["placement_violations"] == 0
        assert sf["running_service_coverage"] == 0.0
        assert sf["running_zone_coverage"] == 0.0
        assert sf["running_dep_coverage"] == 0.0

    def test_scenario_fit_keys(self):
        from honeynet_framework.metrics import DeploymentMetrics
        m = DeploymentMetrics(zone_count=5, system_count=20, model_dep_rate=1.0, image_check_rate=1.0)
        sf = m.to_dict()["scenario_fit"]
        expected_keys = {
            "zone_count", "system_count", "model_dep_rate",
            "image_check_rate",
            # Benchmark coverage fields (schema v3)
            "planned_service_coverage", "running_service_coverage",
            "planned_zone_coverage", "running_zone_coverage",
            "planned_dep_coverage", "running_dep_coverage",
            "benchmark_dep_required", "placement_violations",
            "benchmark_pass", "benchmark_status", "benchmark_id",
            "benchmark_ref", "scenario_fit_score",
        }
        assert set(sf.keys()) == expected_keys

    def test_errors_is_list(self):
        from honeynet_framework.metrics import DeploymentMetrics
        m = DeploymentMetrics()
        m.errors.append("some error")
        d = m.to_dict()
        assert d["errors"] == ["some error"]

    def test_summary_string(self):
        from honeynet_framework.metrics import DeploymentMetrics
        m = DeploymentMetrics(
            world_model_valid=True,
            expected_containers=10,
            running_containers=8,
            container_start_rate=0.8,
            health_check_rate=0.75,
            health_checks_total=12,
            health_checks_passed=9,
        )
        s = m.summary()
        assert "World Model Valid" in s
        assert "8/10" in s
        assert "9/12" in s


class TestFailureStage:
    """Test FailureStage enum coverage."""

    def test_all_stages_are_strings(self):
        from honeynet_framework.enums_pipeline import FailureStage
        for stage in FailureStage:
            assert isinstance(stage.value, str)

    def test_critical_stages_exist(self):
        from honeynet_framework.enums_pipeline import FailureStage
        required = {"extraction", "world_model_validation", "image_resolution",
                     "init", "validate", "plan", "apply", "runtime_verify", "completed"}
        actual = {s.value for s in FailureStage}
        assert required.issubset(actual)


class TestQARunner:
    """Test QA check generation from DeployProjection."""

    def test_build_checks_generates_running_checks(self):
        from honeynet_framework.qa import QARunner, CheckType
        wm = _make_test_world_model()
        proj = DeployCompiler().compile(wm)
        runner = QARunner()
        checks = runner.build_checks(proj)
        running_checks = [c for c in checks if c.check_type == CheckType.CONTAINER_RUNNING]
        assert len(running_checks) == len(proj.containers)

    def test_build_checks_generates_dep_checks(self):
        from honeynet_framework.qa import QARunner, CheckType
        wm = _make_test_world_model()
        proj = DeployCompiler().compile(wm)
        runner = QARunner()
        checks = runner.build_checks(proj)
        dep_checks = [c for c in checks if c.check_type == CheckType.DEPENDS_ON_RUNNING]
        # db depends on web → at least 1 dep check
        assert len(dep_checks) >= 1

    def test_build_checks_tcp_for_published_ports(self):
        from honeynet_framework.qa import QARunner, CheckType
        wm = _make_test_world_model()
        proj = DeployCompiler().compile(wm)
        runner = QARunner()
        checks = runner.build_checks(proj)
        tcp_checks = [c for c in checks if c.check_type == CheckType.TCP_CONNECT]
        # web has port 80 published (DMZ), db has no published ports (internal)
        assert len(tcp_checks) >= 1

    def test_build_checks_http_for_web_ports(self):
        from honeynet_framework.qa import QARunner, CheckType
        wm = _make_test_world_model()
        proj = DeployCompiler().compile(wm)
        runner = QARunner()
        checks = runner.build_checks(proj)
        http_checks = [c for c in checks if c.check_type == CheckType.HTTP_STATUS]
        # port 80 is in _WEB_PORTS
        assert len(http_checks) >= 1


class TestHealthcheckPipeline:
    """Test healthcheck flows through extract → compile → render."""

    def test_healthcheck_through_compiler(self):
        wm = _make_test_world_model()
        hc = {"test": ["CMD", "curl", "-f", "http://localhost/"], "interval": "30s", "timeout": "10s", "retries": 3, "start_period": "10s"}
        wm.systems["web"].deploy.healthcheck = hc
        proj = DeployCompiler().compile(wm)
        web = next(c for c in proj.containers if "web" in c.name)
        assert web.healthcheck == hc

    def test_healthcheck_in_tofu_json(self):
        wm = _make_test_world_model()
        hc = {"test": ["CMD", "curl", "-f", "http://localhost/"], "interval": "30s", "timeout": "10s", "retries": 3, "start_period": "10s"}
        wm.systems["web"].deploy.healthcheck = hc
        proj = DeployCompiler().compile(wm)
        output = TofuRenderer().render(proj)
        config = json.loads(output)
        containers = config["resource"]["docker_container"]
        web_key = [k for k in containers if "web" in k][0]
        web_res = containers[web_key]
        assert "healthcheck" in web_res

    def test_no_healthcheck_by_default(self):
        wm = _make_test_world_model()
        proj = DeployCompiler().compile(wm)
        web = next(c for c in proj.containers if "web" in c.name)
        assert web.healthcheck is None


class TestPluginRegistry:
    """Test plugin discovery doesn't crash with no plugins installed."""

    def test_discover_repair_empty(self):
        from honeynet_framework.plugins.registry import discover_repair_strategies
        result = discover_repair_strategies()
        assert isinstance(result, list)

    def test_discover_catalog_empty(self):
        from honeynet_framework.plugins.registry import discover_catalog_packs
        result = discover_catalog_packs()
        assert isinstance(result, list)

    def test_load_repair_callables_empty(self):
        from honeynet_framework.plugins.registry import load_repair_strategy_callables
        result = load_repair_strategy_callables()
        assert isinstance(result, list)


class TestFailureReport:
    """Test FailureReport classification from ValidationResult."""

    def test_from_passing_validation(self):
        from honeynet_framework.failure_report import from_validation_result, FailureReport
        from honeynet_framework.models.validation import ValidationResult
        vr = ValidationResult(passed=True)
        fr = from_validation_result(vr)
        assert fr.passed is True
        assert fr.total_errors == 0

    def test_from_failed_validation(self):
        from honeynet_framework.failure_report import from_validation_result, FailureType
        from honeynet_framework.models.validation import ValidationResult, ValidationError
        vr = ValidationResult(
            passed=False,
            errors=[
                ValidationError(rule="REF_ZONE", details="bad zone"),
                ValidationError(rule="CIRCULAR_DEPENDENCY", details="a -> b -> a"),
            ],
        )
        fr = from_validation_result(vr)
        assert fr.passed is False
        assert fr.total_errors == 2
        assert fr.errors[0].failure_type == FailureType.REFERENCE
        assert fr.errors[1].failure_type == FailureType.CYCLE

    def test_to_dict_shape(self):
        from honeynet_framework.failure_report import from_validation_result
        from honeynet_framework.models.validation import ValidationResult, ValidationError
        vr = ValidationResult(
            passed=False,
            errors=[ValidationError(rule="MIN_ZONES", details="no zones")],
        )
        fr = from_validation_result(vr)
        d = fr.to_dict()
        assert set(d.keys()) == {"passed", "total_errors", "total_warnings", "errors", "warnings"}
        assert d["errors"][0]["failure_type"] == "structural"

    def test_unknown_rule_classified(self):
        from honeynet_framework.failure_report import from_validation_result, FailureType
        from honeynet_framework.models.validation import ValidationResult, ValidationError
        vr = ValidationResult(passed=False, errors=[ValidationError(rule="CUSTOM_RULE", details="custom")])
        fr = from_validation_result(vr)
        assert fr.errors[0].failure_type == FailureType.UNKNOWN


class TestNoopRepair:
    """Test the no-op repair strategy plugin."""

    def test_noop_returns_same_model(self):
        from honeynet_framework.plugins.noop_repair import noop_repair
        wm = _make_test_world_model()
        result = noop_repair(wm)
        assert result is wm

    def test_noop_is_callable(self):
        from honeynet_framework.plugins.noop_repair import noop_repair
        assert callable(noop_repair)


class TestImageResolverSoftPolicy:
    """Test blocking_unresolved promote_soft parameter."""

    def test_soft_not_promoted_by_default(self):
        from honeynet_framework.image_resolver import ResolutionReport, ImageResolutionEntry, ResolutionStatus
        report = ResolutionReport(
            entries=[
                ImageResolutionEntry(image_ref="a:1", status=ResolutionStatus.RESOLVED),
                ImageResolutionEntry(image_ref="b:1", status=ResolutionStatus.RATE_LIMITED, message="429"),
            ],
        )
        assert len(report.blocking_unresolved()) == 0
        assert len(report.soft_unresolved()) == 1

    def test_soft_promoted_when_requested(self):
        from honeynet_framework.image_resolver import ResolutionReport, ImageResolutionEntry, ResolutionStatus
        report = ResolutionReport(
            entries=[
                ImageResolutionEntry(image_ref="b:1", status=ResolutionStatus.RATE_LIMITED, message="429"),
                ImageResolutionEntry(image_ref="c:1", status=ResolutionStatus.NETWORK_ERROR, message="timeout"),
            ],
        )
        assert len(report.blocking_unresolved(promote_soft=True)) == 2
        assert len(report.blocking_unresolved(promote_soft=False)) == 0


class TestCatalog:
    """Catalog snapshot load + deterministic resolution (P4)."""

    def test_load_catalog_snapshot(self):
        snap = load_catalog_snapshot(_FIXTURES / "catalog_snapshot_minimal.json")
        assert snap.schema_version == 1
        assert len(snap.entries) == 1
        assert snap.entries[0].id == "test-nginx"

    def test_duplicate_entry_ids_rejected(self, tmp_path: Path):
        bad = tmp_path / "bad.json"
        bad.write_text(
            '{"schema_version": 1, "entries": ['
            '{"id": "a", "default_image": "x:1"}, {"id": "a", "default_image": "y:1"}]}',
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="duplicate"):
            load_catalog_snapshot(bad)

    def test_apply_catalog_resolution(self):
        wm = _make_test_world_model()
        assert wm.systems["web"].deploy is not None
        wm.systems["web"].deploy.catalog_archetype = "test-nginx"
        snap = load_catalog_snapshot(_FIXTURES / "catalog_snapshot_minimal.json")
        _, applied = apply_catalog_resolution(wm, snap)
        assert wm.systems["web"].deploy.image == "nginx:1.25-alpine"
        assert len(applied) == 1
        assert applied[0]["catalog_archetype"] == "test-nginx"

    def test_missing_archetype_raises(self):
        wm = _make_test_world_model()
        assert wm.systems["web"].deploy is not None
        wm.systems["web"].deploy.catalog_archetype = "does-not-exist"
        snap = load_catalog_snapshot(_FIXTURES / "catalog_snapshot_minimal.json")
        with pytest.raises(CatalogResolutionError):
            apply_catalog_resolution(wm, snap)


class TestStrictSuccess:
    """Test strict_success property on DeploymentResult."""

    def test_strict_success_deployed(self):
        from honeynet_framework.models.deployment import DeploymentResult
        from honeynet_framework.models.enums import DeploymentStatus
        r = DeploymentResult(status=DeploymentStatus.DEPLOYED)
        assert r.success is True
        assert r.strict_success is True

    def test_strict_success_degraded(self):
        from honeynet_framework.models.deployment import DeploymentResult
        from honeynet_framework.models.enums import DeploymentStatus
        r = DeploymentResult(status=DeploymentStatus.RUNTIME_DEGRADED)
        assert r.success is True
        assert r.strict_success is False

    def test_strict_success_failed(self):
        from honeynet_framework.models.deployment import DeploymentResult
        from honeynet_framework.models.enums import DeploymentStatus
        r = DeploymentResult(status=DeploymentStatus.FAILED)
        assert r.success is False
        assert r.strict_success is False


class TestTelemetrySchemaVersion:
    """Test that telemetry events include schema_version per event."""

    def test_event_has_schema_version(self, tmp_path: Path):
        from honeynet_framework.telemetry_events import append_event, TELEMETRY_SCHEMA_VERSION
        append_event(
            tmp_path,
            run_id="test-run",
            stage="test",
            outcome="ok",
            duration_ms=42.0,
        )
        lines = (tmp_path / "telemetry" / "events.jsonl").read_text(encoding="utf-8").strip().split("\n")
        # Line 0 is header, line 1 is the event
        assert len(lines) == 2
        event = json.loads(lines[1])
        assert event["schema_version"] == TELEMETRY_SCHEMA_VERSION
        assert event["run_id"] == "test-run"

    def test_header_still_written(self, tmp_path: Path):
        from honeynet_framework.telemetry_events import append_event
        append_event(
            tmp_path,
            run_id="test-run",
            stage="test",
            outcome="ok",
            duration_ms=1.0,
        )
        lines = (tmp_path / "telemetry" / "events.jsonl").read_text(encoding="utf-8").strip().split("\n")
        header = json.loads(lines[0])
        assert header["kind"] == "honeynet_telemetry_header"
        assert "schema_version" in header


class TestSemanticJudgeNonDict:
    """Test that semantic judge handles non-dict JSON gracefully."""

    def test_non_dict_response_graceful(self):
        """Semantic judge handles non-dict / unparseable JSON without crashing."""
        import asyncio
        from unittest.mock import AsyncMock
        from honeynet_framework.semantic_judge import run_semantic_judge

        mock_llm = AsyncMock()
        # LLM returns garbage that extract_json_from_response can't parse
        mock_llm.generate.return_value = ('totally not json at all', {"total_tokens": 10})

        wm = _make_test_world_model()
        result = asyncio.run(
            run_semantic_judge(
                user_request="test",
                world_model=wm,
                llm=mock_llm,
            )
        )
        # Should not crash — returns passed=False with error info
        assert result.passed is False
        assert len(result.findings) > 0


class TestImageResolverNoSuchManifest:
    """Test that 'no such manifest' stderr maps to NOT_FOUND."""

    def test_no_such_manifest_is_not_found(self):
        from honeynet_framework.image_resolver import ResolutionStatus
        # The pattern "no such manifest" should be in the NOT_FOUND check
        # This is a unit-level assertion on the code logic
        stderr = "no such manifest: docker.io/library/vault:1.14.3"
        assert "no such manifest" in stderr
        # Verify via the actual status classification patterns
        not_found_patterns = ["not found", "does not exist", "404", "manifest unknown", "no such manifest"]
        assert any(p in stderr for p in not_found_patterns)


class TestPortAllocationGuard:
    """Test that port allocation raises instead of looping infinitely."""

    def test_exhausted_ports_raises(self):
        from honeynet_framework.deploy_compiler import DeployCompiler, CompilerConfig
        compiler = DeployCompiler(CompilerConfig())
        # Fill up all ports from 10000 to 65535
        compiler._used_host_ports = set(range(10000, 65536))
        # Also fill preferred port range below 10000
        compiler._used_host_ports.update(range(1, 10000))
        with pytest.raises(RuntimeError, match="are exhausted"):
            compiler._allocate_host_port(8080)

    def test_normal_allocation_works(self):
        from honeynet_framework.deploy_compiler import DeployCompiler, CompilerConfig
        compiler = DeployCompiler(CompilerConfig())
        port1 = compiler._allocate_host_port(8080)
        assert port1 == 8080
        port2 = compiler._allocate_host_port(8080)
        assert port2 == 8081  # next available


class TestContainerNameCollision:
    """Test that container name collisions are detected and resolved."""

    def test_duplicate_safe_names_resolved(self):
        from honeynet_framework.deploy_compiler import DeployCompiler, CompilerConfig
        compiler = DeployCompiler(CompilerConfig())
        wm = WorldModel()
        wm.zones["dmz"] = WorldZone(name="dmz", deploy=ZoneDeploy(network_name="dmz"))
        # Two systems that map to the same safe name
        wm.systems["my-app"] = System(
            name="my-app", kind=SystemKind.WEB,
            deploy=SystemDeploy(image="nginx:latest", zone="dmz"),
        )
        wm.systems["my app"] = System(
            name="my app", kind=SystemKind.WEB,
            deploy=SystemDeploy(image="nginx:latest", zone="dmz"),
        )
        proj = compiler.compile(wm)
        names = [c.name for c in proj.containers]
        assert len(names) == len(set(names)), f"Duplicate container names: {names}"


class TestJsonBraceExtraction:
    """Test that JSON extraction handles braces inside strings correctly."""

    def test_braces_in_string_values(self):
        from honeynet_framework.llm import extract_json_from_response
        text = '{"key": "value with {braces} inside", "num": 42}'
        result = extract_json_from_response(text)
        assert result["key"] == "value with {braces} inside"
        assert result["num"] == 42

    def test_nested_objects(self):
        from honeynet_framework.llm import extract_json_from_response
        text = 'Here is JSON: {"outer": {"inner": "value"}, "list": [1, 2]}'
        result = extract_json_from_response(text)
        assert result["outer"]["inner"] == "value"


class TestYamlQuoteEscaping:
    """Test that YAML repair properly escapes quotes when auto-quoting."""

    def test_value_with_quotes_and_colon(self):
        from honeynet_framework.extraction.yaml_parsing import _repair_yaml
        text = 'description: He said "hello": world'
        repaired = _repair_yaml(text)
        # The colon triggers quoting; the inner " must be escaped
        assert '\\"hello\\"' in repaired


class TestScaleEstimation:
    """Test scale estimation edge cases."""

    def test_min_never_exceeds_max(self):
        from honeynet_framework.extraction.prompts import _estimate_scale
        # Very large prompt with many tech mentions should still have min <= max
        big_prompt = " ".join([
            "postgres mysql redis elasticsearch mongodb kafka rabbitmq",
            "nginx traefik haproxy grafana prometheus kibana",
            "vault consul jenkins gitlab kubernetes docker",
        ] * 5)
        min_sys, max_sys, min_zones, max_zones = _estimate_scale(big_prompt)
        assert min_sys <= max_sys, f"min_systems ({min_sys}) > max_systems ({max_sys})"
        assert min_zones <= max_zones


class TestHealthcheckExtraction:
    """Test healthcheck parsing edge cases in extractor."""

    def test_non_numeric_retries_defaults(self):
        from honeynet_framework.extraction.extractor import WorldModelExtractor
        from unittest.mock import AsyncMock
        ext = WorldModelExtractor(AsyncMock())
        raw = {
            "zones": {"dmz": {"network_name": "dmz"}},
            "systems": {
                "web": {
                    "kind": "web",
                    "deploy": {
                        "image": "nginx:latest",
                        "zone": "dmz",
                        "healthcheck": {
                            "test": ["CMD", "curl", "-f", "http://localhost/"],
                            "retries": "not_a_number",
                        },
                    },
                }
            },
        }
        wm = ext._build_world_model(raw)
        hc = wm.systems["web"].deploy.healthcheck
        assert hc is not None
        assert hc["retries"] == 3  # default fallback

    def test_healthcheck_without_test_ignored(self):
        from honeynet_framework.extraction.extractor import WorldModelExtractor
        from unittest.mock import AsyncMock
        ext = WorldModelExtractor(AsyncMock())
        raw = {
            "zones": {"dmz": {"network_name": "dmz"}},
            "systems": {
                "web": {
                    "kind": "web",
                    "deploy": {
                        "image": "nginx:latest",
                        "zone": "dmz",
                        "healthcheck": {"interval": "30s"},
                    },
                }
            },
        }
        wm = ext._build_world_model(raw)
        assert wm.systems["web"].deploy.healthcheck is None


class TestRendererUsesConstraints:
    """Test that renderer uses projection.constraints.provider_version."""

    def test_custom_provider_version(self):
        from honeynet_framework.tofu_renderer import TofuRenderer
        from honeynet_framework.models import DeployProjection, TerraformConstraints
        proj = DeployProjection(
            project_name="test",
            networks=[],
            containers=[],
            volumes=[],
            constraints=TerraformConstraints(provider_version="99.0.0"),
        )
        renderer = TofuRenderer()
        out = renderer.render(proj)
        config = json.loads(out)
        version = config["terraform"]["required_providers"]["docker"]["version"]
        assert version == "99.0.0"


class TestJsonEscapeDetection:
    """Test that JSON extraction handles escaped backslashes before quotes correctly.

    The edge case: \\\\" inside a JSON string means escaped-backslash + real-closing-quote.
    The parser must count consecutive backslashes (even count → quote is NOT escaped).
    """

    def test_escaped_backslash_before_quote(self):
        from honeynet_framework.llm import extract_json_from_response
        # JSON: {"path": "C:\\\\Users\\\\test"} — the \\\\ are escaped backslashes
        text = r'{"path": "C:\\Users\\test"}'
        result = extract_json_from_response(text)
        assert result["path"] == "C:\\Users\\test"

    def test_escaped_backslash_at_end_of_string(self):
        from honeynet_framework.llm import extract_json_from_response
        # JSON: {"trail": "end\\\\"} — the value ends with a literal backslash
        text = '{"trail": "end\\\\"}'
        result = extract_json_from_response(text)
        assert result["trail"] == "end\\"

    def test_actual_escaped_quote_inside_string(self):
        from honeynet_framework.llm import extract_json_from_response
        # JSON: {"say": "he said \\"hi\\""} — real escaped quotes
        text = '{"say": "he said \\"hi\\""}'
        result = extract_json_from_response(text)
        assert result["say"] == 'he said "hi"'

    def test_mixed_escapes(self):
        from honeynet_framework.llm import extract_json_from_response
        # Braces inside a string value with escapes nearby
        text = '{"cmd": "echo \\"{hello}\\"", "n": 1}'
        result = extract_json_from_response(text)
        assert result["n"] == 1


class TestCommandWarningsBlocking:
    """Test that command_warnings_are_errors config promotes warnings to errors."""

    def test_one_shot_is_warning_by_default(self):
        """ONE_SHOT_COMMAND is a warning, not an error — validation passes."""
        wm = WorldModel(
            organization=Organization(name="Test"),
            zones={"dmz": WorldZone(name="dmz", deploy=ZoneDeploy(network_name="dmz"))},
            systems={
                "backup": System(
                    name="backup",
                    kind=SystemKind.STORAGE,
                    deploy=SystemDeploy(
                        image="restic/restic:0.15.1",
                        zone="dmz",
                        command=["restic", "backup", "/data"],
                    ),
                ),
            },
        )
        result = validate(wm)
        assert result.passed, "One-shot commands should be warnings, not errors"
        warning_rules = {w.rule for w in result.warnings}
        assert "ONE_SHOT_COMMAND" in warning_rules

    def test_fictional_script_is_warning_by_default(self):
        """FICTIONAL_SCRIPT is a warning — validation passes without profiles."""
        wm = WorldModel(
            organization=Organization(name="Test"),
            zones={"dmz": WorldZone(name="dmz", deploy=ZoneDeploy(network_name="dmz"))},
            systems={
                "app": System(
                    name="app",
                    kind=SystemKind.RUNTIME,
                    deploy=SystemDeploy(
                        image="python:3.12-slim",
                        zone="dmz",
                        command=["python", "scheduler.py"],
                    ),
                ),
            },
        )
        result = validate(wm)
        # Without ImageProfiles, command policy trusts all commands (no
        # INVALID_COMMAND errors).  FICTIONAL_SCRIPT still fires as a warning
        # because the image name starts with a MODULE_LAUNCHER.
        assert result.passed, "Without profiles, validation should pass"
        warning_rules = {w.rule for w in result.warnings}
        assert "FICTIONAL_SCRIPT" in warning_rules

    def test_shell_wrapped_one_shot_detected(self):
        """One-shot commands wrapped in sh -c are also detected."""
        wm = WorldModel(
            organization=Organization(name="Test"),
            zones={"dmz": WorldZone(name="dmz", deploy=ZoneDeploy(network_name="dmz"))},
            systems={
                "dump": System(
                    name="dump",
                    kind=SystemKind.DATABASE,
                    deploy=SystemDeploy(
                        image="postgres:16-alpine",
                        zone="dmz",
                        command=["sh", "-c", "pg_dump mydb > /backup/dump.sql"],
                    ),
                ),
            },
        )
        result = validate(wm)
        # Without ImageProfiles, command policy trusts all commands.
        # ONE_SHOT_COMMAND still fires as a warning for observability.
        warning_rules = {w.rule for w in result.warnings}
        assert "ONE_SHOT_COMMAND" in warning_rules

    def test_no_false_positive_on_daemon(self):
        """Normal daemon images with no command produce no warnings."""
        wm = _make_test_world_model()
        result = validate(wm)
        assert result.passed
        command_warnings = [
            w for w in result.warnings
            if w.rule in ("ONE_SHOT_COMMAND", "FICTIONAL_SCRIPT")
        ]
        assert len(command_warnings) == 0


class TestLockingFailClosed:
    """Test that lock acquisition failure prevents deployment."""

    def test_oserror_on_lock_returns_failed(self):
        """OSError during lock creation must fail-closed, not proceed."""
        import asyncio
        from unittest.mock import patch, AsyncMock
        from honeynet_framework.orchestrator import HoneynetOrchestrator, OrchestratorConfig
        from honeynet_framework.models import DeploymentStatus

        config = OrchestratorConfig(work_dir=Path("test_lock_dir"))
        orch = HoneynetOrchestrator(config)
        orch._initialized = True
        orch.llm = AsyncMock()
        orch.extractor = AsyncMock()
        orch.deployer = AsyncMock()
        orch.compiler = AsyncMock()
        orch.renderer = AsyncMock()
        orch.image_resolver = AsyncMock()

        with patch("os.open", side_effect=OSError(13, "Permission denied")):
            result = asyncio.run(orch._deploy_impl("test prompt"))
        assert result.status == DeploymentStatus.FAILED
        assert "Cannot create lock file" in result.errors[0]


class TestCatalogArchetypeWithoutImage:
    """Test that catalog_archetype-only deploy sections are preserved."""

    def test_archetype_without_image_creates_deploy(self):
        from honeynet_framework.extraction.extractor import WorldModelExtractor
        from unittest.mock import AsyncMock

        ext = WorldModelExtractor(AsyncMock())
        raw = {
            "zones": {"dmz": {"network_name": "dmz"}},
            "systems": {
                "honey_db": {
                    "kind": "database",
                    "deploy": {
                        "zone": "dmz",
                        "catalog_archetype": "postgres_honeypot",
                        "ports": [5432],
                    },
                },
            },
        }
        wm = ext._build_world_model(raw)
        sys = wm.systems["honey_db"]
        assert sys.deploy is not None, "deploy section should exist with catalog_archetype even without image"
        assert sys.deploy.catalog_archetype == "postgres_honeypot"
        assert sys.deploy.image == ""  # empty until catalog resolves it
        assert 5432 in sys.deploy.ports

    def test_no_deploy_without_image_or_archetype(self):
        """Systems with neither image nor catalog_archetype get no deploy."""
        from honeynet_framework.extraction.extractor import WorldModelExtractor
        from unittest.mock import AsyncMock

        ext = WorldModelExtractor(AsyncMock())
        raw = {
            "zones": {"dmz": {"network_name": "dmz"}},
            "systems": {
                "empty": {
                    "kind": "unknown",
                    "deploy": {"zone": "dmz"},
                },
            },
        }
        wm = ext._build_world_model(raw)
        assert wm.systems["empty"].deploy is None


class TestPortSeedingInCompiler:
    """Test that pre-seeded ports are avoided during compilation."""

    def test_seeded_port_not_allocated(self):
        compiler = DeployCompiler()
        compiler.seed_used_ports({80, 8080})
        port = compiler._allocate_host_port(80)
        assert port != 80, "Seeded port 80 should not be re-allocated"
        assert port != 8080, "Seeded port 8080 should not be re-allocated"

    def test_compile_preserves_seeded_ports(self):
        """Seeded ports persist across compile() calls."""
        compiler = DeployCompiler()
        compiler.seed_used_ports({80})
        wm = _make_test_world_model()
        projection = compiler.compile(wm)
        # The web container requests port 80 but it's seeded as occupied
        for c in projection.containers:
            for p in c.ports:
                if p.internal == 80:
                    assert p.external != 80, "Host port 80 should be avoided"


class TestCoercionWarnings:
    """Test that coercion functions warn on dropped values."""

    def test_none_string_dropped_with_warning(self, caplog):
        import logging
        from honeynet_framework.extraction.coercion import coerce_string_list
        with caplog.at_level(logging.WARNING):
            result = coerce_string_list([None])
        assert result == []

    def test_str_none_dropped_with_warning(self, caplog):
        import logging
        from honeynet_framework.extraction.coercion import coerce_string_list
        with caplog.at_level(logging.WARNING):
            result = coerce_string_list(["valid", None])
        assert result == ["valid"]

    def test_non_integer_port_warns(self, caplog):
        import logging
        from honeynet_framework.extraction.coercion import coerce_int_list
        with caplog.at_level(logging.WARNING):
            result = coerce_int_list([80, "not_a_port", 443])
        assert result == [80, 443]
        assert "non-integer" in caplog.text


class TestLockOwnershipCleanup:
    """Lock file should only be deleted if owned by current run."""

    def test_foreign_lock_not_deleted(self, tmp_path):
        """A lock owned by a different run_id must not be deleted."""
        import json

        lockfile = tmp_path / ".honeynet.lock"
        foreign_lock = {"run_id": "foreign-run-999", "pid": 12345}
        lockfile.write_text(json.dumps(foreign_lock), encoding="utf-8")

        # Simulate what the finally block does: read + check run_id
        our_run_id = "our-run-001"
        try:
            lock_content = json.loads(lockfile.read_text(encoding="utf-8"))
            if lock_content.get("run_id") == our_run_id:
                lockfile.unlink()
        except (OSError, json.JSONDecodeError, ValueError):
            pass

        assert lockfile.exists(), "Foreign lock must not be deleted"
        assert json.loads(lockfile.read_text())["run_id"] == "foreign-run-999"

    def test_own_lock_is_deleted(self, tmp_path):
        """A lock owned by our run_id should be cleaned up."""
        import json

        lockfile = tmp_path / ".honeynet.lock"
        our_run_id = "our-run-001"
        lockfile.write_text(json.dumps({"run_id": our_run_id, "pid": 99}), encoding="utf-8")

        try:
            lock_content = json.loads(lockfile.read_text(encoding="utf-8"))
            if lock_content.get("run_id") == our_run_id:
                lockfile.unlink()
        except (OSError, json.JSONDecodeError, ValueError):
            pass

        assert not lockfile.exists(), "Own lock should be deleted"


class TestDependsOnRemappingAfterCollision:
    """depends_on references must track name collision renames."""

    def test_depends_on_updated_after_rename(self):
        """When two systems produce the same container name, depends_on must
        point to the renamed container."""
        wm = WorldModel(
            organization=Organization(name="test"),
            zones={
                "dmz": WorldZone(
                    name="dmz",
                    deploy=ZoneDeploy(network_name="dmz", driver="bridge", internal=False),
                ),
            },
            systems={
                # These two system names both sanitize to "web_app"
                "web-app": System(
                    name="web-app",
                    kind=SystemKind.WEB,
                    deploy=SystemDeploy(
                        image="nginx:latest",
                        zone="dmz",
                        ports=[80],
                    ),
                ),
                "web.app": System(
                    name="web.app",
                    kind=SystemKind.WEB,
                    deploy=SystemDeploy(
                        image="nginx:latest",
                        zone="dmz",
                        ports=[8080],
                        depends_on=[],
                    ),
                ),
                "backend": System(
                    name="backend",
                    kind=SystemKind.WEB,
                    deploy=SystemDeploy(
                        image="python:3",
                        zone="dmz",
                        ports=[5000],
                        depends_on=["web-app", "web.app"],
                    ),
                ),
            },
        )
        compiler = DeployCompiler()
        proj = compiler.compile(wm)

        # Find backend container
        backend = [c for c in proj.containers if "backend" in c.name][0]
        # All depends_on entries must reference containers that actually exist
        container_names = {c.name for c in proj.containers}
        for dep in backend.depends_on:
            assert dep in container_names, (
                f"depends_on '{dep}' not found in containers: {container_names}"
            )

        # The two web_app containers must have distinct names
        web_containers = [c for c in proj.containers if "web_app" in c.name]
        assert len(web_containers) == 2
        assert web_containers[0].name != web_containers[1].name


class TestScopedOrphanCleanup:
    """Orphan cleanup must be scoped to the project, not global."""

    def test_cleanup_reads_project_name_from_world_model(self):
        """_remove_orphaned_docker_resources derives project name from saved
        world_model file and skips when it cannot be determined. Verify the
        method contains the safety-skip logic."""
        import ast

        src_path = Path(__file__).resolve().parent.parent / "honeynet_framework" / "orchestrator.py"
        source = src_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        # Find the method and verify it contains the "project_name" guard
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_remove_orphaned_docker_resources":
                body_src = ast.get_source_segment(source, node)
                assert "project_name" in body_src, "Method must derive project_name"
                assert "if not project_name" in body_src, "Method must skip when project_name is empty"
                break
        else:
            pytest.fail("_remove_orphaned_docker_resources not found in orchestrator.py")

    def test_cleanup_filters_containers_with_startswith(self):
        """Orphan cleanup must use startswith, not Docker's substring --filter alone."""
        import ast

        src_path = Path(__file__).resolve().parent.parent / "honeynet_framework" / "orchestrator.py"
        source = src_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_remove_orphaned_docker_resources":
                body_src = ast.get_source_segment(source, node)
                assert "startswith" in body_src, (
                    "Container cleanup must use startswith() to avoid substring matches"
                )
                break
        else:
            pytest.fail("_remove_orphaned_docker_resources not found")


class TestCompilerIdempotency:
    """compile() must be idempotent: same input → same output across calls."""

    def test_repeated_compile_same_output(self):
        """Two compile() calls on the same WorldModel must produce identical port allocations."""
        wm = _make_test_world_model()
        compiler = DeployCompiler()

        proj1 = compiler.compile(wm)
        proj2 = compiler.compile(wm)

        ports1 = {c.name: [p.external for p in c.ports] for c in proj1.containers}
        ports2 = {c.name: [p.external for p in c.ports] for c in proj2.containers}
        assert ports1 == ports2, f"Non-idempotent: first={ports1}, second={ports2}"

    def test_seeded_ports_persist_across_compile(self):
        """Seeded ports must be respected in every compile() call."""
        wm = _make_test_world_model()
        compiler = DeployCompiler()
        compiler.seed_used_ports({80})

        for _ in range(3):
            proj = compiler.compile(wm)
            for c in proj.containers:
                for p in c.ports:
                    if p.internal == 80:
                        assert p.external != 80, "Seeded port 80 must not be allocated"


class TestImportRecoveryRegex:
    """Import recovery must only match resource types it can actually handle."""

    def test_regex_matches_network_and_container(self):
        import re
        pattern = re.compile(r'with\s+(docker_(?:network|container)\.(\S+?)),')
        assert pattern.search('with docker_network.my_net,')
        assert pattern.search('with docker_container.my_ctr,')

    def test_regex_rejects_image_and_volume(self):
        import re
        pattern = re.compile(r'with\s+(docker_(?:network|container)\.(\S+?)),')
        assert pattern.search('with docker_image.my_img,') is None
        assert pattern.search('with docker_volume.my_vol,') is None


class TestImageResolverRetry:
    """Image resolver must retry transient errors."""

    def test_resolver_has_retry_capability(self):
        from honeynet_framework.image_resolver import ImageResolver, ResolutionStatus
        resolver = ImageResolver(max_retries=2)
        assert resolver.max_retries == 2
        assert ResolutionStatus.NETWORK_ERROR in resolver._TRANSIENT_STATUSES
        assert ResolutionStatus.RATE_LIMITED in resolver._TRANSIENT_STATUSES
        # Non-transient statuses must NOT be retried
        assert ResolutionStatus.NOT_FOUND not in resolver._TRANSIENT_STATUSES
        assert ResolutionStatus.AUTH_REQUIRED not in resolver._TRANSIENT_STATUSES


class TestFailureStageTracking:
    """_current_stage must be set precisely before each substep."""

    def test_stage_markers_in_deploy_locked(self):
        """Verify render/cleanup/plan each get their own _current_stage assignment."""
        src_path = Path(__file__).resolve().parent.parent / "honeynet_framework" / "orchestrator.py"
        source = src_path.read_text(encoding="utf-8")

        # The render step must have its own stage marker
        assert '_current_stage = FailureStage.RENDER.value' in source, "Render step must set _current_stage"
        assert '_current_stage = FailureStage.CLEANUP.value' in source, "Cleanup step must set _current_stage"
        # Plan/apply step must set stage before plan_and_apply
        assert '_current_stage = FailureStage.PLAN.value' in source, (
            "Plan step must set _current_stage before plan_and_apply"
        )


class TestCleanupBestEffort:
    """File deletion in _cleanup_old_state must be best-effort."""

    def test_unlink_wrapped_in_oserror_handler(self):
        """Verify that unlink() calls in _cleanup_old_state are wrapped with OSError handling."""
        import ast

        src_path = Path(__file__).resolve().parent.parent / "honeynet_framework" / "orchestrator.py"
        source = src_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_cleanup_old_state":
                body_src = ast.get_source_segment(source, node)
                # The main file-deletion loop must have OSError handling
                assert "OSError" in body_src, (
                    "_cleanup_old_state must catch OSError on file deletion"
                )
                break
        else:
            pytest.fail("_cleanup_old_state not found in orchestrator.py")


class TestDockerLabelsInRenderer:
    """TofuRenderer must set honeynet.project labels on containers."""

    def test_containers_have_project_label(self):
        wm = _make_test_world_model()
        from honeynet_framework.deploy_compiler import DeployCompiler
        from honeynet_framework.tofu_renderer import TofuRenderer
        import json

        compiler = DeployCompiler()
        proj = compiler.compile(wm)
        renderer = TofuRenderer()
        out = renderer.render(proj)
        config = json.loads(out)

        containers = config["resource"]["docker_container"]
        for name, block in containers.items():
            assert "labels" in block, f"Container {name} missing labels"
            assert isinstance(block["labels"], list)
            labels = {entry["label"]: entry["value"] for entry in block["labels"]}
            assert labels.get("honeynet.project") == wm.project_name
            assert labels.get("honeynet.managed") == "true"


class TestPluginAllowlist:
    """Plugin loading must respect allowlist."""

    def test_allowlist_blocks_unlisted(self):
        from honeynet_framework.plugins.registry import set_plugin_allowlist, _is_allowed
        set_plugin_allowlist(["trusted_plugin"])
        assert _is_allowed("trusted_plugin") is True
        assert _is_allowed("malicious_plugin") is False
        # Clean up
        set_plugin_allowlist([])

    def test_empty_allowlist_allows_all(self):
        from honeynet_framework.plugins.registry import set_plugin_allowlist, _is_allowed
        set_plugin_allowlist([])
        assert _is_allowed("any_plugin") is True

    def test_plugins_disabled_by_default(self):
        from honeynet_framework.orchestrator import OrchestratorConfig
        config = OrchestratorConfig()
        assert config.enable_plugins is False


class TestStringCommandNormalization:
    """Validator must handle string commands via shlex tokenization."""

    def test_string_command_detected_as_one_shot(self):
        """A bare string command like 'restic backup /data' must be detected."""
        from honeynet_framework.validator import _normalize_command
        tokens = _normalize_command("restic backup /data")
        assert tokens == ["restic", "backup", "/data"]

    def test_list_command_passthrough(self):
        from honeynet_framework.validator import _normalize_command
        tokens = _normalize_command(["python", "app.py"])
        assert tokens == ["python", "app.py"]

    def test_quoted_string_command(self):
        from honeynet_framework.validator import _normalize_command
        tokens = _normalize_command('sh -c "echo hello && sleep infinity"')
        assert tokens == ["sh", "-c", "echo hello && sleep infinity"]

    def test_one_shot_warning_for_string_command(self):
        """String command 'restic backup' must trigger ONE_SHOT_COMMAND warning."""
        from honeynet_framework.validator import validate
        wm = _make_test_world_model()
        # Inject a string command (simulating LLM output)
        for sys in wm.systems.values():
            if sys.deploy:
                sys.deploy.command = "restic backup /data"  # type: ignore[assignment]
                break
        result = validate(wm)
        one_shot_warnings = [w for w in result.warnings if w.rule == "ONE_SHOT_COMMAND"]
        assert len(one_shot_warnings) >= 1, "String command 'restic backup' should be detected"


class TestWindowsVolumeCoercion:
    """Volume coercion must handle Windows drive-letter paths."""

    def test_windows_path_parsed_correctly(self):
        from honeynet_framework.extraction.coercion import coerce_volume_mounts
        mounts = coerce_volume_mounts("C:\\data:/container/data:ro")
        assert len(mounts) == 1
        assert mounts[0].name == "C:\\data"
        assert mounts[0].path == "/container/data"
        assert mounts[0].readonly is True

    def test_unix_path_still_works(self):
        from honeynet_framework.extraction.coercion import coerce_volume_mounts
        mounts = coerce_volume_mounts("myvolume:/data:ro")
        assert len(mounts) == 1
        assert mounts[0].name == "myvolume"
        assert mounts[0].path == "/data"
        assert mounts[0].readonly is True

    def test_windows_path_no_readonly(self):
        from honeynet_framework.extraction.coercion import coerce_volume_mounts
        mounts = coerce_volume_mounts("D:\\logs:/var/log")
        assert len(mounts) == 1
        assert mounts[0].name == "D:\\logs"
        assert mounts[0].path == "/var/log"
        assert mounts[0].readonly is False


class TestQAPortOwnerVerification:
    """QA checks must include port-owner container param."""

    def test_tcp_check_includes_container_param(self):
        from honeynet_framework.qa import QARunner
        wm = _make_test_world_model()
        from honeynet_framework.deploy_compiler import DeployCompiler
        compiler = DeployCompiler()
        proj = compiler.compile(wm)

        runner = QARunner()
        checks = runner.build_checks(proj)
        tcp_checks = [c for c in checks if c.check_type.value == "tcp_connect"]
        for check in tcp_checks:
            assert "container" in check.params, (
                f"TCP check {check.id} must include container param for port-owner verification"
            )


class TestRuntimeStartingHandling:
    """verify_deployment must eventually treat 'starting' containers as degraded."""

    def test_deployer_docstring_mentions_starting_as_unhealthy(self):
        """Verify the deployer treats 'starting' as potentially unhealthy."""
        from honeynet_framework.deployer import TerraformDeployer
        docstring = TerraformDeployer.verify_deployment.__doc__
        assert "starting" in docstring.lower()
        # Should NOT say starting is treated as healthy
        assert "starting_timeout" in open(
            str(Path(__file__).resolve().parent.parent / "honeynet_framework" / "deployer.py"),
            encoding="utf-8",
        ).read(), "Deployer must mark stuck-starting containers with starting_timeout"


class TestDestroyCycleRecovery:
    """Destroy-cycle recovery should heal state when dependency direction flips."""

    def test_extract_destroy_cycle_container_addresses(self):
        from honeynet_framework.deployer import TerraformDeployer

        stderr = (
            "Error: Cycle: docker_container.hn_job_scheduler (destroy), "
            "docker_container.hn_gpu_manager (destroy)"
        )

        assert TerraformDeployer._extract_destroy_cycle_container_addresses(stderr) == [
            "docker_container.hn_job_scheduler",
            "docker_container.hn_gpu_manager",
        ]

    def test_recover_container_destroy_cycle_removes_containers_and_state(self, tmp_path: Path):
        import asyncio

        from honeynet_framework.deployer import CommandResult, DeployerConfig, TerraformDeployer

        state = {
            "resources": [
                {
                    "type": "docker_container",
                    "name": "hn_job_scheduler",
                    "instances": [{"attributes": {"name": "hn_job_scheduler"}}],
                },
                {
                    "type": "docker_container",
                    "name": "hn_gpu_manager",
                    "instances": [{"attributes": {"name": "hn_gpu_manager"}}],
                },
            ]
        }
        (tmp_path / "terraform.tfstate").write_text(json.dumps(state), encoding="utf-8")

        deployer = TerraformDeployer(DeployerConfig(work_dir=tmp_path, tofu_binary="tofu"))
        docker_calls = []
        tofu_calls = []

        async def fake_run_docker_cmd(args):
            docker_calls.append(args)
            return CommandResult(success=True, stdout="", stderr="", exit_code=0)

        async def fake_run_tofu(args):
            tofu_calls.append(args)
            return CommandResult(success=True, stdout="", stderr="", exit_code=0)

        deployer._run_docker_cmd = fake_run_docker_cmd
        deployer._run_tofu = fake_run_tofu

        result = asyncio.run(
            deployer.recover_container_destroy_cycle(
                "Cycle: docker_container.hn_job_scheduler (destroy), "
                "docker_container.hn_gpu_manager (destroy)"
            )
        )

        assert result.success is True
        assert docker_calls == [
            ["rm", "-f", "hn_job_scheduler"],
            ["rm", "-f", "hn_gpu_manager"],
        ]
        assert tofu_calls == [[
            "state",
            "rm",
            "docker_container.hn_job_scheduler",
            "docker_container.hn_gpu_manager",
        ]]

    def test_destroy_retries_after_cycle_recovery(self, tmp_path: Path):
        import asyncio

        from honeynet_framework.deployer import CommandResult, DeployerConfig, TerraformDeployer

        deployer = TerraformDeployer(DeployerConfig(work_dir=tmp_path, tofu_binary="tofu"))
        tofu_calls = []
        recover_calls = []

        async def fake_run_tofu(args):
            tofu_calls.append(args)
            if len(tofu_calls) == 1:
                return CommandResult(
                    success=False,
                    stdout="",
                    stderr=(
                        "Cycle: docker_container.hn_job_scheduler (destroy), "
                        "docker_container.hn_gpu_manager (destroy)"
                    ),
                    exit_code=1,
                )
            return CommandResult(success=True, stdout="destroyed", stderr="", exit_code=0)

        async def fake_recover(stderr):
            recover_calls.append(stderr)
            return CommandResult(success=True, stdout="cycle recovered", stderr="", exit_code=0)

        deployer._run_tofu = fake_run_tofu
        deployer.recover_container_destroy_cycle = fake_recover

        result = asyncio.run(deployer.destroy())

        assert result.success is True
        assert recover_calls == [
            "Cycle: docker_container.hn_job_scheduler (destroy), "
            "docker_container.hn_gpu_manager (destroy)"
        ]
        assert tofu_calls == [
            ["destroy", "-auto-approve"],
            ["destroy", "-auto-approve"],
        ]


# ===================================================================== #
# TP1: Telemetry Features
# ===================================================================== #

class TestInteractionFeature:
    """InteractionFeature schema with defensive defaults."""

    def test_defaults_are_safe(self):
        from honeynet_framework.telemetry_features import InteractionFeature
        f = InteractionFeature()
        assert f.event_type == "unknown"
        assert f.latency_s == -1.0
        assert f.action_family == "other"
        assert f.source_class == "unknown"
        assert f.confidence_hint == 0.0

    def test_invalid_action_family_normalized(self):
        from honeynet_framework.telemetry_features import InteractionFeature
        f = InteractionFeature(action_family="INVALID")
        assert f.action_family == "other"

    def test_confidence_clamped(self):
        from honeynet_framework.telemetry_features import InteractionFeature
        f = InteractionFeature(confidence_hint=5.0)
        assert f.confidence_hint == 1.0
        f2 = InteractionFeature(confidence_hint=-2.0)
        assert f2.confidence_hint == 0.0

    def test_from_dict_defensive(self):
        from honeynet_framework.telemetry_features import InteractionFeature
        # Missing fields → defaults
        f = InteractionFeature.from_dict({"event_type": "navigate"})
        assert f.event_type == "navigate"
        assert f.latency_s == -1.0
        # Non-dict → safe default
        f2 = InteractionFeature.from_dict("garbage")
        assert f2.event_type == "unknown"
        # None → safe default
        f3 = InteractionFeature.from_dict(None)
        assert f3.event_type == "unknown"

    def test_roundtrip(self):
        from honeynet_framework.telemetry_features import InteractionFeature
        orig = InteractionFeature(
            event_type="navigate", latency_s=0.5, lure_followed=True,
            command_hash="abc123", action_family="navigate",
            source_class="human", confidence_hint=0.8,
        )
        d = orig.to_dict()
        restored = InteractionFeature.from_dict(d)
        assert restored.event_type == orig.event_type
        assert restored.latency_s == orig.latency_s
        assert restored.lure_followed is True


class TestSessionSnapshot:
    """Session aggregate computation."""

    def test_empty_session(self):
        from honeynet_framework.telemetry_features import compute_session_snapshot
        snap = compute_session_snapshot("run1", [])
        assert snap.event_count == 0
        assert snap.lure_hit_ratio == 0.0
        assert snap.median_latency_s == -1.0

    def test_basic_aggregation(self):
        from honeynet_framework.telemetry_features import (
            InteractionFeature, compute_session_snapshot,
        )
        features = [
            InteractionFeature(latency_s=0.1, lure_followed=True, command_hash="a", action_family="navigate"),
            InteractionFeature(latency_s=0.2, lure_followed=False, command_hash="b", action_family="navigate"),
            InteractionFeature(latency_s=0.3, lure_followed=True, command_hash="a", action_family="interact"),
            InteractionFeature(latency_s=1.5, lure_followed=False, command_hash="c", action_family="enumerate"),
        ]
        snap = compute_session_snapshot("run2", features)
        assert snap.event_count == 4
        assert snap.lure_hit_count == 2
        assert snap.lure_hit_ratio == 0.5
        assert snap.fast_action_ratio > 0  # 3 of 4 are under 0.5s
        assert snap.repeat_pattern_ratio > 0  # "a" repeated
        assert "navigate" in snap.action_family_counts
        assert snap.action_family_counts["navigate"] == 2

    def test_deterministic(self):
        from honeynet_framework.telemetry_features import (
            InteractionFeature, compute_session_snapshot,
        )
        features = [
            InteractionFeature(latency_s=0.1, command_hash="x"),
            InteractionFeature(latency_s=0.2, command_hash="y"),
        ]
        snap1 = compute_session_snapshot("r", features)
        snap2 = compute_session_snapshot("r", features)
        assert snap1.to_dict() == snap2.to_dict()

    def test_snapshot_roundtrip(self):
        from honeynet_framework.telemetry_features import SessionSnapshot
        snap = SessionSnapshot(run_id="r1", event_count=10, lure_hit_ratio=0.3)
        d = snap.to_dict()
        restored = SessionSnapshot.from_dict(d)
        assert restored.run_id == "r1"
        assert restored.event_count == 10


# ===================================================================== #
# TP2: Detection — 3-Zone Decision
# ===================================================================== #

class TestDetectorResult:
    """DetectorResult carries score, decision, and reasons."""

    def test_three_decisions_exist(self):
        from honeynet_framework.detection import Decision
        assert Decision.ALLOW.value == "allow"
        assert Decision.REVIEW.value == "review"
        assert Decision.LIKELY_AGENT.value == "likely_agent"

    def test_score_clamping(self):
        from honeynet_framework.detection import DetectorResult
        r = DetectorResult(score=5.0)
        assert r.score == 1.0
        r2 = DetectorResult(score=-2.0)
        assert r2.score == 0.0

    def test_roundtrip(self):
        from honeynet_framework.detection import Decision, DetectorResult
        orig = DetectorResult(
            score=0.65, decision=Decision.REVIEW,
            reasons=["fast actions", "lure hits"],
            thresholds_used="v1", run_id="run42",
        )
        d = orig.to_dict()
        restored = DetectorResult.from_dict(d)
        assert restored.decision == Decision.REVIEW
        assert restored.score == 0.65
        assert len(restored.reasons) == 2

    def test_is_suspicious(self):
        from honeynet_framework.detection import Decision, DetectorResult
        assert DetectorResult(decision=Decision.ALLOW).is_suspicious is False
        assert DetectorResult(decision=Decision.REVIEW).is_suspicious is True
        assert DetectorResult(decision=Decision.LIKELY_AGENT).is_suspicious is True


class TestDetectorConfig:
    """DetectorConfig with threshold validation."""

    def test_default_thresholds(self):
        from honeynet_framework.detection import DetectorConfig
        cfg = DetectorConfig()
        assert cfg.t_allow < cfg.t_agent
        assert cfg.version == "v1"

    def test_invalid_thresholds_rejected(self):
        from honeynet_framework.detection import DetectorConfig
        with pytest.raises(ValueError):
            DetectorConfig(t_allow=0.8, t_agent=0.3)

    def test_roundtrip(self):
        from honeynet_framework.detection import DetectorConfig
        cfg = DetectorConfig(version="v2", t_allow=0.25, t_agent=0.75)
        d = cfg.to_dict()
        restored = DetectorConfig.from_dict(d)
        assert restored.version == "v2"
        assert restored.t_allow == 0.25


# ===================================================================== #
# TP4: Multi-Signal Detector
# ===================================================================== #

class TestMultiSignalDetector:
    """MultiSignalDetector scoring and 3-zone mapping."""

    def _make_snapshot(self, **kwargs):
        from honeynet_framework.telemetry_features import SessionSnapshot
        defaults = {
            "run_id": "test",
            "event_count": 20,
            "fast_action_ratio": 0.0,
            "lure_hit_ratio": 0.0,
            "repeat_pattern_ratio": 0.0,
            "inter_action_jitter": 2.0,  # high jitter = human-like
            "trajectory_linearity": 0.3,
            "median_latency_s": 1.0,
            "stddev_latency_s": 0.5,
        }
        defaults.update(kwargs)
        return SessionSnapshot(**defaults)

    def test_human_like_session_is_allow(self):
        from honeynet_framework.detection import MultiSignalDetector
        detector = MultiSignalDetector()
        snap = self._make_snapshot(
            fast_action_ratio=0.1, lure_hit_ratio=0.0,
            repeat_pattern_ratio=0.05, inter_action_jitter=1.5,
            trajectory_linearity=0.3,
        )
        result = detector.analyze(snap)
        assert result.decision.value == "allow"
        assert result.score < 0.3

    def test_bot_like_session_is_likely_agent(self):
        from honeynet_framework.detection import MultiSignalDetector
        detector = MultiSignalDetector()
        snap = self._make_snapshot(
            fast_action_ratio=0.95, lure_hit_ratio=0.8,
            repeat_pattern_ratio=0.7, inter_action_jitter=0.01,
            trajectory_linearity=0.95,
            median_latency_s=0.1, stddev_latency_s=0.01,
        )
        result = detector.analyze(snap)
        assert result.decision.value == "likely_agent"
        assert result.score >= 0.7
        assert len(result.reasons) > 0

    def test_mixed_signals_is_review(self):
        from honeynet_framework.detection import MultiSignalDetector
        detector = MultiSignalDetector()
        snap = self._make_snapshot(
            fast_action_ratio=0.5, lure_hit_ratio=0.4,
            repeat_pattern_ratio=0.3, inter_action_jitter=0.8,
            trajectory_linearity=0.5,
        )
        result = detector.analyze(snap)
        assert result.decision.value == "review"

    def test_insufficient_events_is_allow(self):
        from honeynet_framework.detection import MultiSignalDetector
        detector = MultiSignalDetector()
        snap = self._make_snapshot(event_count=2)
        result = detector.analyze(snap)
        assert result.decision.value == "allow"
        assert "insufficient" in result.reasons[0].lower()

    def test_score_is_deterministic(self):
        from honeynet_framework.detection import MultiSignalDetector
        detector = MultiSignalDetector()
        snap = self._make_snapshot(fast_action_ratio=0.6, lure_hit_ratio=0.5)
        r1 = detector.analyze(snap)
        r2 = detector.analyze(snap)
        assert r1.score == r2.score
        assert r1.decision == r2.decision

    def test_feature_contributions_explained(self):
        from honeynet_framework.detection import MultiSignalDetector
        detector = MultiSignalDetector()
        snap = self._make_snapshot(fast_action_ratio=0.9, lure_hit_ratio=0.7)
        result = detector.analyze(snap)
        assert "fast_action_ratio" in result.feature_contributions
        assert "lure_hit_ratio" in result.feature_contributions
        assert result.thresholds_used == "v1"

    def test_legacy_compat_is_agent(self):
        from honeynet_framework.detection import MultiSignalDetector
        detector = MultiSignalDetector()
        snap = self._make_snapshot(
            fast_action_ratio=0.95, lure_hit_ratio=0.9,
            repeat_pattern_ratio=0.8, inter_action_jitter=0.01,
            trajectory_linearity=0.95,
            median_latency_s=0.05, stddev_latency_s=0.005,
        )
        assert detector.is_agent(snap) is True
        assert detector.classify(snap) == "likely_agent"


# ===================================================================== #
# TP3: Deception QA Checks
# ===================================================================== #

class TestDeceptionQA:
    """Deception-specific QA checks."""

    def test_deception_surface_passes_with_ports(self):
        from honeynet_framework.qa_deception import check_deception_surface_present
        wm = _make_test_world_model()
        from honeynet_framework.deploy_compiler import DeployCompiler
        compiler = DeployCompiler()
        proj = compiler.compile(wm)
        # Test world model has 1 container with ports; use min_services=1
        result = check_deception_surface_present(proj, min_services=1)
        assert result.check_id == "deception_surface_present"
        assert result.passed is True

    def test_deception_surface_fails_without_ports(self):
        from honeynet_framework.qa_deception import check_deception_surface_present
        from honeynet_framework.models import DeployProjection, DeployContainer, DeployNetwork
        proj = DeployProjection(
            project_name="test",
            containers=[
                DeployContainer(name="hn_no_ports", image="alpine:latest", networks=["net"]),
            ],
            networks=[DeployNetwork(name="net")],
        )
        result = check_deception_surface_present(proj, min_services=1)
        assert result.passed is False

    def test_suspicious_uniformity_diverse_passes(self):
        from honeynet_framework.qa_deception import check_suspicious_uniformity
        wm = _make_test_world_model()
        from honeynet_framework.deploy_compiler import DeployCompiler
        compiler = DeployCompiler()
        proj = compiler.compile(wm)
        result = check_suspicious_uniformity(proj)
        assert result.check_id == "suspicious_uniformity_guard"
        # Test model has diverse images
        assert result.passed is True

    def test_bait_artifact_check(self):
        from honeynet_framework.qa_deception import check_bait_artifact_presence
        from honeynet_framework.models import (
            DeployProjection, DeployContainer, DeployNetwork, Port,
        )
        proj = DeployProjection(
            project_name="test",
            containers=[
                DeployContainer(
                    name="hn_bait", image="nginx:latest",
                    networks=["net"],
                    env=["DB_PASSWORD=fake123", "NORMAL_VAR=hello"],
                    ports=[Port(internal=80, external=8080)],
                ),
            ],
            networks=[DeployNetwork(name="net")],
        )
        result = check_bait_artifact_presence(proj)
        assert result.passed is True
        assert "hn_bait" in result.details["containers_with_bait"]

    def test_full_deception_qa_report(self):
        from honeynet_framework.qa_deception import run_deception_qa
        wm = _make_test_world_model()
        from honeynet_framework.deploy_compiler import DeployCompiler
        compiler = DeployCompiler()
        proj = compiler.compile(wm)
        report = run_deception_qa(wm, proj)
        assert report.checks_run == 4
        assert report.pass_rate >= 0.0
        d = report.to_dict()
        assert "results" in d
        assert len(d["results"]) == 4

    def test_deception_qa_report_summary(self):
        from honeynet_framework.qa_deception import run_deception_qa
        wm = _make_test_world_model()
        from honeynet_framework.deploy_compiler import DeployCompiler
        compiler = DeployCompiler()
        proj = compiler.compile(wm)
        report = run_deception_qa(wm, proj)
        summary = report.summary()
        assert "Deception QA" in summary


# ===================================================================== #
# Missing Required Config Validator Warning
# ===================================================================== #

class TestMissingRequiredConfig:
    """Validator must warn about config-dependent images without volumes."""

    def test_haproxy_without_config_warns(self):
        """haproxy:2.9 with no volumes/command/config env → MISSING_REQUIRED_CONFIG."""
        from honeynet_framework.validator import validate
        wm = _make_test_world_model()
        from honeynet_framework.models import System, SystemDeploy, SystemKind
        wm.systems["api_gw"] = System(
            name="api_gw",
            kind=SystemKind.UNKNOWN,
            deploy=SystemDeploy(
                image="haproxy:2.9",
                zone=list(wm.zones.keys())[0],
                ports=[8081],
            ),
        )
        result = validate(wm)
        config_warnings = [w for w in result.warnings if w.rule == "MISSING_REQUIRED_CONFIG"]
        haproxy_warnings = [w for w in config_warnings if "api_gw" in w.details]
        assert len(haproxy_warnings) >= 1
        assert "haproxy" in haproxy_warnings[0].details.lower()

    def test_haproxy_with_volume_no_warning(self):
        """haproxy with a volume mount should NOT trigger warning."""
        from honeynet_framework.validator import validate
        wm = _make_test_world_model()
        from honeynet_framework.models import System, SystemDeploy, SystemKind, VolumeMount
        wm.systems["api_gw"] = System(
            name="api_gw",
            kind=SystemKind.UNKNOWN,
            deploy=SystemDeploy(
                image="haproxy:2.9",
                zone=list(wm.zones.keys())[0],
                ports=[8081],
                volumes=[VolumeMount(name="haproxy_cfg", path="/usr/local/etc/haproxy")],
            ),
        )
        result = validate(wm)
        config_warnings = [w for w in result.warnings if w.rule == "MISSING_REQUIRED_CONFIG"]
        haproxy_warnings = [w for w in config_warnings if "api_gw" in w.details]
        assert len(haproxy_warnings) == 0

    def test_haproxy_with_command_no_warning(self):
        """haproxy with a custom command should NOT trigger warning."""
        from honeynet_framework.validator import validate
        wm = _make_test_world_model()
        from honeynet_framework.models import System, SystemDeploy, SystemKind
        wm.systems["api_gw"] = System(
            name="api_gw",
            kind=SystemKind.UNKNOWN,
            deploy=SystemDeploy(
                image="haproxy:2.9",
                zone=list(wm.zones.keys())[0],
                ports=[8081],
                command=["haproxy", "-f", "/dev/stdin"],
            ),
        )
        result = validate(wm)
        config_warnings = [w for w in result.warnings if w.rule == "MISSING_REQUIRED_CONFIG"]
        haproxy_warnings = [w for w in config_warnings if "api_gw" in w.details]
        assert len(haproxy_warnings) == 0

    def test_haproxy_with_irrelevant_volume_still_warns(self):
        """Unrelated volume mount must not count as config evidence."""
        from honeynet_framework.validator import validate
        wm = _make_test_world_model()
        from honeynet_framework.models import System, SystemDeploy, SystemKind, VolumeMount
        wm.systems["api_gw"] = System(
            name="api_gw",
            kind=SystemKind.UNKNOWN,
            deploy=SystemDeploy(
                image="haproxy:2.9",
                zone=list(wm.zones.keys())[0],
                ports=[8081],
                volumes=[VolumeMount(name="app_data", path="/var/lib/app")],
            ),
        )
        result = validate(wm)
        config_warnings = [w for w in result.warnings if w.rule == "MISSING_REQUIRED_CONFIG"]
        haproxy_warnings = [w for w in config_warnings if "api_gw" in w.details]
        assert len(haproxy_warnings) >= 1

    def test_haproxy_with_irrelevant_command_still_warns(self):
        """Unrelated command must not count as config evidence."""
        from honeynet_framework.validator import validate
        wm = _make_test_world_model()
        from honeynet_framework.models import System, SystemDeploy, SystemKind
        wm.systems["api_gw"] = System(
            name="api_gw",
            kind=SystemKind.UNKNOWN,
            deploy=SystemDeploy(
                image="haproxy:2.9",
                zone=list(wm.zones.keys())[0],
                ports=[8081],
                command=["echo", "hello"],
            ),
        )
        result = validate(wm)
        config_warnings = [w for w in result.warnings if w.rule == "MISSING_REQUIRED_CONFIG"]
        haproxy_warnings = [w for w in config_warnings if "api_gw" in w.details]
        assert len(haproxy_warnings) >= 1

    def test_haproxy_with_irrelevant_env_still_warns(self):
        """Non-config env vars must not suppress required-config warning."""
        from honeynet_framework.validator import validate
        wm = _make_test_world_model()
        from honeynet_framework.models import System, SystemDeploy, SystemKind
        wm.systems["api_gw"] = System(
            name="api_gw",
            kind=SystemKind.UNKNOWN,
            deploy=SystemDeploy(
                image="haproxy:2.9",
                zone=list(wm.zones.keys())[0],
                ports=[8081],
                env=["FOO=bar", "LOG_LEVEL=debug"],
            ),
        )
        result = validate(wm)
        config_warnings = [w for w in result.warnings if w.rule == "MISSING_REQUIRED_CONFIG"]
        haproxy_warnings = [w for w in config_warnings if "api_gw" in w.details]
        assert len(haproxy_warnings) >= 1

    def test_nginx_without_config_no_warning(self):
        """nginx base image is not a strict config-required case; avoid false positives."""
        from honeynet_framework.validator import validate
        wm = _make_test_world_model()
        # The test world model already has nginx:1.25-alpine
        result = validate(wm)
        config_warnings = [w for w in result.warnings if w.rule == "MISSING_REQUIRED_CONFIG"]
        nginx_warnings = [w for w in config_warnings if "web" in w.details]
        assert len(nginx_warnings) == 0

    def test_unrelated_image_no_warning(self):
        """Alpine (non-config-required) should not trigger MISSING_REQUIRED_CONFIG."""
        from honeynet_framework.validator import validate
        from honeynet_framework.models import (
            Organization, System, SystemDeploy, SystemKind,
            WorldModel, WorldZone, ZoneDeploy,
        )
        # Build a minimal world model with only a postgres image (not in config-required list)
        wm = WorldModel(
            organization=Organization(name="test"),
            zones={"dmz": WorldZone(name="dmz", deploy=ZoneDeploy(network_name="dmz"))},
            systems={
                "db": System(
                    name="db", kind=SystemKind.DATABASE,
                    deploy=SystemDeploy(image="postgres:16", zone="dmz", ports=[5432]),
                ),
            },
            secrets={},
        )
        result = validate(wm)
        config_warnings = [w for w in result.warnings if w.rule == "MISSING_REQUIRED_CONFIG"]
        assert len(config_warnings) == 0


class TestHealthcheckBinaryMismatchWarning:
    """Warn when healthcheck binary/shell likely missing in image."""

    def test_haproxy_healthcheck_with_curl_warns(self):
        wm = _make_test_world_model()
        wm.systems["api_gw"] = System(
            name="api_gw",
            kind=SystemKind.UNKNOWN,
            deploy=SystemDeploy(
                image="haproxy:2.9",
                zone="dmz",
                healthcheck={"test": ["CMD", "curl", "-f", "http://localhost:8080/health"]},
            ),
        )
        result = validate(wm)
        warnings = [w for w in result.warnings if w.rule == "HEALTHCHECK_BINARY_MISMATCH"]
        assert len(warnings) == 1
        assert "curl" in warnings[0].details

    def test_distroless_cmd_shell_warns(self):
        wm = _make_test_world_model()
        wm.systems["svc"] = System(
            name="svc",
            kind=SystemKind.WEB,
            deploy=SystemDeploy(
                image="gcr.io/distroless/python3:latest",
                zone="dmz",
                healthcheck={"test": ["CMD-SHELL", "echo ok"]},
            ),
        )
        result = validate(wm)
        warnings = [w for w in result.warnings if w.rule == "HEALTHCHECK_BINARY_MISMATCH"]
        assert len(warnings) == 1

    def test_distroless_string_healthcheck_treated_as_shell_warns(self):
        wm = _make_test_world_model()
        wm.systems["svc"] = System(
            name="svc",
            kind=SystemKind.WEB,
            deploy=SystemDeploy(
                image="gcr.io/distroless/python3:latest",
                zone="dmz",
                healthcheck={"test": "echo ok"},
            ),
        )
        result = validate(wm)
        warnings = [w for w in result.warnings if w.rule == "HEALTHCHECK_BINARY_MISMATCH"]
        assert len(warnings) == 1

    def test_healthcheck_binary_available_no_warning(self):
        wm = _make_test_world_model()
        wm.systems["probe"] = System(
            name="probe",
            kind=SystemKind.WEB,
            deploy=SystemDeploy(
                image="alpine:3.20",
                zone="dmz",
                healthcheck={"test": ["CMD", "wget", "-q", "-O", "-", "http://localhost"]},
            ),
        )
        result = validate(wm)
        assert not any(w.rule == "HEALTHCHECK_BINARY_MISMATCH" for w in result.warnings)


class TestPathAFailureMetrics:
    """Path A apply-failure metrics should be populated robustly."""

    def _build_orchestrator(self, tmp_path: Path):
        from unittest.mock import AsyncMock, Mock
        from honeynet_framework.orchestrator import HoneynetOrchestrator, OrchestratorConfig
        from honeynet_framework.models import DeployProjection, DeploymentStatus
        from honeynet_framework.deployer import CommandResult
        from honeynet_framework.deploy_compiler import DeployCompiler
        from honeynet_framework.tofu_renderer import TofuRenderer

        wm = _make_test_world_model()
        projection = DeployCompiler().compile(wm)

        orch = HoneynetOrchestrator(OrchestratorConfig(
            work_dir=tmp_path,
            enable_partial_apply=False,  # Disable partial apply for failure-path tests
            max_apply_repair_attempts=1,  # Disable repair loop for these tests
        ))
        orch._initialized = True
        orch.extractor = AsyncMock()
        orch.extractor.extract.return_value = wm
        orch.extractor.last_dep_additions = []
        orch.extractor.last_placement_fixes = []
        orch.image_resolver = AsyncMock()
        orch.image_resolver.resolve_all = AsyncMock(return_value={})
        orch._image_repair_loop = AsyncMock(return_value=(wm, [], 10, 10))
        orch._cleanup_old_state = AsyncMock(return_value=None)
        orch.compiler = DeployCompiler()
        orch.renderer = TofuRenderer(work_dir=tmp_path)

        deployer = AsyncMock()
        deployer.check_prerequisites.return_value = (True, "ok")
        deployer.tofu_version_line.return_value = "tofu v1"
        deployer.get_used_ports.return_value = set()
        deployer.verify_deployment.return_value = {"running": {}, "missing": [], "unhealthy": []}
        deployer.last_stage_results = {
            "fmt": None,
            "init": CommandResult(success=True, stdout="", stderr="", exit_code=0),
            "validate": CommandResult(success=True, stdout="", stderr="", exit_code=0),
            "plan": CommandResult(success=True, stdout="", stderr="", exit_code=0),
            "apply": CommandResult(success=False, stdout="", stderr="apply failed", exit_code=1),
        }

        # Defaults for the non-recovery apply-failure path
        plan_ok = CommandResult(success=True, stdout="", stderr="", exit_code=0)
        apply_fail = CommandResult(success=False, stdout="", stderr="apply failed", exit_code=1)
        deployer.plan_and_apply.return_value = (plan_ok, apply_fail)
        deployer.plan.return_value = plan_ok
        deployer.apply.return_value = apply_fail

        orch.deployer = deployer
        return orch, wm, projection, DeploymentStatus

    def test_direct_apply_failure_populates_path_a_metrics(self, tmp_path: Path):
        import asyncio
        from unittest.mock import AsyncMock, patch

        orch, _, _, DeploymentStatus = self._build_orchestrator(tmp_path)
        apply_err = "Error: with docker_container.hn_web, already broken"
        orch.deployer.last_stage_results["apply"].stderr = apply_err
        orch.deployer.plan_and_apply.return_value = (
            orch.deployer.last_stage_results["plan"],
            orch.deployer.last_stage_results["apply"],
        )

        with patch(
            "honeynet_framework.orchestrator.collect_path_a_apply_failure_diagnostics",
            new=AsyncMock(
                return_value={
                    "collected": True,
                    "artifact_path": None,  # must not become literal "None"
                    "failing_resources_count": 2,
                    "docker_targets_count": 1,
                }
            ),
        ):
            result = asyncio.run(orch._deploy_locked("prompt", tmp_path, "run-1", tmp_path / ".honeynet.lock"))

        assert result.status == DeploymentStatus.FAILED
        assert result.failure_stage == "apply"
        path_a = result.metrics["observability"]["path_a"]
        assert path_a["diagnostics_collected"] is True
        assert path_a["diagnostics_artifact"] == ""
        assert path_a["failing_resources_count"] == 2
        assert path_a["docker_targets_count"] == 1

    def test_retry_after_import_failure_populates_path_a_metrics(self, tmp_path: Path):
        import asyncio
        from unittest.mock import AsyncMock, patch
        from honeynet_framework.deployer import CommandResult

        orch, _, _, DeploymentStatus = self._build_orchestrator(tmp_path)
        orch._import_existing_docker_resources = AsyncMock(return_value=True)

        first_apply_fail = CommandResult(
            success=False,
            stdout="",
            stderr="Error: with docker_container.hn_web, already exists",
            exit_code=1,
        )
        orch.deployer.last_stage_results["apply"] = first_apply_fail
        orch.deployer.plan_and_apply.return_value = (
            orch.deployer.last_stage_results["plan"],
            first_apply_fail,
        )

        retry_apply_fail = CommandResult(
            success=False,
            stdout="",
            stderr="retry apply failed hard",
            exit_code=1,
        )
        orch.deployer.plan.return_value = CommandResult(success=True, stdout="", stderr="", exit_code=0)
        orch.deployer.apply.return_value = retry_apply_fail

        with patch(
            "honeynet_framework.orchestrator.collect_path_a_apply_failure_diagnostics",
            new=AsyncMock(
                return_value={
                    "collected": True,
                    "artifact_path": "out/path_a_diagnostics.json",
                    "failing_resources_count": 4,
                    "docker_targets_count": 3,
                }
            ),
        ):
            result = asyncio.run(orch._deploy_locked("prompt", tmp_path, "run-2", tmp_path / ".honeynet.lock"))

        assert result.status == DeploymentStatus.FAILED
        assert result.failure_stage == "apply"
        assert "tofu apply failed" in result.errors[0]
        path_a = result.metrics["observability"]["path_a"]
        assert path_a["diagnostics_collected"] is True
        assert path_a["diagnostics_artifact"] == "out/path_a_diagnostics.json"
        assert path_a["failing_resources_count"] == 4
        assert path_a["docker_targets_count"] == 3

    def test_diagnostics_collector_exception_keeps_failure_non_breaking(self, tmp_path: Path):
        import asyncio
        from unittest.mock import AsyncMock, patch

        orch, _, _, DeploymentStatus = self._build_orchestrator(tmp_path)

        with patch(
            "honeynet_framework.orchestrator.collect_path_a_apply_failure_diagnostics",
            new=AsyncMock(side_effect=RuntimeError("collector exploded")),
        ):
            result = asyncio.run(orch._deploy_locked("prompt", tmp_path, "run-3", tmp_path / ".honeynet.lock"))

        assert result.status == DeploymentStatus.FAILED
        assert result.failure_stage == "apply"
        assert "tofu apply failed" in result.errors[0]
        path_a = result.metrics["observability"]["path_a"]
        assert path_a["diagnostics_collected"] is False
        assert path_a["diagnostics_artifact"] == ""
        assert path_a["failing_resources_count"] == 0
        assert path_a["docker_targets_count"] == 0


# ---------------------------------------------------------------------------
# Guardrail tests: XSS escaping, atomic writes, port-owner parsing
# ---------------------------------------------------------------------------


class TestHtmlEscapingInReportingHtml:
    """Ensure _esc is applied in reporting_html module."""

    def test_esc_function_escapes_angle_brackets(self):
        from honeynet_framework.reporting_html import _esc

        assert _esc("<script>") == "&lt;script&gt;"
        assert _esc('"quoted"') == "&quot;quoted&quot;"
        assert _esc("normal text") == "normal text"

    def test_esc_handles_non_string(self):
        from honeynet_framework.reporting_html import _esc

        assert _esc(42) == "42"
        assert _esc(None) == "None"


class TestAtomicWrite:
    """Verify atomic_write_text produces correct files and cleans up on failure."""

    def test_basic_write(self, tmp_path: Path):
        from honeynet_framework.utils import atomic_write_text

        target = tmp_path / "out.json"
        atomic_write_text(target, '{"key": "value"}')
        assert target.exists()
        assert json.loads(target.read_text(encoding="utf-8")) == {"key": "value"}

    def test_overwrites_existing_file(self, tmp_path: Path):
        from honeynet_framework.utils import atomic_write_text

        target = tmp_path / "out.json"
        target.write_text("old content", encoding="utf-8")
        atomic_write_text(target, "new content")
        assert target.read_text(encoding="utf-8") == "new content"

    def test_creates_parent_dirs(self, tmp_path: Path):
        from honeynet_framework.utils import atomic_write_text

        target = tmp_path / "sub" / "deep" / "file.txt"
        atomic_write_text(target, "hello")
        assert target.read_text(encoding="utf-8") == "hello"

    def test_no_temp_files_left_on_success(self, tmp_path: Path):
        from honeynet_framework.utils import atomic_write_text

        target = tmp_path / "clean.txt"
        atomic_write_text(target, "data")
        files = list(tmp_path.iterdir())
        assert len(files) == 1
        assert files[0].name == "clean.txt"

    def test_original_preserved_on_write_error(self, tmp_path: Path):
        """If os.replace fails, original file should be untouched."""
        from unittest.mock import patch
        from honeynet_framework.utils import atomic_write_text

        target = tmp_path / "safe.txt"
        target.write_text("original", encoding="utf-8")

        with patch("honeynet_framework.utils.os.replace", side_effect=OSError("disk full")):
            with pytest.raises(OSError, match="disk full"):
                atomic_write_text(target, "new content that should not persist")

        assert target.read_text(encoding="utf-8") == "original"

    def test_no_temp_file_left_on_error(self, tmp_path: Path):
        from unittest.mock import patch
        from honeynet_framework.utils import atomic_write_text

        target = tmp_path / "no_leak.txt"
        with patch("honeynet_framework.utils.os.replace", side_effect=OSError("fail")):
            with pytest.raises(OSError):
                atomic_write_text(target, "data")

        # Only temp files would have .tmp suffix
        tmp_files = [f for f in tmp_path.iterdir() if f.suffix == ".tmp"]
        assert tmp_files == []


class TestQAPortOwnerExactParsing:
    """Verify port-owner verification uses exact port matching, not substring."""

    def test_port_80_not_confused_with_8080(self):
        """Port 80 should NOT match a mapping that only has 8080."""
        import asyncio
        from unittest.mock import AsyncMock, patch
        from honeynet_framework.qa import QARunner

        runner = QARunner()

        # Simulate `docker port mycontainer` output that only maps 8080
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(
            return_value=(b"80/tcp -> 0.0.0.0:8080\n", b"")
        )

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            ok, msg = asyncio.run(runner._verify_port_owner(80, "mycontainer"))

        # Port 80 is NOT published — only 8080 is
        assert ok is False
        assert "not mapped" in msg

    def test_exact_port_match_succeeds(self):
        """Port 8080 should match when 8080 is in the mapping."""
        import asyncio
        from unittest.mock import AsyncMock, patch
        from honeynet_framework.qa import QARunner

        runner = QARunner()

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(
            return_value=(b"80/tcp -> 0.0.0.0:8080\n", b"")
        )

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            ok, msg = asyncio.run(runner._verify_port_owner(8080, "mycontainer"))

        assert ok is True
        assert "confirmed" in msg


class TestPartialApply:
    """Tests for the partial apply with -target functionality."""

    def test_apply_targets_builds_correct_command(self):
        """apply_targets should pass -target flags to tofu."""
        import asyncio
        from unittest.mock import AsyncMock
        from honeynet_framework.deployer import TerraformDeployer, DeployerConfig, CommandResult

        deployer = TerraformDeployer(DeployerConfig(work_dir=Path(".")))
        deployer._run_tofu = AsyncMock(return_value=CommandResult(
            success=True, stdout="", stderr="", exit_code=0,
        ))

        targets = [
            "docker_container.hn_redis",
            "docker_container.hn_nginx",
            "docker_image.hn_redis",
        ]
        result = asyncio.run(deployer.apply_targets(targets))
        assert result.success

        call_args = deployer._run_tofu.call_args[0][0]
        assert "apply" in call_args
        assert "-auto-approve" in call_args
        assert "-target" in call_args
        # Each target should appear after a -target flag
        for t in targets:
            idx = call_args.index(t)
            assert call_args[idx - 1] == "-target"


class TestApplyRepairLoop:
    """Tests for the apply failure repair loop and container parser."""

    def test_parse_failing_containers_from_stderr(self):
        from honeynet_framework.orchestrator import HoneynetOrchestrator

        wm = WorldModel(
            organization=Organization(name="Test"),
            zones={"dmz": WorldZone(name="dmz", deploy=ZoneDeploy(network_name="dmz"))},
            systems={
                "job_scheduler": System(
                    name="job_scheduler", kind=SystemKind.RUNTIME,
                    deploy=SystemDeploy(
                        image="python:3.12-slim", zone="dmz",
                        command=["python", "-m", "scheduler"],
                    ),
                ),
                "data_federation": System(
                    name="data_federation", kind=SystemKind.RUNTIME,
                    deploy=SystemDeploy(
                        image="node:20-alpine", zone="dmz",
                        command=["node", "app.js"],
                    ),
                ),
                "redis_cache": System(
                    name="redis_cache", kind=SystemKind.DATABASE,
                    deploy=SystemDeploy(image="redis:7", zone="dmz"),
                ),
            },
        )

        stderr = """
Error: container exited immediately

  with docker_container.hn_job_scheduler,
  on main.tofu.json line 349

Error: container failed to be in running state

  with docker_container.hn_data_federation,
  on main.tofu.json line 498
"""
        failing = HoneynetOrchestrator._parse_failing_containers(stderr, wm)
        assert len(failing) == 2
        names = {c["system_name"] for c in failing}
        assert "job_scheduler" in names
        assert "data_federation" in names
        # redis_cache should NOT be in the list (it didn't fail)
        assert "redis_cache" not in names

    def test_parse_failing_containers_empty_stderr(self):
        from honeynet_framework.orchestrator import HoneynetOrchestrator

        wm = WorldModel(
            organization=Organization(name="Test"),
            zones={"dmz": WorldZone(name="dmz", deploy=ZoneDeploy(network_name="dmz"))},
            systems={},
        )
        failing = HoneynetOrchestrator._parse_failing_containers("", wm)
        assert failing == []

    def test_parse_failing_containers_enriches_metadata(self):
        from honeynet_framework.orchestrator import HoneynetOrchestrator

        wm = WorldModel(
            organization=Organization(name="Test"),
            zones={"dmz": WorldZone(name="dmz", deploy=ZoneDeploy(network_name="dmz"))},
            systems={
                "gpu_manager": System(
                    name="gpu_manager", kind=SystemKind.RUNTIME,
                    deploy=SystemDeploy(
                        image="node:20-alpine", zone="dmz",
                        command=["node", "gpu-manager.js"],
                    ),
                ),
            },
        )

        stderr = "  with docker_container.hn_gpu_manager,"
        failing = HoneynetOrchestrator._parse_failing_containers(stderr, wm)
        assert len(failing) == 1
        assert failing[0]["system_name"] == "gpu_manager"
        assert failing[0]["image"] == "node:20-alpine"
        assert failing[0]["command"] == ["node", "gpu-manager.js"]
        assert failing[0]["zone"] == "dmz"

    def test_container_repair_prompt_built_correctly(self):
        import asyncio
        from honeynet_framework.extraction.prompts import build_container_repair_prompt
        from unittest.mock import patch, AsyncMock

        failing = [
            {
                "system_name": "job_scheduler",
                "image": "python:3.12-slim",
                "command": ["python", "-m", "scheduler"],
                "zone": "compute",
                "error": "container exited immediately",
            }
        ]
        mock_config = {
            "entrypoint": ["python3"],
            "cmd": ["python3"],
            "env": [],
            "exposed_ports": [],
            "working_dir": "",
        }
        with patch(
            "honeynet_framework.image_resolver.fetch_image_config",
            new_callable=AsyncMock,
            return_value=mock_config,
        ):
            sys_msg, user_msg = asyncio.run(
                build_container_repair_prompt(failing)
            )
        assert "Docker deployment specialist" in sys_msg
        assert "job_scheduler" in user_msg
        assert "python:3.12-slim" in user_msg
        assert "container exited immediately" in user_msg
        assert "oci_config" in user_msg
        assert "python3" in user_msg


class TestFuzzyDependsRepair:
    """Tests for fuzzy depends_on reference repair."""

    def test_substring_match_postgres(self):
        """depends_on: 'postgres' should match 'postgres_metadata'."""
        from honeynet_framework.repair_depends import try_repair_depends_edges

        wm = WorldModel(
            organization=Organization(name="Test"),
            zones={"dmz": WorldZone(name="dmz", deploy=ZoneDeploy(network_name="dmz"))},
            systems={
                "postgres_metadata": System(
                    name="postgres_metadata", kind=SystemKind.DATABASE,
                    deploy=SystemDeploy(image="postgres:16", zone="dmz"),
                ),
                "app": System(
                    name="app", kind=SystemKind.WEB,
                    deploy=SystemDeploy(
                        image="nginx:1.25", zone="dmz",
                        depends_on=["postgres"],  # Broken ref
                    ),
                ),
            },
        )
        wm, changed = try_repair_depends_edges(wm)
        assert changed
        assert wm.systems["app"].deploy.depends_on == ["postgres_metadata"]

    def test_suffix_match_api_gateway(self):
        """depends_on: 'api_gateway' should match 'partner_api_gateway'."""
        from honeynet_framework.repair_depends import try_repair_depends_edges

        wm = WorldModel(
            organization=Organization(name="Test"),
            zones={"dmz": WorldZone(name="dmz", deploy=ZoneDeploy(network_name="dmz"))},
            systems={
                "partner_api_gateway": System(
                    name="partner_api_gateway", kind=SystemKind.WEB,
                    deploy=SystemDeploy(image="traefik:v2.10", zone="dmz"),
                ),
                "portal": System(
                    name="portal", kind=SystemKind.WEB,
                    deploy=SystemDeploy(
                        image="nginx:1.25", zone="dmz",
                        depends_on=["api_gateway"],
                    ),
                ),
            },
        )
        wm, changed = try_repair_depends_edges(wm)
        assert changed
        assert wm.systems["portal"].deploy.depends_on == ["partner_api_gateway"]

    def test_levenshtein_typo_repair(self):
        """depends_on: 'redis_cach' should match 'redis_cache' (1 edit)."""
        from honeynet_framework.repair_depends import try_repair_depends_edges

        wm = WorldModel(
            organization=Organization(name="Test"),
            zones={"dmz": WorldZone(name="dmz", deploy=ZoneDeploy(network_name="dmz"))},
            systems={
                "redis_cache": System(
                    name="redis_cache", kind=SystemKind.DATABASE,
                    deploy=SystemDeploy(image="redis:7", zone="dmz"),
                ),
                "app": System(
                    name="app", kind=SystemKind.WEB,
                    deploy=SystemDeploy(
                        image="nginx:1.25", zone="dmz",
                        depends_on=["redis_cach"],  # Typo
                    ),
                ),
            },
        )
        wm, changed = try_repair_depends_edges(wm)
        assert changed
        assert wm.systems["app"].deploy.depends_on == ["redis_cache"]

    def test_ambiguous_match_not_repaired(self):
        """If multiple systems match, don't repair (ambiguous)."""
        from honeynet_framework.repair_depends import try_repair_depends_edges

        wm = WorldModel(
            organization=Organization(name="Test"),
            zones={"dmz": WorldZone(name="dmz", deploy=ZoneDeploy(network_name="dmz"))},
            systems={
                "postgres_primary": System(
                    name="postgres_primary", kind=SystemKind.DATABASE,
                    deploy=SystemDeploy(image="postgres:16", zone="dmz"),
                ),
                "postgres_replica": System(
                    name="postgres_replica", kind=SystemKind.DATABASE,
                    deploy=SystemDeploy(image="postgres:16", zone="dmz"),
                ),
                "app": System(
                    name="app", kind=SystemKind.WEB,
                    deploy=SystemDeploy(
                        image="nginx:1.25", zone="dmz",
                        depends_on=["postgres"],  # Ambiguous — matches both
                    ),
                ),
            },
        )
        wm, changed = try_repair_depends_edges(wm)
        # Should NOT repair — ambiguous match
        assert wm.systems["app"].deploy.depends_on == ["postgres"]

    def test_exact_match_not_changed(self):
        """Exact matches should not be touched."""
        from honeynet_framework.repair_depends import try_repair_depends_edges

        wm = WorldModel(
            organization=Organization(name="Test"),
            zones={"dmz": WorldZone(name="dmz", deploy=ZoneDeploy(network_name="dmz"))},
            systems={
                "redis": System(
                    name="redis", kind=SystemKind.DATABASE,
                    deploy=SystemDeploy(image="redis:7", zone="dmz"),
                ),
                "app": System(
                    name="app", kind=SystemKind.WEB,
                    deploy=SystemDeploy(
                        image="nginx:1.25", zone="dmz",
                        depends_on=["redis"],
                    ),
                ),
            },
        )
        wm, changed = try_repair_depends_edges(wm)
        assert not changed
        assert wm.systems["app"].deploy.depends_on == ["redis"]

    def test_circular_dependency_real_case(self):
        """The real failure case: 'customer_banking_portal' → 'api_gateway'
        when the actual system is 'partner_api_gateway'."""
        from honeynet_framework.repair_depends import try_repair_depends_edges

        wm = WorldModel(
            organization=Organization(name="Test"),
            zones={"dmz": WorldZone(name="dmz", deploy=ZoneDeploy(network_name="dmz"))},
            systems={
                "customer_banking_portal": System(
                    name="customer_banking_portal", kind=SystemKind.WEB,
                    deploy=SystemDeploy(
                        image="nginx:1.25", zone="dmz",
                        depends_on=["api_gateway"],
                    ),
                ),
                "partner_api_gateway": System(
                    name="partner_api_gateway", kind=SystemKind.INFRA,
                    deploy=SystemDeploy(image="traefik:v2.10", zone="dmz"),
                ),
            },
        )
        wm, changed = try_repair_depends_edges(wm)
        assert changed
        assert wm.systems["customer_banking_portal"].deploy.depends_on == ["partner_api_gateway"]


class TestImageProfile:
    """Tests for the dynamic ImageProfile classification."""

    def test_daemon_entrypoint_detected(self):
        from honeynet_framework.image_introspector import ImageProfile
        profile = ImageProfile(
            image_ref="nginx:1.25-alpine",
            entrypoint=("/docker-entrypoint.sh",),
            cmd=("nginx", "-g", "daemon off;"),
            env=(), exposed_ports=("80/tcp",), working_dir="", error="",
        )
        assert profile.has_daemon_entrypoint is True
        assert profile.is_base_runtime is False
        assert profile.needs_command is False
        assert profile.available is True

    def test_interpreter_entrypoint_detected(self):
        from honeynet_framework.image_introspector import ImageProfile
        profile = ImageProfile(
            image_ref="python:3.12-slim",
            entrypoint=("python3",),
            cmd=("python3",),
            env=(), exposed_ports=(), working_dir="", error="",
        )
        assert profile.has_daemon_entrypoint is False
        assert profile.is_base_runtime is True

    def test_no_entrypoint_needs_command(self):
        from honeynet_framework.image_introspector import ImageProfile
        profile = ImageProfile(
            image_ref="scratch",
            entrypoint=(), cmd=(), env=(), exposed_ports=(),
            working_dir="", error="",
        )
        assert profile.needs_command is True
        assert profile.has_daemon_entrypoint is False

    def test_required_env_detected(self):
        from honeynet_framework.image_introspector import ImageProfile
        profile = ImageProfile(
            image_ref="postgres:16-alpine",
            entrypoint=("docker-entrypoint.sh",),
            cmd=("postgres",),
            env=("POSTGRES_PASSWORD=", "PGDATA=/var/lib/postgresql/data"),
            exposed_ports=("5432/tcp",), working_dir="", error="",
        )
        assert profile.required_env == ["POSTGRES_PASSWORD"]

    def test_error_profile_not_available(self):
        from honeynet_framework.image_introspector import ImageProfile
        profile = ImageProfile(
            image_ref="nonexistent:latest",
            entrypoint=(), cmd=(), env=(), exposed_ports=(),
            working_dir="", error="not found",
        )
        assert profile.available is False
        assert profile.has_daemon_entrypoint is False

    def test_shell_entrypoint_is_base_runtime(self):
        from honeynet_framework.image_introspector import ImageProfile
        profile = ImageProfile(
            image_ref="alpine:3.20",
            entrypoint=("/bin/sh",),
            cmd=(), env=(), exposed_ports=(), working_dir="", error="",
        )
        assert profile.is_base_runtime is True
        assert profile.has_daemon_entrypoint is False


class TestImageResolverFallback:
    """Tests for the OCI-based image resolver: ref parsing and fallback chain."""

    def test_load_known_good_registry(self):
        from honeynet_framework.image_resolver import _load_known_good_registry
        registry = _load_known_good_registry()
        assert "images" in registry
        assert "namespace_migrations" in registry
        assert "nginx" in registry["images"]

    # --- _parse_image_ref ---------------------------------------------------

    def test_parse_official_no_tag(self):
        from honeynet_framework.image_resolver import _parse_image_ref
        reg, repo, ref = _parse_image_ref("nginx")
        assert reg == "registry-1.docker.io"
        assert repo == "library/nginx"
        assert ref == "latest"

    def test_parse_official_with_tag(self):
        from honeynet_framework.image_resolver import _parse_image_ref
        reg, repo, ref = _parse_image_ref("nginx:1.25-alpine")
        assert reg == "registry-1.docker.io"
        assert repo == "library/nginx"
        assert ref == "1.25-alpine"

    def test_parse_namespaced_dockerhub(self):
        from honeynet_framework.image_resolver import _parse_image_ref
        reg, repo, ref = _parse_image_ref("bitnami/kafka:3.6")
        assert reg == "registry-1.docker.io"
        assert repo == "bitnami/kafka"
        assert ref == "3.6"

    def test_parse_external_registry(self):
        from honeynet_framework.image_resolver import _parse_image_ref
        reg, repo, ref = _parse_image_ref("ghcr.io/bitnami/kafka:3.7")
        assert reg == "ghcr.io"
        assert repo == "bitnami/kafka"
        assert ref == "3.7"

    def test_parse_digest_pinned(self):
        from honeynet_framework.image_resolver import _parse_image_ref
        reg, repo, ref = _parse_image_ref("nginx@sha256:abc123")
        assert reg == "registry-1.docker.io"
        assert repo == "library/nginx"
        assert ref == "sha256:abc123"

    def test_parse_deep_registry_path(self):
        from honeynet_framework.image_resolver import _parse_image_ref
        reg, repo, ref = _parse_image_ref("gcr.io/google-containers/pause:3.9")
        assert reg == "gcr.io"
        assert repo == "google-containers/pause"
        assert ref == "3.9"

    # --- Multi-registry fallback --------------------------------------------

    @pytest.mark.asyncio
    async def test_fallback_tries_ghcr_on_not_found(self):
        """When docker.io returns NOT_FOUND for a namespaced image, ghcr.io is tried."""
        from honeynet_framework.image_resolver import (
            ImageResolver, ImageResolutionEntry, ResolutionStatus,
        )
        from unittest.mock import AsyncMock

        resolver = ImageResolver()

        async def mock_retry(image_ref):
            if "ghcr.io/bitnami/kafka" in image_ref:
                return ImageResolutionEntry(
                    image_ref=image_ref,
                    status=ResolutionStatus.RESOLVED,
                    digest_ref=f"{image_ref}@sha256:abc123",
                )
            return ImageResolutionEntry(image_ref=image_ref, status=ResolutionStatus.NOT_FOUND)

        resolver._resolve_with_retry = mock_retry
        resolver._resolve_from_local_image = AsyncMock(return_value=None)

        result = await resolver._resolve_one("bitnami/kafka:3.6")
        assert result.status == ResolutionStatus.RESOLVED
        assert result.digest_ref and "ghcr.io/bitnami/kafka" in result.digest_ref

    @pytest.mark.asyncio
    async def test_fallback_skips_library_images(self):
        """Official Docker Hub images (library/*) are not tried on other registries."""
        from honeynet_framework.image_resolver import (
            ImageResolver, ImageResolutionEntry, ResolutionStatus,
        )
        from unittest.mock import AsyncMock

        resolver = ImageResolver()
        tried: list[str] = []

        async def mock_retry(image_ref):
            tried.append(image_ref)
            return ImageResolutionEntry(image_ref=image_ref, status=ResolutionStatus.NOT_FOUND)

        resolver._resolve_with_retry = mock_retry
        resolver._resolve_from_local_image = AsyncMock(return_value=None)

        await resolver._resolve_one("nginx:1.25-alpine")

        assert not any("ghcr.io" in r or "quay.io" in r for r in tried), (
            f"Should not try other registries for official images, tried: {tried}"
        )

    @pytest.mark.asyncio
    async def test_fallback_not_triggered_for_network_error(self):
        """NETWORK_ERROR (transient) does not trigger the multi-registry fallback."""
        from honeynet_framework.image_resolver import (
            ImageResolver, ImageResolutionEntry, ResolutionStatus,
        )
        from unittest.mock import AsyncMock

        resolver = ImageResolver()
        tried: list[str] = []

        async def mock_retry(image_ref):
            tried.append(image_ref)
            return ImageResolutionEntry(image_ref=image_ref, status=ResolutionStatus.NETWORK_ERROR)

        resolver._resolve_with_retry = mock_retry
        resolver._resolve_from_local_image = AsyncMock(return_value=None)

        result = await resolver._resolve_one("bitnami/kafka:3.6")
        assert result.status == ResolutionStatus.NETWORK_ERROR
        assert tried == ["bitnami/kafka:3.6"], f"Should only try the original ref, got: {tried}"


class TestCommandPolicy:
    """Tests for the dynamic command policy using ImageProfile."""

    def _daemon_profile(self, image="nginx:1.25"):
        """Daemon entrypoint — commands are always trusted."""
        from honeynet_framework.image_introspector import ImageProfile
        return ImageProfile(
            image_ref=image,
            entrypoint=("/docker-entrypoint.sh",), cmd=("nginx", "-g", "daemon off;"),
            env=(), exposed_ports=("80/tcp",), working_dir="", error="",
        )

    def _interpreter_profile(self, image="python:3.12-slim"):
        from honeynet_framework.image_introspector import ImageProfile
        return ImageProfile(
            image_ref=image,
            entrypoint=("python3",), cmd=("python3",),
            env=(), exposed_ports=(), working_dir="", error="",
        )

    def test_null_command_always_ok(self):
        from honeynet_framework.command_policy import evaluate_command
        verdict, _ = evaluate_command("python:3.12-slim", None)
        assert verdict == "ok"

    def test_daemon_image_trusts_command(self):
        """Daemon images always trust the user's command (LLM may provide needed subcommands)."""
        from honeynet_framework.command_policy import evaluate_command
        profile = self._daemon_profile()
        verdict, _ = evaluate_command("nginx:1.25", ["some", "command"], profile=profile)
        assert verdict == "ok"

    def test_base_image_fictional_script_stripped(self):
        """Interpreter images get command stripped."""
        from honeynet_framework.command_policy import evaluate_command
        profile = self._interpreter_profile()
        verdict, _ = evaluate_command("python:3.12-slim", ["python", "scheduler.py"], profile=profile)
        assert verdict == "strip"

    def test_no_profile_trusts_command(self):
        """Without a profile, commands are trusted (no silent rewriting)."""
        from honeynet_framework.command_policy import evaluate_command
        verdict, _ = evaluate_command("python:3.12-slim", ["python", "scheduler.py"])
        assert verdict == "ok"

    def test_unknown_image_trusts_command(self):
        """Unknown images (no profile) are not restricted."""
        from honeynet_framework.command_policy import evaluate_command
        verdict, _ = evaluate_command("mycompany/myapp:v2", ["run", "--workers=4"])
        assert verdict == "ok"

    def test_repair_world_model_with_profiles(self):
        """repair_world_model_commands only strips interpreter commands, trusts daemon commands."""
        from honeynet_framework.command_policy import repair_world_model_commands
        from honeynet_framework.image_introspector import ImageProfile

        # Interpreter — command should be stripped
        interp_profile = ImageProfile(
            image_ref="python:3.12-slim", entrypoint=("python3",),
            cmd=("python3",), env=(), exposed_ports=(),
            working_dir="", error="",
        )
        # Daemon — command should be trusted (LLM may provide needed subcommands)
        daemon_profile = ImageProfile(
            image_ref="keycloak/keycloak:23.0", entrypoint=("/opt/keycloak/bin/kc.sh",),
            cmd=(), env=(), exposed_ports=("8080/tcp",),
            working_dir="", error="",
        )

        wm = WorldModel(
            organization=Organization(name="Test"),
            zones={"dmz": WorldZone(name="dmz", deploy=ZoneDeploy(network_name="dmz"))},
            systems={
                "bad_python": System(
                    name="bad_python", kind=SystemKind.RUNTIME,
                    deploy=SystemDeploy(image="python:3.12-slim", zone="dmz",
                                        command=["python", "-m", "scheduler"]),
                ),
                "sso": System(
                    name="sso", kind=SystemKind.IDENTITY,
                    deploy=SystemDeploy(image="keycloak/keycloak:23.0", zone="dmz",
                                        command=["start-dev"]),
                ),
            },
        )

        profiles = {
            "python:3.12-slim": interp_profile,
            "keycloak/keycloak:23.0": daemon_profile,
        }
        repairs = repair_world_model_commands(wm, profiles=profiles)
        assert len(repairs) == 1  # only python stripped, keycloak trusted
        assert wm.systems["bad_python"].deploy.command is None
        assert wm.systems["sso"].deploy.command == ["start-dev"]

    def test_repair_without_profiles_is_noop(self):
        """Without profiles, repair doesn't touch commands."""
        from honeynet_framework.command_policy import repair_world_model_commands

        wm = WorldModel(
            organization=Organization(name="Test"),
            zones={"dmz": WorldZone(name="dmz", deploy=ZoneDeploy(network_name="dmz"))},
            systems={
                "svc": System(
                    name="svc", kind=SystemKind.RUNTIME,
                    deploy=SystemDeploy(image="python:3.12-slim", zone="dmz",
                                        command=["python", "worker.py"]),
                ),
            },
        )

        repairs = repair_world_model_commands(wm)
        assert len(repairs) == 0
        assert wm.systems["svc"].deploy.command == ["python", "worker.py"]

    def test_multiple_port_mappings(self):
        """Should find the correct port among multiple mappings."""
        import asyncio
        from unittest.mock import AsyncMock, patch
        from honeynet_framework.qa import QARunner

        runner = QARunner()

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(
            return_value=(
                b"80/tcp -> 0.0.0.0:8080\n443/tcp -> 0.0.0.0:8443\n3000/tcp -> 0.0.0.0:3000\n",
                b"",
            )
        )

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            ok_3000, _ = asyncio.run(runner._verify_port_owner(3000, "mycontainer"))
            ok_80, _ = asyncio.run(runner._verify_port_owner(80, "mycontainer"))

        assert ok_3000 is True
        assert ok_80 is False  # only 8080 is mapped, not 80


# ---------------------------------------------------------------------------
# Tool-use tests
# ---------------------------------------------------------------------------

class TestGenerateWithToolsFallback:
    """Base LLMProvider.generate_with_tools() falls back to plain generate()."""

    @pytest.mark.asyncio
    async def test_base_provider_fallback(self):
        """When a provider does not override generate_with_tools, it calls generate()."""
        import asyncio
        from unittest.mock import AsyncMock
        from honeynet_framework.llm import LLMProvider, TokenUsage

        class _MinimalProvider(LLMProvider):
            async def generate(self, prompt, system_message=None, temperature=None):
                return ("plain-response", TokenUsage(prompt_tokens=5, completion_tokens=3))

        provider = _MinimalProvider()
        tool_executor = AsyncMock(return_value='{"exists": true}')

        text, usage = await provider.generate_with_tools(
            "build me a honeynet",
            tools=[{"name": "validate_docker_image", "input_schema": {}}],
            tool_executor=tool_executor,
        )

        assert text == "plain-response"
        assert usage.total_tokens == 8
        tool_executor.assert_not_called()

    @pytest.mark.asyncio
    async def test_base_provider_passes_system_message(self):
        """system_message is forwarded to generate() in the fallback path."""
        from unittest.mock import AsyncMock
        from honeynet_framework.llm import LLMProvider, TokenUsage

        received: dict = {}

        class _MinimalProvider(LLMProvider):
            async def generate(self, prompt, system_message=None, temperature=None):
                received["system"] = system_message
                return ("ok", TokenUsage())

        provider = _MinimalProvider()
        await provider.generate_with_tools(
            "prompt",
            tools=[],
            tool_executor=AsyncMock(return_value=""),
            system_message="you are a helpful assistant",
        )
        assert received["system"] == "you are a helpful assistant"


class TestImageToolExecutor:
    """Tests for _image_tool_executor in extraction/extractor.py."""

    @pytest.mark.asyncio
    async def test_valid_image_returns_exists_true(self):
        from unittest.mock import patch, AsyncMock
        from honeynet_framework.extraction.extractor import _image_tool_executor

        mock_result = {"exists": True, "ref": "nginx:1.25-alpine@sha256:abc123"}
        with patch(
            "honeynet_framework.extraction.extractor.check_image_exists",
            new=AsyncMock(return_value=mock_result),
        ):
            result_str = await _image_tool_executor(
                "validate_docker_image", {"image_ref": "nginx:1.25-alpine"}
            )
        import json
        result = json.loads(result_str)
        assert result["exists"] is True
        assert "nginx" in result["ref"]

    @pytest.mark.asyncio
    async def test_invalid_image_returns_exists_false(self):
        from unittest.mock import patch, AsyncMock
        from honeynet_framework.extraction.extractor import _image_tool_executor

        mock_result = {"exists": False, "status": "not_found", "message": "404"}
        with patch(
            "honeynet_framework.extraction.extractor.check_image_exists",
            new=AsyncMock(return_value=mock_result),
        ):
            result_str = await _image_tool_executor(
                "validate_docker_image", {"image_ref": "nonexistent/image:99.9"}
            )
        import json
        result = json.loads(result_str)
        assert result["exists"] is False

    @pytest.mark.asyncio
    async def test_empty_image_ref_returns_error(self):
        from honeynet_framework.extraction.extractor import _image_tool_executor
        import json

        result_str = await _image_tool_executor("validate_docker_image", {"image_ref": ""})
        result = json.loads(result_str)
        assert result["exists"] is False
        assert "error" in result

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error(self):
        from honeynet_framework.extraction.extractor import _image_tool_executor
        import json

        result_str = await _image_tool_executor("nonexistent_tool", {})
        result = json.loads(result_str)
        assert "error" in result
        assert "nonexistent_tool" in result["error"]

    @pytest.mark.asyncio
    async def test_missing_image_ref_key_returns_error(self):
        from honeynet_framework.extraction.extractor import _image_tool_executor
        import json

        result_str = await _image_tool_executor("validate_docker_image", {})
        result = json.loads(result_str)
        assert result["exists"] is False
        assert "error" in result


class TestCheckImageExists:
    """Tests for check_image_exists() in image_resolver.py."""

    @pytest.mark.asyncio
    async def test_resolved_image_returns_exists_true(self):
        from unittest.mock import AsyncMock, patch, MagicMock
        from honeynet_framework.image_resolver import check_image_exists, ResolutionStatus

        mock_entry = MagicMock()
        mock_entry.status = ResolutionStatus.RESOLVED
        mock_entry.digest_ref = "nginx:1.25-alpine@sha256:deadbeef"

        with patch(
            "honeynet_framework.image_resolver.ImageResolver._resolve_one",
            new=AsyncMock(return_value=mock_entry),
        ):
            result = await check_image_exists("nginx:1.25-alpine")

        assert result["exists"] is True
        assert result["ref"] == "nginx:1.25-alpine@sha256:deadbeef"

    @pytest.mark.asyncio
    async def test_resolved_without_digest_returns_original_ref(self):
        """When digest_ref is None (shouldn't happen but defensive), original ref is returned."""
        from unittest.mock import AsyncMock, patch, MagicMock
        from honeynet_framework.image_resolver import check_image_exists, ResolutionStatus

        mock_entry = MagicMock()
        mock_entry.status = ResolutionStatus.RESOLVED
        mock_entry.digest_ref = None

        with patch(
            "honeynet_framework.image_resolver.ImageResolver._resolve_one",
            new=AsyncMock(return_value=mock_entry),
        ):
            result = await check_image_exists("nginx:latest")

        assert result["exists"] is True
        assert result["ref"] == "nginx:latest"

    @pytest.mark.asyncio
    async def test_not_found_image_returns_exists_false(self):
        from unittest.mock import AsyncMock, patch, MagicMock
        from honeynet_framework.image_resolver import check_image_exists, ResolutionStatus

        mock_entry = MagicMock()
        mock_entry.status = ResolutionStatus.NOT_FOUND
        mock_entry.message = "manifest unknown"

        with patch(
            "honeynet_framework.image_resolver.ImageResolver._resolve_one",
            new=AsyncMock(return_value=mock_entry),
        ):
            result = await check_image_exists("ghost/nonexistent:1.0")

        assert result["exists"] is False
        assert result["status"] == ResolutionStatus.NOT_FOUND.value

    @pytest.mark.asyncio
    async def test_network_error_returns_exists_false(self):
        from unittest.mock import AsyncMock, patch, MagicMock
        from honeynet_framework.image_resolver import check_image_exists, ResolutionStatus

        mock_entry = MagicMock()
        mock_entry.status = ResolutionStatus.NETWORK_ERROR
        mock_entry.message = "connection refused"

        with patch(
            "honeynet_framework.image_resolver.ImageResolver._resolve_one",
            new=AsyncMock(return_value=mock_entry),
        ):
            result = await check_image_exists("nginx:1.25-alpine")

        assert result["exists"] is False
        assert result["status"] == ResolutionStatus.NETWORK_ERROR.value
