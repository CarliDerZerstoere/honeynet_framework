# Call 06 — Phase 2.4: Repair-Loops normalisieren

**Schwierigkeit:** ★★☆ mittel  
**Abhängigkeiten:** Call 01 (Phase 2.1), Call 05 (Phase 2.2 — `apply_catalog_defaults_to_system` mit `archetype_index`)  
**Risiko:** mittel — sechs bestehende Aufrufstellen in zwei Dateien ändern

---

## Ziel

In `world_model_repair.py` und `semantic_repair.py` den
`build_archetype_lookup_index`-Index einmal in `repair()` bauen und an alle
internen `apply_catalog_defaults_to_system`-Aufrufe durchreichen.
**Kein neuer globaler Sweep, kein verändertes Reparaturverhalten** —
nur die Normalisierung wird konsistent.

---

## Dateien

### Zu editieren
- `honeynet_framework/world_model_repair.py` (~500 Zeilen)
- `honeynet_framework/semantic_repair.py` (~400 Zeilen)

### Als Kontext mitgeben
- Beide Dateien vollständig (werden direkt editiert)
- Signatur von `apply_catalog_defaults_to_system` (aus Call 05)
- Signatur von `build_archetype_lookup_index` (aus Call 01)

---

## Änderungen

### `world_model_repair.py`

**Lazy Import + Index in `repair()`:**
```python
def repair(self, world_model, validation_result, catalog,
           repair_hint=None):
    if not catalog:
        return world_model
    from .catalog_inference import build_archetype_lookup_index
    index = build_archetype_lookup_index(catalog)  # einmal hier

    new_systems = dict(world_model.systems)

    # L42 — erster System-Sweep:
    for name, system in list(new_systems.items()):
        new_systems[name] = apply_catalog_defaults_to_system(
            system, catalog, archetype_index=index  # ← ergänzt
        )

    for err in list(validation_result.errors) + list(validation_result.warnings):
        if err.rule == 'CONTRACT_REQUIRED_ENV':
            system_name = self._extract_system_name(err.details)
            if system_name and system_name in new_systems:
                # L48
                new_systems[system_name] = apply_catalog_defaults_to_system(
                    new_systems[system_name], catalog, archetype_index=index  # ← ergänzt
                )
        # L95 (gleicher Pattern, archetype_index=index ergänzen)
```

**`_add_system_for_candidate` — Signatur erweitern:**
```python
@staticmethod
def _add_system_for_candidate(
    world_model, systems, candidate, catalog,
    archetype_index=None,  # NEU
) -> None:
    # ... unverändert bis zur letzten Zeile ...
    systems[name] = apply_catalog_defaults_to_system(
        system, catalog, archetype_index=archetype_index  # ← ergänzt
    )
```

**Aufruf von `_add_system_for_candidate` in `repair()` ebenfalls anpassen:**
```python
self._add_system_for_candidate(
    world_model, new_systems, candidate, catalog, archetype_index=index
)
```

### `semantic_repair.py`

**Lazy Import + Index in `repair()`:**
```python
def repair(self, world_model, judge_result, catalog):
    from .catalog_inference import build_archetype_lookup_index
    index = build_archetype_lookup_index(catalog) if catalog else None

    # L88 — Archetype-Tausch:
    systems[system_name] = apply_catalog_defaults_to_system(
        replace(system, deploy=new_deploy), catalog, archetype_index=index  # ← ergänzt
    )
```

**`_add_missing_core_service` — Signatur erweitern:**
```python
def _add_missing_core_service(
    self, world_model, systems, context, catalog,
    archetype_index=None,  # NEU
) -> bool:
    # ... unverändert bis zur letzten Zeile vor return True ...
    systems[name] = apply_catalog_defaults_to_system(
        system, catalog, archetype_index=archetype_index  # ← ergänzt
    )
```

**Aufruf von `_add_missing_core_service` in `repair()` anpassen:**
```python
self._add_missing_core_service(
    world_model, systems, context, catalog, archetype_index=index
)
```

---

## Smoke-Test nach dem Call

```python
# Import-Test: keine zirkulären Imports
from honeynet_framework.world_model_repair import WorldModelRepairLoop
from honeynet_framework.semantic_repair import SemanticRepairLoop

# Repair mit Katalog — kein Fehler
loop = WorldModelRepairLoop()
# result = loop.repair(world_model, validation_result, catalog)
```

---

## Was NICHT geändert wird

- `world_model_validator.py` (→ Call 07)
- `repair/strategies.py` — wird **nicht** angefasst (falsches Domain)
