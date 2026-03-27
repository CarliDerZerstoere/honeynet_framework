"""
Pipeline artifact persistence — all file I/O for metrics, reports, and snapshots.

Extracted from ``orchestrator.py`` to reduce the god-object and isolate
I/O concerns from pipeline logic.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from .consistency_fixer import scrub_env_for_artifact, _SECRET_KEY_HINTS
from .deployer import serialize_stage_results
from .metrics import DeploymentMetrics
from .models import WorldModel
from .repair_incident import RepairIncident, append_repair_incident, excerpt_hash
from .reporting_html import update_metrics_report
from .utils import atomic_write_text

logger = logging.getLogger(__name__)


class ArtifactWriter:
    """Handles all file I/O for pipeline artifacts.

    Stateless except for configuration — all methods are pure I/O with
    no side effects on the pipeline state.
    """

    def __init__(self, run_artifacts_mode: str = "legacy", emit_repair_incidents: bool = False):
        self.run_artifacts_mode = run_artifacts_mode
        self.emit_repair_incidents = emit_repair_incidents

    def save_deployer_stages_snapshot(
        self, work_dir: Path, run_id: str, stage_results: Any,
    ) -> None:
        """Persist OpenTofu stage results (root + optional per-run mirror)."""
        payload = {
            "run_id": run_id,
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            "stages": serialize_stage_results(stage_results),
        }
        text = json.dumps(payload, indent=2)
        try:
            atomic_write_text(work_dir / "deployer_stages.json", text)
            if self.run_artifacts_mode != "legacy" and _is_valid_run_id(run_id):
                rd = work_dir / "runs" / run_id
                rd.mkdir(parents=True, exist_ok=True)
                atomic_write_text(rd / "deployer_stages.json", text)
        except OSError as e:
            logger.debug("deployer_stages snapshot skipped: %s", e)

    def emit_repair_incident_stage_failure(
        self, work_dir: Path, run_id: str, phase: str, stderr_text: str,
    ) -> bool:
        """Record a repair incident for a failed pipeline stage."""
        if not self.emit_repair_incidents:
            return False
        try:
            rid_path = work_dir / "runs" / run_id
            rid_path.mkdir(parents=True, exist_ok=True)
            append_repair_incident(
                rid_path / "repair_attempts.jsonl",
                RepairIncident(
                    run_id=run_id,
                    phase=phase,
                    rule_or_kind="tofu_stage",
                    outcome="fail",
                    detail_hash=excerpt_hash(stderr_text or ""),
                ),
            )
            return True
        except OSError as e:
            logger.debug("Repair incident ledger skipped: %s", e)
        return False

    def save_metrics(
        self,
        metrics: DeploymentMetrics,
        work_dir: Path,
        *,
        qa_report: dict | None = None,
        validation_report: dict | None = None,
    ) -> None:
        """Save deployment metrics to JSON and update cumulative reports."""
        work_dir.mkdir(parents=True, exist_ok=True)
        metrics_dict = _scrub_artifact(metrics.to_dict())
        mode = self.run_artifacts_mode
        if mode != "per_run":
            metrics_path = work_dir / "metrics.json"
            atomic_write_text(metrics_path, json.dumps(metrics_dict, indent=2))
            logger.info("Saved metrics to %s", metrics_path)
        else:
            rid = metrics.run_id or ""
            if not _is_valid_run_id(rid):
                metrics_path = work_dir / "metrics.json"
                atomic_write_text(metrics_path, json.dumps(metrics_dict, indent=2))
                logger.info(
                    "Per-run mode: invalid run_id — wrote fallback metrics.json to %s",
                    metrics_path,
                )
            else:
                logger.info(
                    "Per-run artifacts mode: skipping root metrics.json (use runs/<run_id>/metrics.json)"
                )
        try:
            update_metrics_report(
                work_dir,
                metrics_dict,
                qa_report=qa_report,
                validation_report=validation_report,
            )
            logger.info("Updated HTML metrics report in %s", work_dir)
        except Exception as e:
            logger.warning("Failed to update HTML metrics report: %s", e)
        try:
            self._write_run_artifact_snapshots(
                metrics, work_dir, qa_report, validation_report, metrics_dict
            )
        except OSError as e:
            logger.debug("Run artifact snapshots skipped: %s", e)

    def _write_run_artifact_snapshots(
        self,
        metrics: DeploymentMetrics,
        work_dir: Path,
        qa_report: dict | None,
        validation_report: dict | None,
        metrics_dict: dict[str, Any],
    ) -> None:
        if self.run_artifacts_mode == "legacy":
            return
        rid = metrics.run_id
        if not _is_valid_run_id(rid):
            return
        rd = work_dir / "runs" / rid
        rd.mkdir(parents=True, exist_ok=True)
        atomic_write_text(rd / "metrics.json", json.dumps(metrics_dict, indent=2))
        env_path = rd / "envelope.json"
        started_at = str(
            metrics_dict.get("observability", {}).get("started_at_utc")
            or metrics_dict.get("recorded_at_utc")
            or ""
        )
        if env_path.exists():
            try:
                old = json.loads(env_path.read_text(encoding="utf-8"))
                if isinstance(old, dict) and old.get("started_at_utc"):
                    started_at = str(old["started_at_utc"])
            except (OSError, json.JSONDecodeError):
                pass
        envelope = {
            "schema_version": 2,
            "run_id": rid,
            "started_at_utc": started_at,
            "last_recorded_at_utc": metrics_dict.get("recorded_at_utc", ""),
            "metrics_schema_version": 2,
        }
        atomic_write_text(env_path, json.dumps(envelope, indent=2))
        if qa_report is not None:
            atomic_write_text(rd / "qa_report.json", json.dumps(qa_report, indent=2))
        if validation_report is not None:
            atomic_write_text(rd / "validation_report.json", json.dumps(validation_report, indent=2))

    def save_validation_report(
        self, report: dict, work_dir: Path, run_id: str = "",
    ) -> dict:
        """Save classified validation report to JSON. Returns the written dict."""
        work_dir.mkdir(parents=True, exist_ok=True)
        path = work_dir / "validation_report.json"
        if run_id:
            report = {**report, "run_id": run_id}
        atomic_write_text(path, json.dumps(report, indent=2))
        logger.info("Saved validation report to %s", path)
        return report

    def save_qa_report(
        self, qa_report: dict, work_dir: Path, run_id: str = "",
    ) -> dict:
        """Save QA report to JSON. Returns the written dict."""
        work_dir.mkdir(parents=True, exist_ok=True)
        qa_path = work_dir / "qa_report.json"
        if run_id:
            qa_report = {**qa_report, "run_id": run_id}
        qa_report = _scrub_artifact(qa_report)
        atomic_write_text(qa_path, json.dumps(qa_report, indent=2))
        logger.info("Saved QA report to %s", qa_path)
        return qa_report

    def save_scenario_fit_report(self, report: dict, work_dir: Path) -> None:
        """Save formal scenario-fit report to JSON."""
        work_dir.mkdir(parents=True, exist_ok=True)
        path = work_dir / "scenario_fit_report.json"
        atomic_write_text(path, json.dumps(report, indent=2))
        logger.info("Saved scenario-fit report to %s", path)

    def save_catalog_resolution(
        self, work_dir: Path, run_id: str, applied: list[dict[str, str]],
    ) -> None:
        """Record which catalog archetypes were applied."""
        work_dir.mkdir(parents=True, exist_ok=True)
        path = work_dir / "catalog_resolution.json"
        atomic_write_text(path, json.dumps(
            {"run_id": run_id, "applied": applied},
            indent=2, ensure_ascii=False,
        ))
        logger.info("Saved catalog resolution to %s", path)

    def save_world_model(self, world_model: WorldModel, work_dir: Path) -> None:
        """Save world model as YAML and JSON."""
        work_dir.mkdir(parents=True, exist_ok=True)
        data = world_model.to_dict()
        yaml_path = work_dir / "world_model.yaml"
        atomic_write_text(yaml_path, yaml.dump(data, default_flow_style=False, allow_unicode=True))
        json_path = work_dir / "world_model.json"
        atomic_write_text(json_path, json.dumps(data, indent=2, ensure_ascii=False))

    @staticmethod
    def load_command_warning_rules(work_dir: Path) -> set[str]:
        """Promotable validation warning rule names: policy JSON or defaults."""
        default = {"ONE_SHOT_COMMAND", "FICTIONAL_SCRIPT"}
        path = work_dir / "command_warning_rules.json"
        env = os.environ.get("HONEYNET_COMMAND_WARNING_RULES", "").strip()
        if env:
            try:
                ep = Path(env)
                if ep.is_file():
                    path = ep
            except OSError:
                pass
        if not path.is_file():
            return default
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                return {str(x) for x in raw if x}
            if isinstance(raw, dict) and isinstance(raw.get("rules"), list):
                return {str(x) for x in raw["rules"] if x}
        except (OSError, json.JSONDecodeError):
            logger.warning("Invalid command_warning_rules file %s — using defaults", path)
        return default


def _is_valid_run_id(run_id: str) -> bool:
    """Check if a run_id is a valid 32-char hex string."""
    return len(run_id) == 32 and all(c in "0123456789abcdef" for c in run_id)


_ENV_LIKE_KEYS = frozenset({"env", "environment", "env_vars", "environment_variables"})


def _scrub_artifact(obj: Any, _parent_key: str = "") -> Any:
    """Recursively scrub secret env values from a serialized artifact dict/list.

    Walks the structure looking for:
    1. Lists of "KEY=value" strings (env arrays) under env-like parent keys
    2. Dict values whose key names match secret patterns (password, token, etc.)

    Returns a new object — the input is never mutated.
    """
    if isinstance(obj, dict):
        result = {}
        for k, v in obj.items():
            # Scrub dict values whose key looks secret-like (handles non-env secrets)
            if isinstance(v, str) and v and _SECRET_KEY_HINTS.search(k):
                result[k] = "[REDACTED]"
            else:
                result[k] = _scrub_artifact(v, _parent_key=k)
        return result
    if isinstance(obj, list):
        # Heuristic: if every non-empty element contains "=", treat as env list
        # BUT only if the parent key suggests it's actually an env field.
        non_empty = [x for x in obj if x]
        if (
            non_empty
            and _parent_key.lower() in _ENV_LIKE_KEYS
            and all(isinstance(x, str) and "=" in x for x in non_empty)
        ):
            return scrub_env_for_artifact(obj)
        # Preserve parent key context when recursing into list items
        return [_scrub_artifact(item, _parent_key=_parent_key) for item in obj]
    return obj
