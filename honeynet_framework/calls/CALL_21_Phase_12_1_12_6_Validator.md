# Call 21 — Phase 12.1–12.6 (Validator-Teil): Schwachstellen-Validation

**Schwierigkeit:** ★★☆ mittel  
**Abhängigkeiten:** Call 02 (Phase 0.1), Call 12 (Phase 6b.5 — `VulnerabilityProfile`), Call 01 (Phase 2.1)  
**Risiko:** mittel — neue Validator-Methode mit vier expliziten Fällen

---

## Ziel

`WorldModelValidator` um Schwachstellen-Profil-Validierung erweitern.
Vier Fälle klar trennen:
1. kein Katalog → gutgläubig warnen
2. Archetyp unbekannt → schweigen (Archetyp-Validator spricht)
3. Entry vorhanden, Profil nicht gefunden → `VULN_PROFILE_UNKNOWN`-Error
4. Profil aufgelöst → `HONEYNET_VULNERABILITY_ACTIVE`-Warning

Zusätzlich: `SystemDeploy` und `Port` um neue Felder erweitern (12.3 + 12.4).

---

## Dateien

### Zu editieren
- `honeynet_framework/models.py` — `SystemDeploy.vulnerability_profile`, `Port.ip`
- `honeynet_framework/world_model_validator.py` — Profil-Validierung

### Als Kontext mitgeben
- `models.py` — `SystemDeploy`, `Port`
- `world_model_validator.py` vollständig
- `catalog/models.py` — Signatur `VulnerabilityProfile`, `get_catalog_entry_for_archetype`

---

## Änderungen

### 12.3 — `models.py`: `SystemDeploy`

```python
@dataclass
class SystemDeploy:
    # ... bestehende Felder ...
    vulnerability_profile: Optional[str] = None
```

### 12.4 — `models.py`: `Port`

```python
@dataclass
class Port:
    internal: int
    external: int
    protocol: str = "tcp"
    ip: Optional[str] = None
    # ip="0.0.0.0" = absichtliche externe Exposition via vulnerability_profile
    # ip=None = normale Port-Bindung (kein Override)
```

### 12.6 — `world_model_validator.py`: Profil-Validierung

Im Loop über `wm.systems` (in `_validate_security_policy` oder eigenem Abschnitt):

```python
vuln_name = system.deploy.vulnerability_profile
if vuln_name:
    archetype = getattr(deploy, "archetype", None) or ""

    entry = None
    active_vuln = None
    if catalog and archetype:
        entry = get_catalog_entry_for_archetype(archetype, catalog)
        if entry:
            active_vuln = next(
                (p for p in (getattr(entry, "vulnerability_profiles", None) or [])
                 if p.name == vuln_name),
                None,
            )

    # Vier explizit getrennte Fälle (REV36-P2):
    if not catalog:
        # Fall 1: kein Katalog — gutgläubig warnen.
        warnings.append(ValidationError(
            rule="HONEYNET_VULNERABILITY_ACTIVE",
            details=(
                f"System '{sys_name}' hat aktives Schwachstellenprofil "
                f"'{vuln_name}'. Nur in isolierten Honeynet-Umgebungen deployen."
            ),
            severity="WARNING",
            fix_hint="Stelle sicher dass das Deployment-Netzwerk isoliert ist (Honeywall).",
        ))
    elif entry is None:
        pass  # Fall 2: Archetyp unbekannt — Archetyp-Validator spricht allein.
    elif active_vuln is None:
        # Fall 3: Entry gefunden, Profil darin nicht → Tippfehler.
        errors.append(ValidationError(
            rule="VULN_PROFILE_UNKNOWN",
            details=(
                f"System '{sys_name}' referenziert vulnerability_profile "
                f"'{vuln_name}', das für archetype '{archetype}' im Katalog "
                f"nicht gefunden wurde."
            ),
            severity="ERROR",
            fix_hint=(
                f"Tippfehler im Profilnamen prüfen oder Katalog-Eintrag für "
                f"'{archetype}' um das Profil ergänzen."
            ),
        ))
    else:
        # Fall 4: Profil aufgelöst → Warning.
        warnings.append(ValidationError(
            rule="HONEYNET_VULNERABILITY_ACTIVE",
            details=(
                f"System '{sys_name}' hat aktives Schwachstellenprofil "
                f"'{vuln_name}'. Nur in isolierten Honeynet-Umgebungen deployen."
            ),
            severity="WARNING",
            fix_hint="Stelle sicher dass das Deployment-Netzwerk isoliert ist (Honeywall).",
        ))
```

---

## Smoke-Test nach dem Call

```python
# Fall 3: Tippfehler → VULN_PROFILE_UNKNOWN, nicht HONEYNET_VULNERABILITY_ACTIVE
# system.deploy.vulnerability_profile = "defualt_creds"  (Tippfehler)
# Katalog vorhanden, Entry vorhanden, Profil "defualt_creds" nicht im Katalog
# → errors enthält VULN_PROFILE_UNKNOWN
# → warnings enthält KEIN HONEYNET_VULNERABILITY_ACTIVE

# Fall 2: Archetyp unbekannt → keine Profil-Fehler
# system.deploy.archetype = "unknown_xyz"
# system.deploy.vulnerability_profile = "defualt_creds"
# → weder VULN_PROFILE_UNKNOWN noch HONEYNET_VULNERABILITY_ACTIVE

# Fall 4: Profil korrekt aufgelöst → nur Warning
# system.deploy.vulnerability_profile = "default_creds"  (korrekt)
# → warnings enthält HONEYNET_VULNERABILITY_ACTIVE
# → errors enthält KEIN VULN_PROFILE_UNKNOWN
```
