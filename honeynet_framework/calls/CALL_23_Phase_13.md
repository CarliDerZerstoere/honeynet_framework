# Call 23 — Phase 13: Zweistufiger Repair-Loop

**Schwierigkeit:** ★★★ hoch  
**Abhängigkeiten:** Call 15 (Phase 6.3 — Repair-Loop in Orchestrator), Call 06 (Phase 2.4)  
**Risiko:** hoch — Abbruch-Heuristik muss korrekt abgestimmt sein; Stage-1 vs. Stage-2-Grenze

---

## Ziel

`world_model_repair.py` um zweistufige Eskalation erweitern:
- `MAX_REPAIR_ATTEMPTS_STAGE_1` und `MAX_REPAIR_ATTEMPTS_STAGE_2` Konstanten
- `_build_repair_prompt()` komprimiert in Stage 1, vollständig in Stage 2
- `_repair_is_making_progress()` lässt Stage 1 bedingungslos durch; Stage 2 erfordert Fortschritt

In `orchestrator.py` den bestehenden Repair-Loop durch die zweistufige Version ersetzen.

**[G3/G4]:** Helfer kommen in `world_model_repair.py` — NICHT in `repair/strategies.py`.
`repair/strategies.py` wird nicht angefasst.

---

## Dateien

### Zu editieren
- `honeynet_framework/world_model_repair.py`
- `honeynet_framework/orchestrator.py`

### Als Kontext mitgeben
- `world_model_repair.py` vollständig
- `orchestrator.py` — Repair-Loop-Block (L587–595)

---

## Änderungen

### 13.1 — `world_model_repair.py`: Konstanten + Helfer

```python
MAX_REPAIR_ATTEMPTS_STAGE_1 = 2
MAX_REPAIR_ATTEMPTS_STAGE_2 = 4


def _build_repair_prompt(
    errors: list["ValidationError"],
    attempt: int,
    full_error_output: Optional[str] = None,
) -> str:
    if attempt <= MAX_REPAIR_ATTEMPTS_STAGE_1:
        # Stage 1: komprimierter Summary
        summary = "; ".join(
            f"{e.rule}: {e.details[:80]}" for e in errors[:5]
        )
        return f"Fix these validation errors: {summary}"
    else:
        # Stage 2: vollständige Fehlerliste
        lines = [f"- [{e.rule}] {e.details}" for e in errors]
        prompt = "Fix ALL of the following errors:\n" + "\n".join(lines)
        if full_error_output:
            prompt += f"\n\nFull error output:\n{full_error_output}"
        return prompt


def _repair_is_making_progress(
    prev_error_count: int,
    curr_error_count: int,
    attempt: int,
) -> bool:
    # [REV33-P1] Stage 1 läuft bedingungslos durch — hier sammeln wir Informationen.
    # Ab Stage 2 (attempt > MAX_REPAIR_ATTEMPTS_STAGE_1) muss die Fehlerzahl sinken.
    #
    # Beispiele (konstanten-relativ formuliert):
    #   attempt <= MAX_REPAIR_ATTEMPTS_STAGE_1, prev=10, curr=10
    #       → True  (Stage 1, letzter freier Versuch)
    #   attempt = MAX_REPAIR_ATTEMPTS_STAGE_1 + 1, prev=10, curr=5
    #       → True  (erster Stage-2-Versuch, Fehler gesunken)
    #   attempt = MAX_REPAIR_ATTEMPTS_STAGE_1 + 1, prev=10, curr=10
    #       → False (erster Stage-2-Versuch, kein Fortschritt → Abort)
    if attempt <= MAX_REPAIR_ATTEMPTS_STAGE_1:
        return True  # Stage 1: immer weitermachen
    return curr_error_count < prev_error_count  # Stage 2: Fortschritt erforderlich
```

### 13.2 — `world_model_repair.py`: `repair()` Signatur erweitern

```python
def repair(
    self,
    world_model: WorldModel,
    validation_result: "ValidationResult",
    catalog: Optional["CatalogSnapshot"] = None,
    repair_hint: Optional[str] = None,  # ignoriert (rule-based Repair)
) -> WorldModel:
    # ... bestehende Logik unverändert ...
    # repair_hint legt die API für künftige LLM-gestützte Erweiterung fest.
```

### 13.3 — `orchestrator.py`: Repair-Loop ersetzen

**Import ergänzen:**
```python
from .world_model_repair import (
    _build_repair_prompt,
    _repair_is_making_progress,
    MAX_REPAIR_ATTEMPTS_STAGE_1,
    MAX_REPAIR_ATTEMPTS_STAGE_2,
)
```

**Repair-Loop-Block (L587–595) ersetzen:**
```python
if self.config.world_model_repair_mode and not validation_result.passed:
    wm_repair = WorldModelRepairLoop()
    prev_error_count = len(validation_result.errors)
    max_attempts = MAX_REPAIR_ATTEMPTS_STAGE_1 + MAX_REPAIR_ATTEMPTS_STAGE_2

    for attempt in range(1, max_attempts + 1):
        if validation_result.passed:
            break
        if not _repair_is_making_progress(prev_error_count,
                                           len(validation_result.errors),
                                           attempt):
            logger.warning(
                "Repair-Loop abgebrochen: kein Fortschritt in Attempt %s "
                "(%s → %s Fehler)", attempt, prev_error_count,
                len(validation_result.errors),
            )
            break

        repair_prompt = _build_repair_prompt(
            validation_result.errors,
            attempt=attempt,
        )
        world_model = wm_repair.repair(
            world_model, validation_result, catalog_snapshot,
            repair_hint=repair_prompt,
        )
        prev_error_count = len(validation_result.errors)
        validation_result = self.world_model_validator.validate(
            world_model, catalog=catalog_snapshot
        )
```

---

## Smoke-Test nach dem Call

```python
from honeynet_framework.world_model_repair import (
    _repair_is_making_progress,
    _build_repair_prompt,
    MAX_REPAIR_ATTEMPTS_STAGE_1,
    MAX_REPAIR_ATTEMPTS_STAGE_2,
)

# Stage 1: immer True
assert _repair_is_making_progress(10, 10, attempt=1) is True
assert _repair_is_making_progress(10, 10, attempt=MAX_REPAIR_ATTEMPTS_STAGE_1) is True

# Stage 2: Fortschritt erforderlich
assert _repair_is_making_progress(10, 5, attempt=MAX_REPAIR_ATTEMPTS_STAGE_1 + 1) is True
assert _repair_is_making_progress(10, 10, attempt=MAX_REPAIR_ATTEMPTS_STAGE_1 + 1) is False
assert _repair_is_making_progress(10, 10,
    attempt=MAX_REPAIR_ATTEMPTS_STAGE_1 + MAX_REPAIR_ATTEMPTS_STAGE_2) is False

# Prompt-Eskalation
class FakeError:
    rule = "X"; details = "y" * 100
errors = [FakeError()] * 3
p1 = _build_repair_prompt(errors, attempt=1)
p2 = _build_repair_prompt(errors, attempt=MAX_REPAIR_ATTEMPTS_STAGE_1 + 1)
assert len(p1) < len(p2)  # Stage 2 ist ausführlicher

# Import-Quelle: aus world_model_repair, NICHT aus repair.strategies
try:
    from honeynet_framework.repair.strategies import _build_repair_prompt as _bad
    assert False, "Sollte nicht importierbar sein"
except ImportError:
    pass  # korrekt
```
