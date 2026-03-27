# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
This project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html) —
see [VERSIONING.md](VERSIONING.md) for the versioning policy.

---

## [Unreleased]

### Added
- **Curated honeynet profiles** (`honeynet_framework/profiles/`): A new profiles
  package with `HoneynetProfile`, `ReplayScript`, `ReplayStep`, and `ProfileBundle`
  data models.  `ProfileRegistry` loads built-in YAML bundle files and supports
  runtime registration and entry-point-based plugin discovery
  (`honeynet.honeynet_profile` group).  Five built-in profiles shipped across easy,
  medium, and hard difficulty tiers: `corporate_dmz_basic`, `corporate_dmz_extended`,
  `identity_federation_basic`, `data_exfiltration_target`, `ot_scada_basic`.  Each
  profile includes a `ReplayScript` with typed `ReplayStep` objects describing the
  deception surface declaratively without binding to a specific execution tool.
- **31 profile tests** (`tests/test_profiles.py`) covering model roundtrips, registry
  lifecycle, tag/difficulty filtering, built-in bundle loading, and structural
  validation of all shipped profiles.

- **Isolated execution plane** (`honeynet_framework/execution_policy.py`): Three
  independent security control layers — registry allowlist, digest pinning, and
  topology review gate — each independently configurable as `DISABLED`, `WARN`, or
  `ENFORCE`.  `ExecutionPolicy.enforce()` raises `PolicyViolationError` on blocked
  deployments.  `OrchestratorConfig.execution_policy` wires the policy into the
  deploy pipeline between image resolution and IaC rendering.  Defaults to `None`
  (no policy) for full backwards compatibility.  Exported from the top-level package.
- **34 execution policy tests** (`tests/test_execution_policy.py`) covering digest
  detection, registry extraction, allowlist enforcement, digest-pinning modes,
  topology gate, custom risk classifiers, `enforce()` error paths, and
  `OrchestratorConfig` field presence.

- **Plugin/provider ecosystem** (`honeynet_framework/plugins.py`): Entry-point-based
  plugin registry with stable ABCs for `CatalogPackPlugin`, `RepairStrategyPlugin`,
  and `EvaluationBackendPlugin`. Plugins are discovered via `importlib.metadata`
  entry points in groups `honeynet.catalog_pack`, `honeynet.repair_strategy`, and
  `honeynet.evaluation_backend`. Broken plugins are caught and logged without
  crashing the framework. All three ABCs are exported from the top-level package.
- **Reference plugin example** (`honeynet_framework/examples/plugin_example.py`):
  `IoTCatalogPack`, `IoTZoneRepairStrategy`, and `LocalDockerBackend` as concrete
  demonstrations of all three plugin types, plus `register_example_plugins()` for
  in-process registration.
- **Plugin entry-point groups declared** in `pyproject.toml` under
  `[project.entry-points."honeynet.*"]` with inline documentation comments.
- **28 plugin tests** (`tests/test_plugins.py`) covering: registry lifecycle,
  runtime registration, ABC enforcement, resilience to broken entry points,
  group-name constants, public API export, and reference implementation correctness.


- **Benchmark corpus v1**: 18 reproducible scenarios across easy / medium / hard
  difficulty tiers with `manifest.yaml` and structural validation tests
  (`tests/test_benchmark_corpus.py`).
- **`honeynet_framework/redaction.py`**: Central secret-redaction utilities
  (`redact_api_key`, `redact_mapping`, `redact_env_list`) for safe log output.
- **API-drift guard tests** in `tests/test_packaging.py`: 5 new assertions that
  catch README ↔ `OrchestratorConfig`/`LLMConfig` divergence at test time.
- **Safety-default tests** in `tests/test_safety_defaults.py`: 12 assertions
  covering judge fail-closed behaviour, network prune removal, and redaction.

### Changed
- **`semantic_judge._parse_failure_result`** now returns `passed=False`,
  `score=0.0`, and `severity="error"` instead of `passed=True, severity="warning"`.
  This is a **behaviour-breaking change** for callers relying on the previous
  fail-open default; set `judge_fail_on_error_findings=False` to restore
  permissive behaviour.  See [README Security Considerations](README.md#security-considerations).
- **`deployer.cleanup_docker_resources`**: removed global `docker network prune -f`.
  Only explicitly named project networks are removed; unrelated host networks
  are left untouched.
- **README** architecture diagram, overview steps, `OrchestratorConfig` table,
  and Python API example updated to reflect the actual deterministic pipeline
  (IaC output is `main.tofu.json`, not LLM-generated HCL).
- **`examples/basic_usage.py`**: updated docstring, `deploy_v2` → `deploy` (public API).
- **`__init__.py`** docstring: `deploy_v2` → `deploy`.
- **`cli.py`** `run_deploy`: `deploy_v2` → `deploy`.
- **`pyproject.toml`**: removed `github.com/example/...` placeholder URLs.

### Fixed
- `README.md` incorrectly documented `use_docker_for_tofu` default as `True`
  (actual default is `False`).
- `README.md` referenced non-existent `max_architect_attempts` field;
  replaced with correct `max_repair_attempts` / `max_total_attempts`.
- `README.md` referenced `requirements.txt` which does not exist in the
  repository; replaced with correct `pip install -e ".[dev]"` instruction.

---

## [0.1.0] — Initial Release (Alpha)

> **Note:** This is an alpha release.  The public API is subject to change
> without notice until the project reaches `1.0.0`.

### Added
- Natural-language → World Model → deterministic `main.tofu.json` pipeline.
- `WorldModelExtractor`, `WorldModelValidator`, `DeployCompiler`, `TofuRenderer`.
- Tiered deterministic World Model repair loop.
- Semantic Judge + Semantic Repair Loop.
- Docker-based honeynet deployment via OpenTofu.
- Interactive CLI (`honeynet`) with `prompt_toolkit`.
- Catalog system (`catalog/`) backed by `docker-library/official-images`.
- Evaluation framework (`evaluation/`) with scenario-fit scoring.
- QA runner for post-deploy port/service health checks.
- Telemetry collector and metrics reporter.
- Runtime contract checking.
- Log aggregation (optional, via Docker).
- Benchmark runner (`honeynet benchmark run`).

[Unreleased]: https://github.com/YOUR_ORG/honeynet-framework/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/YOUR_ORG/honeynet-framework/releases/tag/v0.1.0
