"""Regression tests for report_data v2, run entry building, and summary generation."""

import json
from pathlib import Path

from honeynet_framework import reporting_html
from honeynet_framework.models import Organization, System, SystemDeploy, SystemKind, SystemSimulate, WorldModel, WorldZone, ZoneDeploy
from honeynet_framework.qa import CheckType, QACheck, filter_canary_checks
from honeynet_framework.repair_depends import try_repair_depends_edges


def _minimal_metrics(run_id: str, **dep) -> dict:
    base_dep = {
        "world_model_valid": True,
        "config_validation_pass": True,
        "plan_success": True,
        "deploy_success": True,
        "runtime_verification_success": False,
        "health_check_rate": 0.0,
        "container_start_rate": 0.0,
        "expected_containers": 0,
        "running_containers": 0,
        "health_checks_total": 0,
        "health_checks_passed": 0,
        "health_check_status": "not_run",
        "container_health_status": "not_run",
    }
    base_dep.update(dep)
    return {
        "schema_version": 2,
        "observability": {
            "run_id": run_id,
            "work_dir": "/tmp",
            "tofu_version": "1.6.0",
            "failure_stage": "EXTRACTION",
            "apply_reached_success": False,
            "repair_incident_count": 0,
            "path_a": {
                "diagnostics_collected": False,
                "diagnostics_artifact": "",
                "failing_resources_count": 0,
                "docker_targets_count": 0,
                "error_class": "",
            },
        },
        "deployability": base_dep,
        "scenario_fit": {
            "zone_count": 0,
            "system_count": 0,
            "model_dep_rate": 0.0,
            "image_check_rate": 0.0,
            "planned_service_coverage": 0.0,
            "planned_zone_coverage": 0.0,
            "planned_dep_coverage": 0.0,
            "placement_violations": 0,
            "benchmark_pass": False,
            "benchmark_status": "not_configured",
            "benchmark_id": "",
            "benchmark_ref": "",
        },
        "errors": [],
    }


def test_stale_disk_qa_ignored_when_qa_not_run(tmp_path: Path) -> None:
    """When metrics say QA did not run, do not use qa_report.json from disk."""
    rid = "run-stale-1"
    stale = {
        "run_id": "old-run",
        "checks_run": 99,
        "checks_passed": 99,
        "checks_failed": 0,
        "pass_rate": 1.0,
    }
    (tmp_path / "qa_report.json").write_text(json.dumps(stale), encoding="utf-8")

    m = _minimal_metrics(rid, health_check_status="not_run", health_checks_total=0)
    entry = reporting_html._build_run_entry(tmp_path, m, qa_report=None, validation_report=None)

    assert entry["sources"]["qa_report"]["mode"] == "absent"
    assert entry["qa_summary"].get("checks_run", 0) != 99


def test_inline_qa_authoritative(tmp_path: Path) -> None:
    rid = "run-inline-1"
    m = _minimal_metrics(rid, health_checks_total=2, health_check_status="measured")
    inline = {
        "run_id": rid,
        "checks_run": 2,
        "checks_passed": 2,
        "checks_failed": 0,
        "pass_rate": 1.0,
    }
    entry = reporting_html._build_run_entry(tmp_path, m, qa_report=inline, validation_report=None)
    assert entry["sources"]["qa_report"]["mode"] == "inline"
    assert entry["qa_summary"]["checks_run"] == 2


def test_upsert_run_dedupes_by_run_id(tmp_path: Path) -> None:
    payload = {"schema_version": 2, "runs": [], "generated_at_utc": "", "total_runs": 0}
    e1 = {"run_id": "abc", "recorded_at_utc": "t1", "foo": 1}
    e2 = {"run_id": "abc", "recorded_at_utc": "t2", "foo": 2}
    reporting_html._upsert_run(payload, e1)
    reporting_html._upsert_run(payload, e2)
    assert len(payload["runs"]) == 1
    assert payload["runs"][0]["foo"] == 2


def test_summary_json_generated(tmp_path: Path) -> None:
    """update_metrics_report should produce a summary.json with aggregate KPIs."""
    m = _minimal_metrics(
        "run-1",
        deploy_success=True,
        health_check_rate=0.9,
        health_checks_total=10,
        health_check_status="measured",
        container_start_rate=0.8,
        container_health_status="measured",
    )
    reporting_html.update_metrics_report(tmp_path, m)

    summary_path = tmp_path / "summary.json"
    assert summary_path.exists()
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["runs"] == 1
    assert summary["deploy_success_rate"] == 1.0
    assert summary["health_check_rate_mean"] == 0.9
    assert summary["container_start_rate_mean"] == 0.8


def test_summary_skips_not_run_qa(tmp_path: Path) -> None:
    """Runs where QA was not run should not drag down the QA average."""
    m1 = _minimal_metrics("run-1", deploy_success=True, health_check_rate=0.9,
                          health_checks_total=10, health_check_status="measured")
    m2 = _minimal_metrics("run-2", deploy_success=False, health_check_status="not_run")
    reporting_html.update_metrics_report(tmp_path, m1)
    reporting_html.update_metrics_report(tmp_path, m2)

    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["runs"] == 2
    # Health check mean should be 0.9 (only 1 measured run), NOT 0.45 (averaging with 0)
    assert summary["health_check_rate_mean"] == 0.9


def test_filter_canary_no_match_returns_empty() -> None:
    checks = [
        QACheck(id="running:a", check_type=CheckType.CONTAINER_RUNNING, target="a"),
        QACheck(id="running:b", check_type=CheckType.CONTAINER_RUNNING, target="b"),
    ]
    assert filter_canary_checks(checks, ["nonexistent_prefix"]) == []


def test_disk_qa_prefers_run_scoped_file(tmp_path: Path) -> None:
    rid = "a" * 32
    stale = {"run_id": "old", "checks_run": 50}
    (tmp_path / "qa_report.json").write_text(json.dumps(stale), encoding="utf-8")
    rd = tmp_path / "runs" / rid
    rd.mkdir(parents=True)
    good = {"run_id": rid, "checks_run": 2, "checks_passed": 2, "checks_failed": 0, "pass_rate": 1.0}
    (rd / "qa_report.json").write_text(json.dumps(good), encoding="utf-8")
    m = _minimal_metrics(
        rid,
        health_checks_total=2,
        health_check_status="measured",
        health_check_rate=1.0,
    )
    entry = reporting_html._build_run_entry(tmp_path, m, qa_report=None, validation_report=None)
    assert entry["qa_summary"]["checks_run"] == 2


def test_repair_depends_drops_self_edge() -> None:
    wm = WorldModel(
        organization=Organization(name="t"),
        zones={"z": WorldZone(name="z", deploy=ZoneDeploy(network_name="z", internal=False))},
        systems={
            "web": System(
                name="web",
                kind=SystemKind.WEB,
                deploy=SystemDeploy(image="nginx:alpine", zone="z", depends_on=["web"]),
                simulate=SystemSimulate(hostname="w", role="w"),
            ),
        },
    )
    _, changed = try_repair_depends_edges(wm)
    assert changed
    assert wm.systems["web"].deploy.depends_on == []


def test_corrupt_report_data_quarantined(tmp_path: Path) -> None:
    """Corrupt JSON is moved aside and load returns empty history."""
    bad = tmp_path / "report_data.json"
    bad.write_text("{not json", encoding="utf-8")
    data = reporting_html._load_report_data(bad)
    assert data["runs"] == []
    assert not bad.exists()
    assert any(bad.name in p.name for p in tmp_path.glob("report_data.json.corrupt.*"))
