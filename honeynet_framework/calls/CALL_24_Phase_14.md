# Call 24 — Phase 14: Security-Compliance-Validator + Port-ip-Prüfung

**Schwierigkeit:** ★★☆ mittel  
**Abhängigkeiten:** Call 21 (Phase 12.1–12.6 Validator), Call 22 (Phase 12.5–12.5b Compiler — finale `ports`-Liste)  
**Risiko:** mittel — zwei Stellen; beide müssen finale IR-Liste verwenden, nicht `c["ports"]`

---

## Ziel

`_validate_security_policy` in `world_model_validator.py` implementieren:
`POLICY_ZONE_ISOLATION` nur feuern wenn das Profil tatsächlich aufgelöst wurde.

In `deploy_compiler.py`: Port-Exposition-Warning auf finaler `ports`-Liste prüfen,
Bedingung auf `not active_profile` (nicht `not vuln_profile_name`).

---

## Dateien

### Zu editieren
- `honeynet_framework/world_model_validator.py`
- `honeynet_framework/deploy_compiler.py`

### Als Kontext mitgeben
- `world_model_validator.py` vollständig
- `deploy_compiler.py` — Container-Loop in `_phase4_generate_ir()` (Port-Loop-Abschnitt)

---

## Änderungen

### 14.1 — `world_model_validator.py`: `_validate_security_policy`

```python
def _validate_security_policy(
    self,
    wm: WorldModel,
    catalog: Optional["CatalogSnapshot"],
) -> tuple[list[ValidationError], list[ValidationError]]:
    errors: list[ValidationError] = []
    warnings: list[ValidationError] = []
    EXTERNAL_EXPOSURES = {"internet", "dmz"}

    for sys_name, system in wm.systems.items():
        if not system.deploy:
            continue
        deploy = system.deploy
        zone_name = getattr(deploy, "zone", "") or ""
        vuln_profile_name = getattr(deploy, "vulnerability_profile", None)

        zone_obj = wm.zones.get(zone_name) if zone_name else None
        exposure = (
            zone_obj.simulate.exposure
            if zone_obj and zone_obj.simulate
            else "internal"
        )

        # [REV34-P2] Nur aufgelöstes Profil verwenden.
        # Tippfehler → VULN_PROFILE_UNKNOWN (Phase 12.6) deckelt das ab.
        # POLICY_ZONE_ISOLATION nicht für unaufgelöstes Profil feuern.
        active_vuln = None
        if vuln_profile_name and catalog:
            archetype = getattr(deploy, "archetype", None) or ""
            if archetype:
                entry = get_catalog_entry_for_archetype(archetype, catalog)
                if entry:
                    active_vuln = next(
                        (p for p in (getattr(entry, "vulnerability_profiles", None) or [])
                         if p.name == vuln_profile_name),
                        None,
                    )

        if active_vuln and exposure not in EXTERNAL_EXPOSURES:
            errors.append(ValidationError(
                rule="POLICY_ZONE_ISOLATION",
                details=(
                    f"System '{sys_name}' hat vulnerability_profile "
                    f"'{vuln_profile_name}' in Zone '{zone_name}' "
                    f"(exposure='{exposure}'). "
                    f"Externe Port-Exposition in nicht-externer Zone."
                ),
                severity="ERROR",
                fix_hint=(
                    f"Verschiebe '{sys_name}' in eine Zone mit "
                    f"exposure='internet' oder 'dmz', oder entferne "
                    f"das vulnerability_profile."
                ),
            ))

    return errors, warnings
```

**Einbindung in `validate()`:**
```python
# Security-Policy-Check
if catalog:
    sec_errors, sec_warnings = self._validate_security_policy(wm, catalog)
    errors.extend(sec_errors)
    warnings.extend(sec_warnings)
```

### 14.2 — `deploy_compiler.py`: Port-ip-Prüfung

**Nach dem Port-Loop aus Phase 12.5b** (nachdem `ports`-Liste aufgebaut ist):

```python
# [REV31-G13] Prüft finale ports-Liste (Port-Objekte), nicht c["ports"] (dicts).
# [REV33-P2] not active_profile — Tippfehler liefert vuln_profile_name != ""
#            aber active_profile=None; Warning muss trotzdem feuern.
for p in ports:
    if p.ip == "0.0.0.0" and not active_profile:
        self.warnings.append(CompilerError(
            phase="IR",
            message=(
                f"Container '{c.get('name')}' hat Port {p.external} auf "
                f"0.0.0.0 gebunden ohne aktives vulnerability_profile. "
                f"Unbeabsichtigte externe Exposition?"
            ),
            system=c.get("name", ""),
        ))
```

---

## Smoke-Test nach dem Call

```python
# POLICY_ZONE_ISOLATION nur bei aufgelöstem Profil
# system.deploy.vulnerability_profile = "defualt_creds"  (Tippfehler)
# system.deploy.zone = "internal"
# Katalog vorhanden, Entry vorhanden
# → VULN_PROFILE_UNKNOWN-Error (aus Call 21)
# → KEIN POLICY_ZONE_ISOLATION

# system.deploy.vulnerability_profile = "default_creds"  (korrekt)
# system.deploy.zone = "internal"
# → POLICY_ZONE_ISOLATION-Error

# Port-Exposition-Warning: Container mit p.ip="0.0.0.0" UND active_profile → keine Warning
# Container mit p.ip="0.0.0.0" UND active_profile=None → Warning
# Container mit vulnerability_profile="defualt_creds" (Tippfehler) → active_profile=None → Warning
```
