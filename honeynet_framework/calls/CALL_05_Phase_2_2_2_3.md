# Call 05 — Phase 2.2 + 2.3: Normalisierung in apply_catalog_defaults + Extractor

**Schwierigkeit:** ★★☆ mittel  
**Abhängigkeiten:** Call 01 (Phase 2.1 — `normalize_expanded_archetype`, `build_archetype_lookup_index`)  
**Risiko:** mittel — bestehende Kernfunktion `apply_catalog_defaults_to_system` erweitert

---

## Ziel

Archetype-Normalisierung an **allen drei Eintrittspunkten** einbauen:
1. `apply_catalog_defaults_to_system` in `catalog_inference.py` — normaler Parse-Pfad
2. `_apply_catalog_defaults` im Extractor — Index einmal bauen + durchreichen
3. `_merge_expansion_payload` im Extractor — Expansions-Payload-Pfad

---

## Dateien

### Zu editieren
- `honeynet_framework/catalog_inference.py` (~850 Zeilen)
- `honeynet_framework/world_model_extractor.py`

### Als Kontext mitgeben
- `catalog_inference.py` — Signaturen von `_pep503_normalize`, `build_archetype_lookup_index`, `normalize_expanded_archetype` (aus Call 01)
- `catalog_inference.py` — voller Body von `apply_catalog_defaults_to_system`
- `world_model_extractor.py` — Signaturen + Body von `_apply_catalog_defaults` und `_merge_expansion_payload`

---

## Änderungen

### 2.2 — `catalog_inference.py`: `apply_catalog_defaults_to_system` erweitern

**Signatur um `archetype_index` erweitern:**
```python
def apply_catalog_defaults_to_system(
    system: System,
    catalog: Optional["CatalogSnapshot"],
    archetype_index: Optional[dict] = None,  # NEU — gecachter Index
) -> System:
```

**Vor dem bestehenden `get_catalog_entry_for_archetype`-Aufruf einfügen:**
```python
deploy = system.deploy
archetype = str(deploy.archetype or "").strip()

# [F1] Normalisierung vor dem Katalog-Lookup.
# Löst "kafka" → "apache/kafka" für ALLE Parsepfade.
if archetype:
    normalized = normalize_expanded_archetype(archetype, catalog, archetype_index)
    if normalized and normalized != archetype:
        logger.debug(
            "Archetype normalised: '%s' → '%s' for system '%s'",
            archetype, normalized, system.name,
        )
        archetype = normalized
        deploy = replace(deploy, archetype=archetype)
    elif normalized is None and archetype:
        # Unbekannt oder mehrdeutig — logger.warning für frühe Sichtbarkeit.
        # Der harte Fehler kommt vom Validator (POLICY_ARCHETYPE).
        logger.warning(
            "Archetype '%s' for system '%s' could not be resolved to a "
            "unique catalog entry — using as-is. Validator will raise "
            "POLICY_ARCHETYPE error if strict_catalog=True (default).",
            archetype, system.name,
        )

entry = get_catalog_entry_for_archetype(archetype, catalog) if archetype else None
# ... restlicher Code unverändert ...
```

### 2.3 — `world_model_extractor.py`: Extractor

**Import ergänzen:**
```python
from .catalog_inference import (
    normalize_expanded_archetype,
    build_archetype_lookup_index,
)
```

**`_apply_catalog_defaults()` — Index einmal bauen und durchreichen:**
```python
def _apply_catalog_defaults(
    self,
    world_model: WorldModel,
    catalog_context: "CatalogSnapshot",
) -> WorldModel:
    index = build_archetype_lookup_index(catalog_context)  # ← NEU: einmal bauen
    for name, system in list(world_model.systems.items()):
        world_model.systems[name] = apply_catalog_defaults_to_system(
            system, catalog_context, archetype_index=index  # ← NEU: durchreichen
        )
    return world_model
```

**`_expand_world_model_scope()` — Index an `_merge_expansion_payload` übergeben:**
```python
archetype_index = (
    build_archetype_lookup_index(catalog_context) if catalog_context else None
)
added = self._merge_expansion_payload(
    ..., catalog=catalog_context, archetype_index=archetype_index,
)
```

**`_merge_expansion_payload()` — Normalisierung vor `SystemDeploy()`:**
```python
def _merge_expansion_payload(
    self, ..., catalog=None, archetype_index=None,
):
    # ...
    raw_archetype = str(payload.get("archetype", "")).strip()
    if raw_archetype and catalog:
        normalized = normalize_expanded_archetype(raw_archetype, catalog, archetype_index)
        if normalized:
            raw_archetype = normalized
    # SystemDeploy(archetype=raw_archetype, ...)
```

---

## Smoke-Test nach dem Call

```python
# Mit echtem Katalog, der "postgres" als kanonisch und
# "postgresql" als Alias registriert hat:
from honeynet_framework.catalog_inference import apply_catalog_defaults_to_system

system_with_typo = ...  # System mit deploy.archetype="postgresql"
result = apply_catalog_defaults_to_system(system_with_typo, catalog)
assert result.deploy.archetype == "postgres"  # normalisiert
```

---

## Was NICHT geändert wird

- `world_model_repair.py` und `semantic_repair.py` (→ Call 06)
- `world_model_validator.py` (→ Call 07)
