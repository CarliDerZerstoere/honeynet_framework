# Call 07 — Phase 1.1 + 1.2: Companion-Validator

**Schwierigkeit:** ★★☆ mittel  
**Abhängigkeiten:** Call 01 (Phase 2.1), Call 02 (Phase 0.1 — `required_companion_archetypes`)  
**Risiko:** mittel — neue Validierungsmethode, kein bestehender Code verändert

---

## Ziel

`world_model_validator.py` um eine neue Methode `_validate_service_dependencies`
erweitern, die prüft ob alle `required_companion_archetypes` eines Systems im
WorldModel vorhanden sind. Fehlendes Companion → ERROR, vorhandenes aber ohne
`depends_on` → WARNING.

---

## Dateien

### Zu editieren
- `honeynet_framework/world_model_validator.py` (~900 Zeilen)

### Als Kontext mitgeben
- `world_model_validator.py` vollständig (direkt editiert)
- Signaturen: `build_archetype_lookup_index`, `normalize_expanded_archetype` (Call 01)
- Signatur: `get_catalog_entry_for_archetype` aus `catalog_inference.py`
- `ValidationError`-Dataclass-Felder aus `models.py`

---

## Änderungen

### 1.1 — Import ergänzen

```python
from .catalog_inference import (
    # ...bestehende Imports...
    build_archetype_lookup_index,
    normalize_expanded_archetype,
)
```

### 1.1 — Neue Methode `_validate_service_dependencies`

```python
def _validate_service_dependencies(
    self,
    wm: WorldModel,
    catalog: Optional["CatalogSnapshot"],
) -> tuple[list[ValidationError], list[ValidationError]]:
    if not catalog:
        return [], []

    errors: list[ValidationError] = []
    warnings: list[ValidationError] = []

    index = build_archetype_lookup_index(catalog)

    def _canonical(raw: str) -> str:
        result = normalize_expanded_archetype(raw, catalog, index)
        return result if result else raw.strip().lower()

    deployed_archetypes: set[str] = {
        _canonical(sys.deploy.archetype)
        for sys in wm.systems.values()
        if sys.deploy and sys.deploy.archetype
    }

    archetype_to_names: dict[str, list[str]] = {}
    for name, sys in wm.systems.items():
        if sys.deploy and sys.deploy.archetype:
            canon = _canonical(sys.deploy.archetype)
            archetype_to_names.setdefault(canon, []).append(name)

    for sys_name, system in wm.systems.items():
        if not system.deploy or not system.deploy.archetype:
            continue

        canonical_self = _canonical(system.deploy.archetype)
        entry = get_catalog_entry_for_archetype(canonical_self, catalog)
        if not entry:
            continue

        seen_companions: set[str] = set()
        companions: list[str] = []
        for c in (entry.required_companion_archetypes or []):
            if not str(c).strip():
                continue
            canon_c = _canonical(c)
            if canon_c not in seen_companions:
                seen_companions.add(canon_c)
                companions.append(canon_c)

        for companion in companions:
            if companion not in deployed_archetypes:
                errors.append(ValidationError(
                    rule="CONTRACT_COMPANION_MISSING",
                    details=(
                        f"System '{sys_name}' "
                        f"(archetype '{system.deploy.archetype}') "
                        f"benötigt Companion-Service '{companion}', "
                        f"der im WorldModel fehlt."
                    ),
                    severity="ERROR",
                    fix_hint=f"Füge archetype='{companion}' zum WorldModel hinzu.",
                ))
            else:
                providers = archetype_to_names.get(companion, [])
                existing_depends_on = set(system.deploy.depends_on or [])

                if len(providers) == 1:
                    provider_name = providers[0]
                    if provider_name not in existing_depends_on:
                        warnings.append(ValidationError(
                            rule="CONTRACT_COMPANION_NO_DEPENDS_ON",
                            details=(
                                f"System '{sys_name}' benötigt '{companion}' "
                                f"('{provider_name}'), hat aber keinen "
                                f"depends_on-Eintrag."
                            ),
                            severity="WARNING",
                            fix_hint=(
                                f"Ergänze '{provider_name}' in "
                                f"deploy.depends_on für '{sys_name}'."
                            ),
                        ))
                elif len(providers) > 1:
                    if not existing_depends_on.intersection(providers):
                        warnings.append(ValidationError(
                            rule="CONTRACT_COMPANION_NO_DEPENDS_ON",
                            details=(
                                f"System '{sys_name}' benötigt '{companion}', "
                                f"aber es existieren {len(providers)} Provider "
                                f"({', '.join(providers[:3])}). Kein eindeutiger "
                                f"depends_on ableitbar."
                            ),
                            severity="WARNING",
                            fix_hint=(
                                f"Setze depends_on manuell auf den gewünschten "
                                f"Provider für '{sys_name}'."
                            ),
                        ))

    return errors, warnings
```

### 1.2 — Einbindung in `validate()`

Am Ende der `validate()`-Methode, vor dem `return`-Statement:
```python
# 10. Service-Dependency-Contracts
if catalog:
    dep_errors, dep_warnings = self._validate_service_dependencies(wm, catalog)
    errors.extend(dep_errors)
    warnings.extend(dep_warnings)
```

---

## Verhalten des Repair-Loops

`CONTRACT_COMPANION_NO_DEPENDS_ON` ist eine WARNING, kein ERROR.
`validation_result.passed` hängt nur an `errors`, nicht an `warnings`.
Eine reine Warning löst den Repair-Loop **nicht** aus — das ist gewünscht.
`CONTRACT_COMPANION_MISSING` ist ein ERROR und löst den Repair-Loop aus.

---

## Smoke-Test nach dem Call

```python
from honeynet_framework.world_model_validator import WorldModelValidator

# Companion fehlt → ERROR
# Companion vorhanden, depends_on fehlt → WARNING
# Kein Katalog → keine Companion-Checks
validator = WorldModelValidator()
result = validator.validate(world_model, catalog=None)
assert all(e.rule != "CONTRACT_COMPANION_MISSING" for e in result.errors)
```
