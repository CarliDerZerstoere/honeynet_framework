# Call 08 — Phase 4.1: models.py — DeployContainer Netzwerk-Felder

**Schwierigkeit:** ★☆☆ einfach  
**Abhängigkeiten:** keine (parallel zu anderen Calls möglich)  
**Risiko:** gering — neue optionale Felder mit Default-Werten

---

## Ziel

`DeployContainer` in `models.py` um vier neue Netzwerk-Felder erweitern.
`network_mode` muss **hier** eingeführt werden (nicht in Phase 16.2), damit
der Renderer-Guard in Call 10 (Phase 4.3) sofort funktioniert — sonst
`AttributeError` beim Rendern.

---

## Dateien

### Zu editieren
- `honeynet_framework/models.py` (~900 Zeilen)

### Als Kontext mitgeben
- Bestehende `DeployContainer`-Klassendefinition in `models.py`

---

## Änderungen

### `DeployContainer` — neue Felder ergänzen

```python
@dataclass
class DeployContainer:
    name: str
    image: str
    # NEU — Netzwerk-Felder (Phase 4)
    networks: list[str] = field(default_factory=list)
    network_aliases: dict[str, list[str]] = field(default_factory=dict)
    # network_aliases-Format: {network_name: [alias1, alias2]}
    # Beispiel: {"hn_project_default": ["postgres", "db"]}
    hostname: Optional[str] = None
    network_mode: Optional[str] = None
    # "host" = Container nutzt Host-Netzwerk direkt.
    # Nur für externe Traffic-Generatoren (Phase 16) — nie für Honeynet-Services.
    # Feld hier in Phase 4 eingeführt, damit Phase 4.3 (Renderer-Guard) sofort
    # funktioniert. Phase 16.2 nutzt das Feld, führt es nicht erneut ein.
    # ... restliche bestehende Felder unverändert ...
```

**Wichtig:** Neue Felder mit Default-Werten nach bestehenden Feldern OHNE
Default anfügen (`name` und `image` müssen weiterhin vorne stehen).
Reihenfolge der bestehenden Felder nicht verändern.

### `Optional` sicherstellen

`from typing import Optional` muss importiert sein.

---

## Smoke-Test nach dem Call

```python
from honeynet_framework.models import DeployContainer

# Neue Felder mit Defaults
c = DeployContainer(name="nginx", image="nginx:latest")
assert c.networks == []
assert c.network_aliases == {}
assert c.hostname is None
assert c.network_mode is None

# Felder setzen
c2 = DeployContainer(
    name="pg",
    image="postgres:15",
    networks=["hn_default"],
    network_aliases={"hn_default": ["postgres", "db"]},
    hostname="pg-host",
    network_mode=None,
)
assert c2.networks == ["hn_default"]
assert c2.network_aliases["hn_default"] == ["postgres", "db"]
```

---

## Was NICHT geändert wird

- Keine Healthcheck-Felder (→ Phase 1, in `deploy_compiler.py` + `tofu_renderer.py`)
- `to_dict()` für diese Felder → Call 11 (Phase 6b.3)
- `DeployProjection` → Call 16 (Phase 7.5)
