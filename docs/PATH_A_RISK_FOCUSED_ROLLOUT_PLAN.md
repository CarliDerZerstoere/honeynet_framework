# Path A Risk-Focused Rollout Plan

## Scope and Intent

This plan defines a safe rollout for **Path A** (deterministic, catalog-driven deployment path and related strictness/policy improvements) in `workflow_analysis`.

Primary goals:
- improve operational safety and deterministic behavior;
- protect compatibility and migration outcomes for existing prompts/world models;
- avoid developer frustration from over-strict defaults or abrupt breakages.

Out of scope:
- `iac_mode="llm"` legacy path (kept stable, not actively expanded).

---

## Risk Register (Top Risks to Control)

1. **False-negative strictness:** valid scenarios blocked by strict catalog/policy checks.
2. **False-positive permissiveness:** risky or non-deployable configurations pass unnoticed.
3. **Migration regressions:** existing prompts/world models degrade or fail under new normalization/validation semantics.
4. **Operational blast radius:** increased deployment failures in `validate/plan/apply` due to stricter upstream assumptions.
5. **Developer productivity loss:** noisy warnings/errors and unclear remediation creating rule fatigue.

Mitigation principle: **warn first, enforce later**, with per-feature kill switches and clear diagnostics.

---

## Phased Rollout

## Phase 0 - Baseline and Safety Harness (1 week)

**Objective:** no behavior changes yet; establish observability, comparability, and rollback controls.

- Add run tags to telemetry/metrics:
  - `path_a_enabled` (bool)
  - `path_a_feature_set` (string list)
  - `path_a_policy_mode` (`off|warn|enforce`)
- Create baseline dashboards for:
  - failure stages (`catalog_snapshot`, `world_model_validation`, `deploy_compilation`, `image_resolution`, `init/validate/plan/apply`);
  - `strict_success` rate;
  - runtime degraded rate.
- Build golden regression suite:
  - representative prompts (simple/medium/complex/edge);
  - stored world models from successful historical runs.
- Add one-command fallback profile: `PATH_A_POLICY_MODE=off` and `PATH_A_ENABLE=false`.

**Gate to next phase:** baseline is stable and regression suite reproducible.

---

## Phase 1 - Dark Launch (Read/Analyze only, no enforcement) (1-2 weeks)

**Objective:** run Path A logic in parallel for diagnostics only.

- Enable Path A internals in shadow mode:
  - normalization/indexing;
  - companion/policy checks;
  - expanded profile handling.
- Emit diagnostics as structured warnings and telemetry fields, but do not block pipelines.
- Compare old vs. Path A outcomes:
  - normalized archetype deltas;
  - policy findings that would have blocked runs;
  - repair loop behavior changes.

**Developer UX constraints:**
- warnings must include `rule`, impacted `system`, and concrete fix hint;
- deduplicate repeated messages per system/rule.

**Gate to next phase:** <5% of baseline-success runs show high-severity "would-block" deltas without actionable fix hints.

---

## Phase 2 - Opt-In Enforcement for Early Adopters (2 weeks)

**Objective:** production-like validation with explicit opt-in.

- Default remains permissive (`warn`).
- Enable enforcement only for:
  - CI lane `path-a-canary`;
  - selected developer teams via config or CLI flag.
- Introduce bounded enforcement:
  - strict catalog checks enforce only after successful archetype normalization attempts;
  - unknown/ambiguous archetypes produce one deterministic error category with guidance.
- Roll out staged rule classes:
  - Stage A: archetype normalization + unknown archetype errors;
  - Stage B: companion consistency and profile resolution checks;
  - Stage C: zone/isolation and exposure policy enforcement.

**Gate to next phase:** canary `strict_success` no worse than baseline by more than 3%, and no sustained spike in `world_model_validation`.

---

## Phase 3 - Default-On Warn, Selective Enforce (2 weeks)

**Objective:** broad adoption while keeping low-friction defaults.

- Set `PATH_A_ENABLE=true` by default.
- Keep `PATH_A_POLICY_MODE=warn` default globally.
- Enforce only high-confidence, high-safety rules by default:
  - invalid/unknown vulnerability profile reference with clear catalog-backed evidence;
  - invalid network mode combinations that produce non-valid deploy specs.
- All other strict rules remain warn-level unless team opts into enforce profile.

**Developer UX constraints:**
- every enforce error links to short remediation docs;
- "quick fix" suggestions in logs/messages (e.g., canonical archetype candidates).

**Gate to next phase:** support volume and median time-to-fix remain within predefined thresholds.

---

## Phase 4 - Progressive Enforcement by Environment (ongoing)

**Objective:** stronger production safety without blocking local iteration.

- Local/dev default: `warn`.
- CI/staging default: mixed (`enforce` for critical rules only).
- Production automation default: `enforce` for all approved stable Path A rules.
- Keep emergency rollback:
  - env var or config toggle to drop to `warn` immediately;
  - per-rule override list for temporary suppression.

---

## Feature Flags and Defaults

Recommended flags:

- `PATH_A_ENABLE` (default: `false` -> `true` in Phase 3)
  - global switch for Path A code paths.
- `PATH_A_POLICY_MODE` (default: `warn`)
  - values: `off|warn|enforce`.
- `PATH_A_RULES_ENFORCED` (default: empty)
  - comma-separated stable rule IDs allowed to enforce.
- `PATH_A_NORMALIZATION_MODE` (default: `safe`)
  - `off|safe|aggressive`; start with `safe` only.
- `PATH_A_MIGRATION_ASSIST` (default: `true`)
  - emits canonicalization and compatibility hints.
- `PATH_A_REPAIR_BUDGET_PROFILE` (default: `conservative`)
  - prevents aggressive auto-repair loops from hiding root cause.

Default profile by stage:
- Phases 0-2: `PATH_A_ENABLE=true` only in canary/shadow, `PATH_A_POLICY_MODE=warn`.
- Phase 3: `PATH_A_ENABLE=true` globally, `warn` globally, selective `enforce`.
- Phase 4: environment-based enforce matrix.

---

## Compatibility Strategy

1. **Backward-compatible parsing first**
   - accept legacy archetype naming variants;
   - normalize internally to canonical forms;
   - preserve original user intent fields where possible.

2. **Stable schema handling**
   - support previous catalog/world model schema versions through explicit adapters;
   - reject only when safe transformation is impossible.

3. **Deterministic resolution behavior**
   - if ambiguous mapping occurs, fail with explicit alternatives rather than implicit remapping.

4. **No silent behavior changes**
   - any auto-normalization must be logged and surfaced in run artifacts.

5. **Legacy path safety**
   - keep `iac_mode="llm"` behavior unchanged; no coupling to Path A strict policy.

---

## Migration Strategy for Existing Prompts and World Models

## Prompt Migration

- Build a prompt corpus from historical successful deployments.
- Replay in three lanes:
  - Baseline (current behavior),
  - Path A warn mode,
  - Path A selective enforce.
- Classify diffs:
  - benign normalization;
  - policy violations with auto-fix suggestion;
  - hard blockers requiring prompt contract updates.
- Publish a "prompt migration cookbook":
  - canonical archetype references;
  - companion dependency expression patterns;
  - vulnerability/profile naming conventions.

## World Model Migration

- Add migration CLI utility (or script) with dry-run:
  - normalize archetype identifiers;
  - validate profile and companion references;
  - emit patch suggestions and risk annotations.
- Migration pipeline:
  1. backup existing artifacts;
  2. run adapter/normalizer;
  3. run validator in warn mode;
  4. run validator in enforce preview;
  5. approve and persist migrated artifact.
- Keep dual-read support for at least one full release cycle.

## Compatibility SLA

- "No forced break" policy in first release:
  - all legacy prompts/world models must either run or fail with deterministic, actionable errors and documented escape hatch (`warn` mode or rule override).

---

## Operational Safety Controls

- **Blast-radius control:** canary-first, low percentage rollout, environment gating.
- **Fast rollback:** global and per-rule switches, no redeploy required.
- **Error budget guardrails:** automatic halt of further enforcement if:
  - `strict_success` drops >3% vs baseline over 24h;
  - `world_model_validation` errors rise >2x baseline;
  - production `apply` failure stage increases materially.
- **Deterministic auditability:** all normalization and policy decisions written to artifacts and telemetry.
- **Runbook readiness:** on-call guide for top failure classes and immediate downgrade steps.

---

## Developer Friction Prevention

- Default to `warn` in local workflows.
- Keep "strict-by-default" only for high-confidence, high-impact safety violations.
- Ship concise, human-readable fix hints in every new rule.
- Provide one-click/local-script downgrade profile for urgent unblock.
- Track friction metrics explicitly:
  - median time from first error to successful rerun;
  - number of repeated same-rule failures per developer;
  - support tickets per rule ID.

Design standard for new rules:
- if confidence < high and remediation ambiguous -> `warn`, not `enforce`.

---

## Success Metrics

## Safety and Reliability

- `strict_success` (status `DEPLOYED`) delta vs baseline.
- `RUNTIME_DEGRADED` rate.
- failure-stage distribution stability.
- percentage of runs with unresolved critical policy findings.

## Migration Quality

- prompt replay pass rate (warn and enforce lanes).
- world model migration success rate without manual edits.
- rate of ambiguous normalization outcomes.

## Developer Experience

- median time-to-green after first Path A finding.
- repeated finding rate (same rule, same workflow).
- rollback/downgrade toggle usage trend (should decline over time).
- subjective satisfaction pulse from early adopter teams.

---

## Rollout Decision Checklist (Go/No-Go per phase)

- Baseline and canary metrics within guardrails.
- No unresolved high-severity operational risks.
- Developer friction metrics acceptable and non-worsening.
- Rollback tested and verified.
- Migration playbook/documentation updated for current rule set.

If any criterion fails: pause enforcement expansion, revert affected rules to `warn`, and issue targeted fixes before continuing.
