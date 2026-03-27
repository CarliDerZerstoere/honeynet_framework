"""
HTML reporting utilities for cumulative run metrics.

Maintains:
- report_data.json: append-only-ish history (upsert by run_id)
- metrics_report.html: human-readable dashboard with explanations, table, charts
"""

from __future__ import annotations

import html as _html
import json
import logging
import os
from pathlib import Path
from typing import Any

from .utils import utc_now_iso as _utc_now


def _esc(value: object) -> str:
    """HTML-escape any dynamic value before interpolation into templates."""
    return _html.escape(str(value), quote=True)

from .utils import atomic_write_text

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared HTML helpers (used by both metrics and benchmark reports)
# ---------------------------------------------------------------------------

def _common_css() -> str:
    """Shared CSS variables and base styles for all HTML reports."""
    return """
    :root {
      --bg: #0f172a;
      --panel: #111827;
      --card: #1f2937;
      --text: #e5e7eb;
      --muted: #9ca3af;
      --line: #374151;
      --accent: #38bdf8;
      --ok: #22c55e;
      --warn: #f59e0b;
      --bad: #ef4444;
    }
    * { box-sizing: border-box; }
    body { font-family: Inter, 'Segoe UI', Arial, sans-serif; margin: 0; padding: 24px;
           color: var(--text); background: radial-gradient(circle at top, #111827 0%, var(--bg) 60%); }
    h1, h2, h3 { margin: 0 0 10px; }
    .muted { color: var(--muted); font-size: 0.92rem; }
    .section { margin-top: 28px; }
    .panel { background: color-mix(in srgb, var(--panel) 92%, #000 8%);
             border: 1px solid var(--line); border-radius: 12px; padding: 18px; }

    /* Status banner */
    .banner { border-radius: 10px; padding: 14px 18px; margin-bottom: 20px;
              font-size: 1.05rem; font-weight: 500; display: flex; align-items: center; gap: 10px; }
    .banner.ok  { background: rgba(34,197,94,0.12); border: 1px solid rgba(34,197,94,0.35); color: #86efac; }
    .banner.warn { background: rgba(245,158,11,0.12); border: 1px solid rgba(245,158,11,0.35); color: #fcd34d; }
    .banner.bad  { background: rgba(239,68,68,0.12); border: 1px solid rgba(239,68,68,0.35); color: #fca5a5; }
    .banner .icon { font-size: 1.3rem; }

    /* KPI cards */
    .kpi-grid { display: grid; grid-template-columns: repeat(4, minmax(200px, 1fr));
                gap: 14px; margin: 16px 0 20px; }
    .kpi-card { border: 1px solid var(--line); border-radius: 10px; padding: 14px 16px;
                background: var(--card); }
    .kpi-card .label { color: var(--text); font-size: 0.92rem; font-weight: 600; }
    .kpi-card .subtitle { color: var(--muted); font-size: 0.78rem; margin-top: 3px; line-height: 1.35; }
    .kpi-card .value { font-size: 1.4rem; font-weight: 700; margin-top: 8px; display: flex;
                       align-items: baseline; gap: 6px; }
    .kpi-card .value .detail { font-size: 0.82rem; font-weight: 400; color: var(--muted); }
    .kpi-card .value.ok { color: var(--ok); }
    .kpi-card .value.warn { color: var(--warn); }
    .kpi-card .value.bad { color: var(--bad); }

    /* Tables */
    table { border-collapse: collapse; width: 100%; margin-top: 8px; font-size: 0.88rem;
            background: var(--panel); border-radius: 8px; overflow: hidden; }
    th, td { border: 1px solid var(--line); padding: 7px 9px; text-align: left; }
    th { background: #1f2937; font-weight: 600; font-size: 0.82rem; text-transform: uppercase;
         letter-spacing: 0.02em; color: var(--muted); }
    td { color: var(--text); }

    /* Pipeline stage dots */
    .stage-dots { display: flex; gap: 4px; align-items: center; }
    .stage-dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
    .stage-dot.pass { background: var(--ok); }
    .stage-dot.fail { background: var(--bad); }
    .stage-dot[title] { cursor: help; }

    /* Charts */
    .chart-row { display: grid; grid-template-columns: repeat(2, minmax(320px, 1fr)); gap: 20px; }
    .chart-box { background: var(--panel); border: 1px solid var(--line);
                 border-radius: 12px; padding: 14px; min-height: 300px; }
    .chart-box h3 { font-size: 0.92rem; color: var(--muted); margin-bottom: 8px; }

    /* Glossary */
    .glossary dt { font-weight: 600; color: var(--accent); margin-top: 10px; }
    .glossary dd { color: var(--muted); margin: 2px 0 0 0; font-size: 0.9rem; line-height: 1.5; }

    code { background: #0b1220; border: 1px solid var(--line); padding: 1px 4px;
           border-radius: 4px; color: #bfdbfe; }

    @media (max-width: 1100px) {
      .kpi-grid { grid-template-columns: repeat(2, minmax(200px, 1fr)); }
    }
    @media (max-width: 850px) {
      .chart-row { grid-template-columns: 1fr; }
      .kpi-grid { grid-template-columns: 1fr; }
    }
    """


def _status_banner(level: str, message: str) -> str:
    """Render a colored status banner. level: ok | warn | bad."""
    icons = {"ok": "&#10003;", "warn": "&#9888;", "bad": "&#10007;"}
    return (
        f'<div class="banner {_esc(level)}">'
        f'<span class="icon">{icons.get(level, "")}</span>'
        f'{_esc(message)}'
        f'</div>'
    )


def _kpi_card(label: str, subtitle: str, value: str, detail: str, level: str) -> str:
    """Render a KPI card with label, subtitle, value, and detail (e.g. '19/20 runs')."""
    return (
        f'<div class="kpi-card">'
        f'<div class="label">{_esc(label)}</div>'
        f'<div class="subtitle">{_esc(subtitle)}</div>'
        f'<div class="value {_esc(level)}">{_esc(value)} <span class="detail">{_esc(detail)}</span></div>'
        f'</div>'
    )


def _stage_dots(stages: list[tuple[str, bool]]) -> str:
    """Render pipeline stage indicator as colored dots with tooltips."""
    dots = []
    for name, passed in stages:
        cls = "pass" if passed else "fail"
        dots.append(f'<span class="stage-dot {cls}" title="{_esc(name)}: {"PASS" if passed else "FAIL"}"></span>')
    return f'<div class="stage-dots">{"".join(dots)}</div>'

# Optional override: work_dir/reporting_thresholds.json with float keys (see _default_thresholds)
_DEFAULT_THRESHOLDS: dict[str, float] = {
    "deploy_success_ok": 0.8,
    "deploy_success_warn": 0.5,
    "world_valid_ok": 0.9,
    "world_valid_warn": 0.7,
    "apply_ok": 0.8,
    "apply_warn": 0.5,
    "qa_avg_ok": 0.85,
    "qa_avg_warn": 0.6,
    "container_avg_ok": 0.85,
    "container_avg_warn": 0.6,
    "formal_fit_ok": 0.8,
    "formal_fit_warn": 0.5,
}


def _load_thresholds(work_dir: Path) -> dict[str, float]:
    out = dict(_DEFAULT_THRESHOLDS)
    path = work_dir / "reporting_thresholds.json"
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k, v in raw.items():
                    try:
                        out[str(k)] = float(v)
                    except (TypeError, ValueError):
                        pass
        except (OSError, json.JSONDecodeError):
            pass
    env_path = os.environ.get("HONEYNET_REPORTING_THRESHOLDS")
    if env_path:
        p = Path(env_path)
        if p.is_file():
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    for k, v in raw.items():
                        try:
                            out[str(k)] = float(v)
                        except (TypeError, ValueError):
                            pass
            except (OSError, json.JSONDecodeError):
                pass
    return out


def _write_summary_json(payload: dict[str, Any], work_dir: Path) -> None:
    """Write a summary.json with aggregate KPIs from all runs.

    Replaces the legacy runtime_metrics_legacy module's summary generation.
    Only includes measured values in averages (skips None/not_run).
    """
    from collections import Counter

    runs = payload.get("runs", [])
    total = len(runs)
    if total == 0:
        summary = {
            "generated_at_utc": _utc_now(),
            "runs": 0,
            "deploy_success_rate": 0.0,
            "health_check_rate_mean": 0.0,
            "container_start_rate_mean": 0.0,
            "failure_stage_counts": {},
        }
    else:
        stage_counts: Counter[str] = Counter()
        deploy_successes = 0
        health_check_values: list[float] = []
        container_values: list[float] = []

        for r in runs:
            dep = r.get("deployability", {}) if isinstance(r, dict) else {}
            if not isinstance(dep, dict):
                dep = {}

            if dep.get("deploy_success"):
                deploy_successes += 1

            # Only include measured health checks — check status, not just None
            hc_status = str(dep.get("health_check_status", "not_run") or "not_run")
            if hc_status == "measured":
                hc_val = dep.get("health_check_rate")
                if hc_val is not None:
                    try:
                        health_check_values.append(max(0.0, min(1.0, float(hc_val))))
                    except (TypeError, ValueError):
                        pass

            # Only include measured container start rates
            rv_status = str(dep.get("container_health_status", "not_run") or "not_run")
            if rv_status == "measured":
                cr_val = dep.get("container_start_rate")
                if cr_val is not None:
                    try:
                        container_values.append(max(0.0, min(1.0, float(cr_val))))
                    except (TypeError, ValueError):
                        pass

            # Failure stage
            stage = str(r.get("failure_stage", "") or "")
            if not stage:
                obs = r.get("observability", {})
                if isinstance(obs, dict):
                    stage = str(obs.get("failure_stage", "") or "")
            if stage and stage != "completed":
                stage_counts[stage] += 1

        summary = {
            "generated_at_utc": _utc_now(),
            "runs": total,
            "deploy_success_rate": round(deploy_successes / total, 4),
            "health_check_rate_mean": (
                round(sum(health_check_values) / len(health_check_values), 4) if health_check_values else 0.0
            ),
            "container_start_rate_mean": (
                round(sum(container_values) / len(container_values), 4)
                if container_values else 0.0
            ),
            "failure_stage_counts": dict(sorted(stage_counts.items())),
        }

    summary_path = work_dir / "summary.json"
    atomic_write_text(summary_path, json.dumps(summary, indent=2))


def update_metrics_report(
    work_dir: Path,
    metrics: dict[str, Any],
    *,
    qa_report: dict[str, Any] | None = None,
    validation_report: dict[str, Any] | None = None,
) -> None:
    """Upsert one run into report_data.json and regenerate metrics_report.html."""
    work_dir.mkdir(parents=True, exist_ok=True)
    data_path = work_dir / "report_data.json"
    html_path = work_dir / "metrics_report.html"

    payload = _load_report_data(data_path)
    _migrate_payload_schema(payload)
    run_entry = _build_run_entry(
        work_dir,
        metrics,
        qa_report=qa_report,
        validation_report=validation_report,
    )
    _upsert_run(payload, run_entry)
    payload["generated_at_utc"] = _utc_now()
    payload["total_runs"] = len(payload["runs"])

    atomic_write_text(data_path, json.dumps(payload, indent=2))
    atomic_write_text(html_path, _render_html(payload, work_dir))
    try:
        _write_summary_json(payload, work_dir)
    except Exception as e:
        logger.warning("Summary JSON generation failed: %s", e)


def _migrate_payload_schema(payload: dict[str, Any]) -> None:
    """Ensure schema_version >= 2 for report_data."""
    try:
        sv = int(payload.get("schema_version", 1) or 1)
    except (TypeError, ValueError):
        sv = 1
    if sv < 2:
        payload["schema_version"] = 2


def _load_report_data(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": 2, "generated_at_utc": _utc_now(), "total_runs": 0, "runs": []}
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.error("report_data.json corrupt — quarantining and starting fresh: %s", e)
        try:
            if path.exists():
                quarantine = path.with_suffix(path.suffix + f".corrupt.{_utc_now().replace(':', '-')}")
                path.replace(quarantine)
        except OSError:
            pass
        return {"schema_version": 2, "generated_at_utc": _utc_now(), "total_runs": 0, "runs": []}
    if not isinstance(obj, dict):
        return {"schema_version": 2, "generated_at_utc": _utc_now(), "total_runs": 0, "runs": []}
    runs = obj.get("runs")
    if not isinstance(runs, list):
        obj["runs"] = []
    _migrate_payload_schema(obj)
    return obj


def _resolve_side_report(
    work_dir: Path,
    filename: str,
    inline: dict[str, Any] | None,
    current_run_id: str,
) -> tuple[dict[str, Any], str, bool]:
    """
    Return (report_dict, mode, run_id_match).
    mode: inline | disk | absent
    Resolution: inline first; else runs/<run_id>/<filename> then root; require run_id match when current_run_id is set.
    """
    if inline is not None:
        return inline, "inline", True
    disk = _load_disk_report_json(work_dir, filename, current_run_id)
    if not disk:
        return {}, "absent", False
    drid = str(disk.get("run_id", "") or "")
    if current_run_id and drid and drid == current_run_id:
        return disk, "disk", True
    if current_run_id and drid and drid != current_run_id:
        logger.debug("Ignoring stale %s (run_id mismatch)", filename)
        return {}, "absent", False
    if current_run_id and not drid:
        return {}, "absent", False
    if not current_run_id and not drid:
        return disk, "disk", False
    return disk, "disk", True


def _build_run_entry(
    work_dir: Path,
    metrics: dict[str, Any],
    *,
    qa_report: dict[str, Any] | None = None,
    validation_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    obs = metrics.get("observability", {}) if isinstance(metrics, dict) else {}
    dep = metrics.get("deployability", {}) if isinstance(metrics, dict) else {}
    scen = metrics.get("scenario_fit", {}) if isinstance(metrics, dict) else {}
    errors = metrics.get("errors", []) if isinstance(metrics, dict) else []
    if not isinstance(errors, list):
        errors = [str(errors)]

    rid = str(obs.get("run_id", "") or "")
    qa_resolved, qa_mode, qa_match = _resolve_side_report(work_dir, "qa_report.json", qa_report, rid)
    val_resolved, val_mode, val_match = _resolve_side_report(
        work_dir, "validation_report.json", validation_report, rid
    )

    # If health checks never ran for this run, do not trust disk QA from a previous run
    qtot = int(dep.get("health_checks_total", 0) or 0)
    hc_eval = str(dep.get("health_check_status") or "not_run")
    if qtot == 0 and hc_eval == "not_run" and qa_mode == "disk":
        qa_resolved, qa_mode, qa_match = {}, "absent", False

    # Preserve None for "not measured" — downstream consumers (summary.json,
    # chart series) use evaluation_status or None-checks to exclude these runs
    # from averages.  Converting to 0.0 here would lose that distinction.
    health_check_rate = _to_float_or_none(dep.get("health_check_rate"))
    container_start_rate = _to_float_or_none(dep.get("container_start_rate"))
    model_dep_rate = _to_float(scen.get("model_dep_rate"))
    image_check_rate = _to_float(scen.get("image_check_rate"))
    planned_service_coverage = _to_float(scen.get("planned_service_coverage"))
    running_service_coverage = _to_float(scen.get("running_service_coverage"))
    planned_zone_coverage = _to_float(scen.get("planned_zone_coverage"))
    running_zone_coverage = _to_float(scen.get("running_zone_coverage"))
    planned_dep_coverage = _to_float(scen.get("planned_dep_coverage"))
    running_dep_coverage = _to_float(scen.get("running_dep_coverage"))
    placement_violations = int(scen.get("placement_violations", 0) or 0)
    benchmark_status = str(scen.get("benchmark_status", "not_configured") or "not_configured")
    if benchmark_status == "not_configured":
        benchmark_pass_out: bool | None = None
    elif benchmark_status == "empty_reference":
        benchmark_pass_out = False
    else:
        benchmark_pass_out = bool(scen.get("benchmark_pass", False))

    pk = metrics.get("pipeline_kpis") if isinstance(metrics, dict) else {}
    if not isinstance(pk, dict):
        pk = {}

    qa_summary = _extract_qa_summary(qa_resolved)
    if qa_mode == "absent":
        qa_summary["derived_from"] = "metrics_only"
        qa_summary["checks_run"] = qtot
        qa_summary["checks_passed"] = int(dep.get("health_checks_passed", 0) or 0)
        qa_summary["checks_failed"] = max(0, qtot - qa_summary["checks_passed"])
        qa_summary["pass_rate"] = health_check_rate
    else:
        qa_summary["derived_from"] = "qa_report" if qa_mode != "inline" else "inline"

    return {
        "run_id": rid,
        "recorded_at_utc": _utc_now(),
        "failure_stage": str(obs.get("failure_stage", "")),
        "error_class": str(obs.get("error_class", "")),
        "stage_durations": dict(obs.get("stage_durations", {})),
        "repair_actions": list(obs.get("repair_actions", [])),
        "tofu_version": str(obs.get("tofu_version", "")),
        "sources": {
            "qa_report": {"mode": qa_mode, "run_id_match": qa_match},
            "validation_report": {"mode": val_mode, "run_id_match": val_match},
        },
        "deployability": {
            "world_model_valid": bool(dep.get("world_model_valid", False)),
            "config_validation_pass": bool(dep.get("config_validation_pass", False)),
            "plan_success": bool(dep.get("plan_success", False)),
            "deploy_success": bool(dep.get("deploy_success", False)),
            "runtime_verification_success": bool(dep.get("runtime_verification_success", False)),
            "health_check_rate": health_check_rate,
            "container_start_rate": container_start_rate,
            "expected_containers": int(dep.get("expected_containers", 0) or 0),
            "planned_containers": int(dep.get("planned_containers", 0) or 0),
            "dropped_containers": int(dep.get("dropped_containers", 0) or 0),
            "running_containers": int(dep.get("running_containers", 0) or 0),
            "health_checks_total": int(dep.get("health_checks_total", 0) or 0),
            "health_checks_passed": int(dep.get("health_checks_passed", 0) or 0),
            "health_check_status": str(dep.get("health_check_status", "not_run")),
            "container_health_status": str(dep.get("container_health_status", "not_run")),
            "health_check_rate_by_type": dict(dep.get("health_check_rate_by_type", {})),
        },
        "scenario_fit": {
            "zone_count": int(scen.get("zone_count", 0) or 0),
            "system_count": int(scen.get("system_count", 0) or 0),
            "model_dep_rate": model_dep_rate,
            "image_check_rate": image_check_rate,
            "planned_service_coverage": planned_service_coverage,
            "running_service_coverage": running_service_coverage,
            "planned_zone_coverage": planned_zone_coverage,
            "running_zone_coverage": running_zone_coverage,
            "planned_dep_coverage": planned_dep_coverage,
            "running_dep_coverage": running_dep_coverage,
            "placement_violations": placement_violations,
            "benchmark_pass": benchmark_pass_out,
            "benchmark_status": benchmark_status,
            "benchmark_id": str(scen.get("benchmark_id", "")),
            "benchmark_ref": str(scen.get("benchmark_ref", "")),
            "scenario_fit_score": _to_float(scen.get("scenario_fit_score")),
        },
        "validation_summary": _extract_validation_summary(val_resolved),
        "qa_summary": qa_summary,
        "pipeline_kpis": pk,
        "errors": [str(e) for e in errors],
    }


def _extract_validation_summary(validation_report: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(validation_report, dict):
        return {"error_count": 0, "warning_count": 0}
    errors = validation_report.get("errors", [])
    warnings = validation_report.get("warnings", [])
    return {
        "error_count": len(errors) if isinstance(errors, list) else 0,
        "warning_count": len(warnings) if isinstance(warnings, list) else 0,
    }


def _extract_qa_summary(qa_report: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(qa_report, dict):
        return {"checks_run": 0, "checks_passed": 0, "checks_failed": 0, "pass_rate": 0.0}
    checks_run = int(qa_report.get("checks_run", 0) or 0)
    checks_passed = int(qa_report.get("checks_passed", 0) or 0)
    raw_failed = qa_report.get("checks_failed")
    checks_failed = int(raw_failed) if raw_failed is not None else max(0, checks_run - checks_passed)
    pass_rate = _to_float(qa_report.get("pass_rate"))
    return {
        "checks_run": checks_run,
        "checks_passed": checks_passed,
        "checks_failed": checks_failed,
        "pass_rate": pass_rate,
    }


def _upsert_run(payload: dict[str, Any], run_entry: dict[str, Any]) -> None:
    runs = payload.setdefault("runs", [])
    run_id = run_entry.get("run_id", "")
    if not run_id:
        logger.warning("Skipping report upsert: empty run_id")
        return
    for i, existing in enumerate(runs):
        if isinstance(existing, dict) and existing.get("run_id") == run_id:
            runs[i] = run_entry
            return
    runs.append(run_entry)


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _to_float_or_none(value: Any) -> float | None:
    """Convert to float, preserving None to distinguish 'not measured' from '0.0'."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pct(v: float | None) -> str:
    if v is None:
        return "N/A"
    return f"{v * 100:.1f}%"


def _ok(v: bool) -> str:
    return "PASS" if v else "FAIL"


def _fmt_formal_fit_cell(scen: dict[str, Any]) -> str:
    st = str(scen.get("benchmark_status", "") or "")
    if st == "not_configured":
        return "N/A"
    if st == "error":
        return "ERR"
    if st == "empty_reference":
        return "EMPTY"
    p = scen.get("benchmark_pass")
    if p is None:
        return "N/A"
    return _ok(bool(p))


def _try_load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _load_disk_report_json(work_dir: Path, filename: str, run_id: str) -> dict[str, Any]:
    """Prefer runs/<run_id>/<filename>, then work_dir root (plan: run-scoped artifacts first)."""
    paths: list[Path] = []
    if len(run_id) == 32 and all(c in "0123456789abcdefABCDEF" for c in run_id):
        paths.append(work_dir / "runs" / run_id / filename)
    paths.append(work_dir / filename)
    for p in paths:
        d = _try_load_json(p)
        if d:
            return d
    return {}




def _render_html(payload: dict[str, Any], work_dir: Path) -> str:
    thr = _load_thresholds(work_dir)
    runs = payload.get("runs", [])
    if not isinstance(runs, list):
        runs = []
    runs_dicts = [r for r in runs if isinstance(r, dict)]
    total = len(runs_dicts)

    # --- Compute aggregate KPIs ---
    deploy_success_series = []
    for r in runs_dicts:
        dep = r.get("deployability", {})
        deploy_success_series.append(
            1 if (dep.get("world_model_valid") and dep.get("config_validation_pass")
                  and dep.get("plan_success") and dep.get("deploy_success")
                  and dep.get("runtime_verification_success")) else 0
        )
    n_e2e = sum(deploy_success_series)
    deploy_success_rate = (n_e2e / total) if total else 0.0

    n_world_valid = sum(1 for r in runs_dicts if r.get("deployability", {}).get("world_model_valid"))
    world_valid_rate = (n_world_valid / total) if total else 0.0

    hc_series_raw = [r.get("deployability", {}).get("health_check_rate") for r in runs_dicts]
    hc_series = [round(_to_float(v), 4) if v is not None else None for v in hc_series_raw]
    hc_measured = [round(_to_float(v), 4) for v in hc_series_raw if v is not None]
    qa_avg = (sum(hc_measured) / len(hc_measured)) if hc_measured else 0.0

    container_series_raw = [r.get("deployability", {}).get("container_start_rate") for r in runs_dicts]
    container_series = [round(_to_float(v), 4) if v is not None else None for v in container_series_raw]
    container_measured = [round(_to_float(v), 4) for v in container_series_raw if v is not None]
    container_avg = (sum(container_measured) / len(container_measured)) if container_measured else 0.0

    service_cov_series = [
        round(_to_float(
            r.get("scenario_fit", {}).get("running_service_coverage")
            if r.get("scenario_fit", {}).get("running_service_coverage") is not None
            else r.get("scenario_fit", {}).get("planned_service_coverage")
        ), 4)
        for r in runs_dicts
    ]

    fit_score_series = [
        round(_to_float(r.get("scenario_fit", {}).get("scenario_fit_score")), 4)
        for r in runs_dicts
    ]
    fit_measured = [v for v in fit_score_series if v is not None]
    fit_avg = (sum(fit_measured) / len(fit_measured)) if fit_measured else 0.0

    # Aggregate error_class distribution
    error_class_counts: dict[str, int] = {}
    for r in runs_dicts:
        ec = str(r.get("error_class", "") or "")
        if ec:
            error_class_counts[ec] = error_class_counts.get(ec, 0) + 1

    # Last run stats for the status banner
    last_dep = runs_dicts[-1].get("deployability", {}) if runs_dicts else {}
    last_stage = runs_dicts[-1].get("failure_stage", "") if runs_dicts else ""
    last_running = int(last_dep.get("running_containers", 0))
    last_expected = int(last_dep.get("expected_containers", 0))

    # Failure stage distribution
    stage_counts: dict[str, int] = {}
    for r in runs_dicts:
        stage = str(r.get("failure_stage", "") or "unknown")
        if stage == "completed":
            continue
        stage_counts[stage] = stage_counts.get(stage, 0) + 1

    # --- Status banner ---
    if not runs_dicts:
        banner = _status_banner("warn", "No runs recorded yet. Run a deployment to see metrics here.")
    elif deploy_success_series[-1] == 1:
        banner = _status_banner("ok",
            f"All systems healthy. Last deployment succeeded end-to-end "
            f"({last_running}/{last_expected} containers running).")
    elif last_dep.get("deploy_success"):
        banner = _status_banner("warn",
            f"Partial success. Containers deployed but health checks degraded "
            f"({last_running}/{last_expected} running).")
    else:
        stage_label = last_stage.replace("_", " ").title() if last_stage else "unknown stage"
        banner = _status_banner("bad",
            f"Pipeline blocked at {stage_label}. Last run did not produce running containers.")

    # --- KPI cards ---
    def _level(v: float, ok: float, warn: float) -> str:
        return "ok" if v >= ok else "warn" if v >= warn else "bad"

    kpi_cards = "".join([
        _kpi_card(
            "End-to-End Success",
            "Runs completing all stages from model to health checks",
            _pct(deploy_success_rate),
            f"{n_e2e}/{total} runs",
            _level(deploy_success_rate, thr["deploy_success_ok"], thr["deploy_success_warn"]),
        ),
        _kpi_card(
            "World Model Valid",
            "LLM output successfully parsed into a deployment model",
            _pct(world_valid_rate),
            f"{n_world_valid}/{total} runs",
            _level(world_valid_rate, thr["world_valid_ok"], thr["world_valid_warn"]),
        ),
        _kpi_card(
            "Container Start Rate",
            "Average share of expected containers that actually started",
            _pct(container_avg),
            f"avg over {len(container_measured)} measured runs",
            _level(container_avg, thr["container_avg_ok"], thr["container_avg_warn"]),
        ),
        _kpi_card(
            "Health Check Pass",
            "Post-startup connectivity probes that passed",
            _pct(qa_avg),
            f"avg over {len(hc_measured)} measured runs",
            _level(qa_avg, thr["qa_avg_ok"], thr["qa_avg_warn"]),
        ),
        _kpi_card(
            "Scenario Fit Score",
            "Composite score: avg(service, zone, dependency coverage), halved on violations",
            f"{fit_avg:.2f}",
            f"avg over {len(fit_measured)} measured runs",
            _level(fit_avg, thr.get("formal_fit_ok", 0.8), thr.get("formal_fit_warn", 0.5)),
        ),
    ])

    # --- Table rows (simplified with pipeline stage dots) ---
    rows = []
    for idx, r in enumerate(runs_dicts, start=1):
        dep = r.get("deployability", {})
        scen = r.get("scenario_fit", {})
        stages = [
            ("World Model", bool(dep.get("world_model_valid"))),
            ("Config", bool(dep.get("config_validation_pass"))),
            ("Plan", bool(dep.get("plan_success"))),
            ("Deploy", bool(dep.get("deploy_success"))),
            ("Runtime", bool(dep.get("runtime_verification_success"))),
        ]
        running = int(dep.get("running_containers", 0))
        expected = int(dep.get("expected_containers", 0))
        dropped = int(dep.get("dropped_containers", 0) or 0)
        containers_str = f"{running}/{expected}"
        if dropped:
            containers_str += f" <small>({dropped} dropped)</small>"
        svc_cov = _to_float(
            scen.get("running_service_coverage")
            if scen.get("running_service_coverage") is not None
            else scen.get("planned_service_coverage")
        )
        # QA by-type tooltip
        _by_type = dep.get("health_check_rate_by_type", {})
        _qa_tip = ", ".join(f"{k}: {v*100:.0f}%" for k, v in _by_type.items()) if _by_type else ""
        _qa_cell = _pct(_to_float_or_none(dep.get('health_check_rate')))
        if _qa_tip:
            _qa_cell = f"<span title='{_esc(_qa_tip)}'>{_qa_cell}</span>"
        # Fit score
        _fit = _to_float(scen.get("scenario_fit_score"))
        _fit_str = f"{_fit:.2f}" if _fit > 0 else "N/A"
        # Duration
        _durations = r.get("stage_durations", {})
        _total_dur = sum(_durations.values()) if _durations else 0
        _dur_str = f"{_total_dur:.0f}s" if _total_dur > 0 else ""
        # Failure + error class
        _fail = r.get('failure_stage', '') or ''
        _ec = r.get('error_class', '') or ''
        _fail_cell = _esc(_fail)
        if _ec and _fail:
            _fail_cell = f"{_esc(_fail)} <small style='color:var(--muted)'>({_esc(_ec)})</small>"
        rows.append(
            "<tr>"
            f"<td>{idx}</td>"
            f"<td title='{_esc(r.get('run_id', ''))}'>{_esc(str(r.get('run_id', ''))[:8])}</td>"
            f"<td>{_esc(r.get('recorded_at_utc', '')[:19])}</td>"
            f"<td>{_stage_dots(stages)}</td>"
            f"<td>{containers_str}</td>"
            f"<td>{_qa_cell}</td>"
            f"<td>{_pct(svc_cov)}</td>"
            f"<td>{_fit_str}</td>"
            f"<td>{_fmt_formal_fit_cell(scen if isinstance(scen, dict) else {})}</td>"
            f"<td>{_dur_str}</td>"
            f"<td>{_fail_cell}</td>"
            "</tr>"
        )

    stage_rows = []
    for stage, count in sorted(stage_counts.items(), key=lambda x: x[1], reverse=True):
        stage_rows.append(f"<tr><td>{_esc(stage)}</td><td>{count}</td></tr>")

    # --- Chart data (2 charts) ---
    labels = [f"Run {idx + 1}" for idx, _ in enumerate(runs_dicts)]
    chart_data = json.dumps({
        "labels": labels,
        "deploySuccess": deploy_success_series,
        "containerStart": container_series,
        "healthCheck": hc_series,
        "serviceCoverage": service_cov_series,
        "fitScore": fit_score_series,
    })

    css = _common_css()

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Honeynet Deployment Dashboard</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>{css}</style>
</head>
<body>
  <h1>Honeynet Deployment Dashboard</h1>
  <p class="muted">Auto-generated after each pipeline run &middot; Last updated: {_esc(payload.get("generated_at_utc", ""))}</p>

  {banner}

  <div class="section">
    <h2>Is the pipeline working?</h2>
    <div class="kpi-grid">{kpi_cards}</div>
  </div>

  <div class="section">
    <h2>How are things trending?</h2>
    <div class="chart-row">
      <div class="chart-box"><h3>Deployment Success</h3><canvas id="chartDeploy"></canvas></div>
      <div class="chart-box"><h3>Quality &amp; Coverage</h3><canvas id="chartQuality"></canvas></div>
    </div>
  </div>

  <div class="section panel">
    <h2>What happened in each run?</h2>
    <p class="muted">Each row shows one pipeline run. The colored dots indicate which stages passed (green) or failed (red):
    World Model &rarr; Config &rarr; Plan &rarr; Deploy &rarr; Runtime.</p>
    <table>
      <thead>
        <tr>
          <th>#</th>
          <th>Run</th>
          <th>Time</th>
          <th title="Five pipeline stages: World Model, Config Validation, Plan, Deploy, Runtime Verify">Pipeline</th>
          <th title="Running containers out of expected containers">Containers</th>
          <th title="Percentage of post-startup health/connectivity checks that passed (hover for per-type breakdown)">Health</th>
          <th title="Benchmark service coverage (running if available, else planned)">Coverage</th>
          <th title="Composite scenario-fit score (0-1)">Fit Score</th>
          <th title="Formal benchmark pass: all coverage = 100% and no placement violations">Benchmark</th>
          <th title="Total pipeline duration in seconds">Duration</th>
          <th title="The pipeline stage where this run failed, with error class in parentheses">Failed At</th>
        </tr>
      </thead>
      <tbody>
        {"".join(rows) if rows else "<tr><td colspan='11'>No runs recorded yet.</td></tr>"}
      </tbody>
    </table>
  </div>

  <div class="section panel">
    <h2>Where do failures happen?</h2>
    <p class="muted">Earlier failures (World Model, Config) typically indicate LLM output quality issues.
    Later failures (Deploy, Runtime) indicate infrastructure or Docker problems.</p>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
    <div>
    <h3 style="font-size:0.88rem;color:var(--muted)">By Stage</h3>
    <table>
      <thead><tr><th>Failure Stage</th><th>Runs</th></tr></thead>
      <tbody>
        {"".join(stage_rows) if stage_rows else "<tr><td colspan='2'>No failures recorded.</td></tr>"}
      </tbody>
    </table>
    </div>
    <div>
    <h3 style="font-size:0.88rem;color:var(--muted)">By Error Class</h3>
    <table>
      <thead><tr><th>Error Class</th><th>Runs</th></tr></thead>
      <tbody>
        {"".join(f"<tr><td>{_esc(ec)}</td><td>{cnt}</td></tr>" for ec, cnt in sorted(error_class_counts.items(), key=lambda x: x[1], reverse=True)) if error_class_counts else "<tr><td colspan='2'>No error classes recorded.</td></tr>"}
      </tbody>
    </table>
    </div>
    </div>
  </div>

  <div class="section panel">
    <h2>Metric Glossary</h2>
    <dl class="glossary">
      <dt>End-to-End Success</dt>
      <dd>A run that passed all five pipeline stages: world model extraction, config validation,
          plan generation, infrastructure deploy, and runtime verification. This is the primary KPI.</dd>
      <dt>World Model Valid</dt>
      <dd>The LLM successfully extracted a structured deployment model (systems, zones, dependencies)
          from the input prompt. Failures here mean the LLM output could not be parsed.</dd>
      <dt>Container Start Rate</dt>
      <dd>Fraction of expected containers that actually started running after deployment.
          Calculated as running containers / expected containers. 100% means all containers launched.</dd>
      <dt>Health Check Pass Rate</dt>
      <dd>Post-startup connectivity probes that passed. These check that containers are not just
          running but actually responding (e.g., HTTP health endpoints, TCP connectivity).</dd>
      <dt>Service Coverage</dt>
      <dd>When a benchmark reference is provided: the fraction of required services (e.g., database,
          web server, cache) that are present in the deployment. 100% = all required services found.</dd>
      <dt>Benchmark Pass</dt>
      <dd>True only when ALL benchmark requirements are met: 100% service coverage, 100% zone coverage,
          100% dependency coverage, and zero placement violations.</dd>
      <dt>Scenario Fit Score</dt>
      <dd>Composite score (0.0&ndash;1.0) computed as the equal-weight average of service, zone, and
          dependency coverage. Halved when placement violations exist. Useful for ranking partially
          correct results without the all-or-nothing nature of Benchmark Pass.</dd>
      <dt>Error Class</dt>
      <dd>Coarse classification of why a run failed (e.g., <code>port_conflict</code>,
          <code>invalid_image</code>, <code>llm_schema_error</code>). Shown in the failure column
          and aggregated in the failure distribution table.</dd>
      <dt>Failure Stage</dt>
      <dd>The earliest pipeline stage where a run failed. Common stages:
          <code>world_model_validation</code>, <code>config_validation</code>,
          <code>plan</code>, <code>deploy</code>, <code>runtime_verify</code>.</dd>
    </dl>
  </div>

  <script>
    const data = {chart_data};
    const pct = (v) => v == null ? null : Math.round((v || 0) * 1000) / 10;

    new Chart(document.getElementById("chartDeploy"), {{
      type: "line",
      data: {{
        labels: data.labels,
        datasets: [
          {{
            label: "End-to-End Success (pass=1, fail=0)",
            data: data.deploySuccess,
            borderColor: "#2563eb",
            backgroundColor: "rgba(37,99,235,0.12)",
            tension: 0.2, fill: true
          }},
          {{
            label: "Container Start Rate %",
            data: data.containerStart.map(v => pct(v)),
            borderColor: "#059669",
            backgroundColor: "rgba(5,150,105,0.12)",
            tension: 0.2
          }},
        ]
      }},
      options: {{
        responsive: true,
        plugins: {{ legend: {{ position: "bottom", labels: {{ color: "#9ca3af" }} }} }},
        scales: {{
          y: {{ beginAtZero: true, ticks: {{ color: "#6b7280" }}, grid: {{ color: "#1f2937" }} }},
          x: {{ ticks: {{ color: "#6b7280" }}, grid: {{ color: "#1f2937" }} }}
        }}
      }}
    }});

    new Chart(document.getElementById("chartQuality"), {{
      type: "line",
      data: {{
        labels: data.labels,
        datasets: [
          {{
            label: "Health Check Pass Rate %",
            data: data.healthCheck.map(v => pct(v)),
            borderColor: "#7c3aed",
            backgroundColor: "rgba(124,58,237,0.12)",
            tension: 0.2, spanGaps: true
          }},
          {{
            label: "Service Coverage %",
            data: data.serviceCoverage.map(v => pct(v)),
            borderColor: "#22c55e",
            backgroundColor: "rgba(34,197,94,0.12)",
            tension: 0.2
          }},
          {{
            label: "Scenario Fit Score %",
            data: data.fitScore.map(v => pct(v)),
            borderColor: "#f59e0b",
            backgroundColor: "rgba(245,158,11,0.12)",
            tension: 0.2
          }},
        ]
      }},
      options: {{
        responsive: true,
        plugins: {{ legend: {{ position: "bottom", labels: {{ color: "#9ca3af" }} }} }},
        scales: {{
          y: {{ beginAtZero: true, suggestedMax: 100,
                ticks: {{ color: "#6b7280" }}, grid: {{ color: "#1f2937" }} }},
          x: {{ ticks: {{ color: "#6b7280" }}, grid: {{ color: "#1f2937" }} }}
        }}
      }}
    }});
  </script>
</body>
</html>
"""
