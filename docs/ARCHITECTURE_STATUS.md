# Architektur-Status (Ist / Soll)

Kurze Matrix: was die **Roadmap** (`roadmap_alte_framework-ideen_e53f6ce3`) vorsieht vs. was im **aktuellen Code** unter `honeynet_framework/` existiert. Ältere Markdown-Guides (`FRAMEWORK_WORKFLOW.md`, `DEFAULT_PIPELINE_DE.md`) beschreiben teils **Zielarchitektur** — bei Widerspruch gilt der eingecheckte Code.

| Feature / Artefakt | Roadmap-Phase | Status im Repo | Hinweis |
|--------------------|---------------|----------------|---------|
| `failure_stage` + `run_id` auf `DeploymentResult` | P1 | Implementiert | `models/deployment.py`, `orchestrator.deploy()` |
| JSONL Telemetrie `telemetry/events.jsonl` | P1 | Implementiert | Flag `enable_telemetry_events` / CLI `--telemetry-events` |
| Mindest-Observability (`metrics.observability`) | P1 | Implementiert | `metrics.py`; stderr-Text in `errors`, nicht separates Feld |
| Modulare Validator-Registry | P2 | Implementiert | `validator.py` → `STRUCTURAL_RULES` |
| FailureReport JSON + Anbindung an Validation | P2 | Implementiert | `failure_report.py`; `from_validation_result()` classifies errors; saved as `validation_report.json` |
| `honeynet.repair_strategy` Entry-Points | P2 | Implementiert | `plugins/registry.py` + Orchestrator hook after validation failure |
| Semantic Judge nach Image-Repair | P3 | Implementiert | `semantic_judge.py`, optional, Artefakt `semantic_judge.json` |
| Catalog Snapshot + Loader | P4 | Implementiert | `catalog/snapshot.py`, Schema `docs/catalog_snapshot.schema.json` |
| Resolver nach Extraktion | P4 | Implementiert | `deploy.catalog_archetype` → `default_image`; `catalog_resolution.json` (YAML-`deploy` braucht weiterhin `image` für die Parser-Zweig) |
| `honeynet.catalog_pack` Plugins | P4 | Implementiert | `discover_catalog_packs()`; optionale `resolve(wm, snapshot)` |
| CLI `deploy --catalog-path` | P4 | Implementiert | `cli.py` |
| CLI `catalog` build/sync | P4 | Geplant / optional | Nicht implementiert; Pfad-Flag reicht laut Roadmap |
| Interactive `honeynet interactive` | optional | Fehlt | `prompt_toolkit` Dependency ungenutzt im CLI |
| Ist/Soll-Doku (diese Datei) | P0 | Implementiert | — |
| ADR-Ordner / Policy-ADR | P0 | Implementiert | `docs/adr/0001-policies-externalized.md` |
| Security-Abschnitt J (Trust, Redaction) | Plan | Implementiert (kurz) | `docs/SECURITY_BASELINE.md` |
| OpenTelemetry | optional Epic | Fehlt | JSONL statt OTLP |
| `strict_success` on DeploymentResult | — | Implementiert | `status == DEPLOYED` gate for automation |
| Deploy lock (concurrent protection) | — | Implementiert | asyncio Lock + file-based `.honeynet.lock` |
| Per-event `schema_version` in JSONL | — | Implementiert | `telemetry_events.py`; consumers filter by version |
| Healthcheck pipeline (LLM→Tofu) | — | Implementiert | prompt, extractor, compiler, renderer |
| QA runner (post-deploy checks) | — | Implementiert | `qa.py`; container, TCP, HTTP checks |
| Deployment metrics | — | Implementiert | `metrics.py`; deployability + scenario fit |

## Dokumente

| Datei | Rolle |
|-------|--------|
| `docs/ARCHITECTURE_STATUS.md` | Diese Matrix (Ist/Soll) |
| `docs/catalog_snapshot.schema.json` | JSON-Schema für Catalog-Dateien |
| `docs/DEFAULT_PIPELINE_DE.md` | Konzept/Phasen (teilweise Zielbild) |
| `docs/FRAMEWORK_WORKFLOW.md` | Ausführlicheres Zielbild; nicht alles im Code |
| `docs/adr/README.md` | ADR-Index |
| `docs/PLAN_PHASE_TEMPLATE.md` | Vorlage für Phasen-Tickets (Plan I.2) |
| `docs/SECURITY_BASELINE.md` | Mindestlinie Plugins/Secrets/Policies |

## YAGNI-Stufen (Roadmap)

| Stufe | Inhalt | Beispiele im Repo |
|-------|--------|-------------------|
| **P1 core** | Observability, maschinenlesbare Fehlerphasen | `failure_stage`, JSONL, `metrics.json` |
| **P2 internal-first** | Erweiterbarkeit ohne Monolith | `STRUCTURAL_RULES`, Plugin-Registry |
| **P3 / P4 optional** | LLM-Judge, Katalog | Flags default aus, CLI-Opt-Ins |

## API-Semantik: `DeploymentResult.success`

- `success` ist `True` für `DeploymentStatus.DEPLOYED` **und** `DeploymentStatus.RUNTIME_DEGRADED` (Deployment ist durchgelaufen; Laufzeit/QA können trotzdem abweichen).
- Für „alles grün“ prüft man **`status == DEPLOYED`** (und QA-/Runtime-Felder), nicht nur `success`. Telemetrie: `failure_stage` / `metrics.observability.failure_stage` nutzen.

## Parallelität / gleiches `work_dir`

- **Eine `HoneynetOrchestrator`-Instanz:** kein paralleles `deploy()` — `deployer.last_stage_results` und JSONL-Append sind nicht für Mehrfach-Deploys auf derselben Instanz serialisiert (best-effort).
- **Zwei Prozesse, gleiches Verzeichnis:** riskant (`cleanup`, geteilte `metrics.json` / `events.jsonl`); nicht unterstützt — separates `work_dir` pro Run oder externe Orchestrierung.

## Plugin-Gruppen (`pyproject.toml`)

- `honeynet.catalog_pack` — optionale `resolve(world_model, snapshot)`-Implementierungen
- `honeynet.repair_strategy` — für künftige Repair-Plugins
- `honeynet.evaluation_backend` — reserviert
