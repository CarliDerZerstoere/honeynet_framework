# Call 15 — Phase 6.3 + 6.4: orchestrator.py + cli.py — Ehrliche Deploy-Ergebnisse

**Schwierigkeit:** ★★★ hoch  
**Abhängigkeiten:** Call 13 (Phase 6.1), Call 14 (Phase 6.2 — `RuntimeVerifier`)  
**Risiko:** hoch — Kern-Orchestrierungslogik; nur deterministischer Pfad!

---

## Ziel

Im deterministischen Deploy-Pfad (`_deploy_deterministic_v2`) die
`RuntimeVerifier`-Integration einbauen, Warnings akkumulieren und
`DeploymentResult` mit `status`, `runtime_summary` und `warnings` befüllen.
In `cli.py` die Ausgabe auf `fully_healthy` / `success` umstellen.

**Scope:** Nur `iac_mode="deterministic"`. `_deploy_with_repair_loop_v2()`
(iac_mode="llm"-Pfad) wird **nicht** angefasst.

---

## Dateien

### Zu editieren
- `honeynet_framework/orchestrator.py` (~950 Zeilen)
- `honeynet_framework/cli.py`

### Als Kontext mitgeben
- `orchestrator.py` vollständig
- `cli.py` vollständig
- `models.py` — `DeploymentStatus`, `RuntimeSummary`, `DeploymentResult`
- `deployer.py` — Signatur `RuntimeVerifier.verify()`

---

## Änderungen

### 6.3 — `orchestrator.py`

**[F1] Import + Initialisierung:**
```python
from .deployer import RuntimeVerifier

# In initialize():
self._runtime_verifier = RuntimeVerifier(llm=self.llm)
```

**[F1] `validation_result` nach Revert neu berechnen (im Semantic-Repair-Block):**
```python
if not validation_result.passed:
    logger.warning("Semantic repair produced an invalid world model; reverting ...")
    world_model = pre_repair_world_model
    # [F1] validation_result auf revertiertes Modell zurücksetzen
    validation_result = self.world_model_validator.validate(
        world_model, catalog=catalog_snapshot
    )
    break
```

**Warning-Akkumulation (beginnt bei L687, nach dem Semantic-Repair-Block):**
```python
result = DeploymentResult(status=DeploymentStatus.PENDING, judge_result=judge_result)
accumulated_warnings: list[str] = []

if validation_result.warnings:
    accumulated_warnings.extend(w.details for w in validation_result.warnings)

# Nach compiler.compile():
accumulated_warnings.extend(str(w) for w in compiler.get_warnings())

# Port-Remap (L712):
if remapped_ports:
    remap_msg = (
        "Host-Port-Remaps vor dem Render: "
        + ", ".join(f"{name}:{old}→{new}" for name, old, new in remapped_ports[:20])
    )
    logger.warning(remap_msg)
    accumulated_warnings.append(remap_msg)

if self.config.enable_registry_resolution:
    # ... bestehende soft_unresolved-Logik ...
    if soft_unresolved:
        soft_msg = (
            "Weiche Image-Resolution-Fehler (Deployment fortgesetzt): "
            + ", ".join(f"{e.image_ref}:{e.status.value}" for e in soft_unresolved[:10])
        )
        logger.warning(soft_msg)
        accumulated_warnings.append(soft_msg)
```

**Ergebnis-Bau (nach `deployer.verify_deployment`):**
```python
verification = await self.deployer.verify_deployment(container_names) \
    if self.deployer else {}
runtime_summary = await self._runtime_verifier.verify(verification)

if runtime_summary.is_total_failure:
    status = DeploymentStatus.FAILED
elif runtime_summary.is_healthy and verification.get("success"):
    status = DeploymentStatus.DEPLOYED
else:
    status = DeploymentStatus.RUNTIME_DEGRADED

result.status = status
result.runtime_summary = runtime_summary
result.running_containers = runtime_summary.total_running
result.expected_containers = runtime_summary.total_expected
result.warnings = accumulated_warnings
result.summary = runtime_summary.human_summary()
return result
```

### 6.4 — `cli.py`

```python
if result.fully_healthy:
    print(f"\nOK: Deployment vollständig erfolgreich")
    print(f"  Container: {result.running_containers}/{result.expected_containers}")
elif result.success:
    rs = result.runtime_summary
    print(f"\nWARN: Apply erfolgreich — Runtime degradiert")
    print(f"  Container: {rs.total_running}/{rs.total_expected} laufen")
    for category, names_in_cat in rs.failure_groups.items():
        print(f"  {len(names_in_cat)}x {category}: {', '.join(names_in_cat)}")
else:
    print(f"\nFAIL: Deployment fehlgeschlagen")
    for error in result.errors[:5]:
        print(f"  - {error}")

# Warnings in ALLEN Pfaden anzeigen
if result.warnings:
    print("\nWarnungen:")
    for w in result.warnings[:10]:
        print(f"  ! {w}")

return 0 if result.success else 1
```

---

## Smoke-Test nach dem Call

```python
from honeynet_framework.models import DeploymentStatus, DeploymentResult

result = DeploymentResult(status=DeploymentStatus.DEPLOYED)
assert result.fully_healthy is True
assert result.success is True

result2 = DeploymentResult(status=DeploymentStatus.RUNTIME_DEGRADED)
assert result2.success is True
assert result2.fully_healthy is False
```
