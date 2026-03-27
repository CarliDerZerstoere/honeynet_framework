# Benchmark Corpus v1

Reproducible benchmark scenarios for the Honeynet Framework evaluation pipeline.

## Design Goals

- **Reproducible**: same scenario, same metrics, across versions and model changes
- **Graded difficulty**: easy (single-domain) → medium (multi-tier) → hard (multi-zone, multi-system, identity)
- **Reusable**: outputs feed `evaluation/` tooling and can be used to compare Catalog, Repair, and Judge improvements
- **Token-cost aware**: each scenario has a `concise` prompt for cost estimation and a `detailed` prompt for quality comparison

## Corpus Structure

```
corpus_v1/
├── README.md          ← this file
├── manifest.yaml      ← machine-readable index
├── easy/              ← 6 scenarios, simple domain, ≤4 services, ≤2 zones
│   ├── e01_web_db.yaml
│   ├── e02_identity_only.yaml
│   ├── e03_web_cache_db.yaml
│   ├── e04_monitoring_stack.yaml
│   ├── e05_message_queue.yaml
│   └── e06_object_storage.yaml
├── medium/            ← 6 scenarios, multi-tier, 4–8 services, 2–4 zones
│   ├── m01_ecommerce.yaml
│   ├── m02_corporate_email.yaml
│   ├── m03_healthcare.yaml
│   ├── m04_devops_platform.yaml
│   ├── m05_supply_chain.yaml
│   └── m06_streaming_platform.yaml
└── hard/              ← 6 scenarios, multi-zone, 6–10+ services, identity/compliance
    ├── h01_banking_core.yaml
    ├── h02_telecom_mediation.yaml
    ├── h03_ml_platform.yaml
    ├── h04_zerotrust_enterprise.yaml
    ├── h05_iot_platform.yaml
    └── h06_government_portal.yaml
```

## Scenario Schema

Each `scenario.yaml` follows the `BenchmarkScenario` schema (see `evaluation/types.py`):

```yaml
id: <unique_id>
difficulty: easy | medium | hard
tags: [...]           # informational
description: >
  Short description.
prompts:
  - id: concise       # short prompt for cost estimation
    text: |
      ...
  - id: detailed      # richer prompt for quality evaluation
    text: |
      ...
reference:
  required_services:
    - id: <ref_id>
      kinds_any: [...]          # matches SystemKind values
      archetypes_any: [...]     # matches catalog archetypes
      roles_any: [...]          # substring-matched against simulate.role
      names_any: [...]          # substring-matched against system name
      deployable_only: true     # only count deployable systems
  required_zones:
    - id: <ref_id>
      names_any: [...]          # substring-matched against zone name
      exposure_any: [...]       # internal, public, internet, dmz, edge
      internal: true | false
  required_dependencies:
    - source_service_id: <required_service.id>
      target_service_id: <required_service.id>
      relation: depends_on
  forbidden_placements:
    - service_id: <required_service.id>
      zone_id: <required_zone.id>
```

## Metrics Collected

When running with `honeynet benchmark run`, the evaluation pipeline collects:

| Metric | Source | Notes |
|--------|--------|-------|
| `service_hit_rate` | scenario_fit.py | Fraction of required_services found |
| `zone_hit_rate` | scenario_fit.py | Fraction of required_zones found |
| `dependency_hit_rate` | scenario_fit.py | Fraction of required_dependencies satisfied |
| `forbidden_violation_count` | scenario_fit.py | Count of forbidden_placements violated |
| `validation_passed` | run_record | World model passed validation |
| `compilation_passed` | run_record | DeployProjection compiled successfully |
| `apply_passed` | run_record | `tofu apply` succeeded |
| `total_tokens` | run_record.token_totals | Sum across all LLM calls |
| `total_duration_s` | run_record | Wall-clock seconds |
| `cold_run` | benchmarked from first run per session | True if Ollama model was not pre-loaded |
| `world_model_repair_attempts` | run_record | Deterministic repair passes used |
| `semantic_judge_score` | run_record | Judge score 0–1 |

## Running the Benchmark

```bash
# Run all corpus_v1 scenarios (dry-run / no Docker deploy)
honeynet benchmark run \
  --scenario-dir benchmarks/corpus_v1 \
  --repeats 1 \
  --output benchmark_output/$(date +%Y%m%d)

# Run only easy scenarios
honeynet benchmark run \
  --scenario-dir benchmarks/corpus_v1/easy \
  --repeats 3 \
  --output benchmark_output/easy_run
```

## Extending the Corpus

1. Add a new `.yaml` file in the appropriate difficulty folder.
2. Follow the schema above.
3. Update `manifest.yaml`.
4. Run the benchmark to verify the new scenario loads and scores correctly.

## Versioning

This is **v1** of the corpus.  Breaking changes to the scenario schema or
reference format must produce a new version folder (`corpus_v2/`) so that
historical benchmark results remain comparable.
