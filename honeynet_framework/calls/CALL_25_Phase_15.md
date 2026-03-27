# Call 25 — Phase 15: Liveness vs. Readiness — HealthContract

**Schwierigkeit:** ★★☆ mittel  
**Abhängigkeiten:** Call 12 (Phase 6b.5 — `HealthContract` in `ImageCatalogEntry`), Call 22 (Phase 12.5b — IR-Struktur)  
**Risiko:** mittel — Phase-1-Healthcheck-Snippet wird durch Phase-15.2-Snippet ersetzt

---

## Ziel

Phase-15.2-Healthcheck-Logik in `deploy_compiler.py` implementieren:
Zwei-Ebenen-Healthcheck (Liveness + Readiness). Readiness hat Priorität,
Liveness ist Fallback. Manueller Override via `sys_obj.deploy.health_contract`.

Konsolidierte Imports für alle neuen Katalog-Typen ergänzen.

---

## Dateien

### Zu editieren
- `honeynet_framework/deploy_compiler.py`

### Als Kontext mitgeben
- `deploy_compiler.py` vollständig
- `catalog/models.py` — Signaturen aller neuen Typen: `HealthContract`, `HoneytokenTemplate`, `VulnerabilityProfile`, `TrafficProfile`, `ExternalTrafficProfile`

---

## Änderungen

### 15.1 — Konsolidierte Imports in `deploy_compiler.py`

```python
from .catalog.models import (
    HealthContract,
    HoneytokenTemplate,      # Phase 9
    VulnerabilityProfile,    # Phase 12
    TrafficProfile,          # Phase 11
    ExternalTrafficProfile,  # Phase 16
)
```

### 15.2 — Phase-1-Snippet ersetzen durch Phase-15.2-Snippet

Im Container-Loop von `_phase4_generate_ir()`, nach dem Entry-Lookup,
den bestehenden Healthcheck-Block (der `health_hints` verwendete) ersetzen:

```python
hc_from_catalog = entry.health_contract if entry and entry.health_contract else None
sys_obj = world_model.systems.get(c["name"])
hc_manual_dict = (
    sys_obj.deploy.health_contract
    if sys_obj and sys_obj.deploy
       and getattr(sys_obj.deploy, "health_contract", None)
    else {}
)

if hc_manual_dict:
    # Manueller Override: dict-zu-HealthContract
    hc = HealthContract(**{
        k: v for k, v in hc_manual_dict.items()
        if k in HealthContract.__dataclass_fields__
    })
else:
    hc = hc_from_catalog

healthcheck = None
wait = False

if hc and hc.readiness_test:
    # Readiness hat Priorität — OpenTofu wartet auf healthy
    healthcheck = {
        "test":         hc.readiness_test,
        "interval":     hc.readiness_interval,
        "timeout":      hc.readiness_timeout,
        "retries":      hc.readiness_retries,
        "start_period": hc.readiness_start_period,
    }
    wait = True
elif hc and hc.liveness_test:
    # Liveness als Fallback
    healthcheck = {
        "test":     hc.liveness_test,
        "interval": hc.liveness_interval,
        "timeout":  hc.liveness_timeout,
        "retries":  hc.liveness_retries,
    }
    wait = True
```

**`DeployContainer`-Konstruktion:** `healthcheck` und `wait` bereits durch
Phase-1-Erweiterung (Call 10) übergeben — sicherstellen dass die Werte
aus dem neuen Snippet verwendet werden.

---

## Smoke-Test nach dem Call

```python
# HealthContract mit readiness_test → Readiness-Healthcheck im IR
# HealthContract nur mit liveness_test → Liveness-Healthcheck im IR
# Kein HealthContract → healthcheck=None, wait=False

# Manueller Override schlägt Katalog-Wert
# sys.deploy.health_contract = {"readiness_test": ["CMD", "custom"]}
# entry.health_contract = HealthContract(readiness_test=["CMD", "catalog"])
# → healthcheck["test"] == ["CMD", "custom"]  (Manual wins)
```

---

## Hinweis zu `health_hints`

`health_hints` bleibt in `PROVENANCE_LLM_ENRICHED` bis Phase 15 vollständig
abgeschlossen ist. Nach diesem Call kann `health_hints` aus dem Enricher-Schema
und der PROVENANCE-Liste entfernt werden, wenn alle Deployments auf
`health_contract` migriert sind.
