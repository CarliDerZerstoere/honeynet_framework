# Call 11 — Phase 6b.1–6b.4: models.py — to_dict() für alle neuen Felder

**Schwierigkeit:** ★☆☆ einfach  
**Abhängigkeiten:** Call 08 (Phase 4.1 — neue Felder in `DeployContainer`)  
**Risiko:** gering — mechanische Serialisierungsergänzungen, keine Logik

---

## Ziel

`to_dict()` auf `SystemDeploy`, `Port`, `DeployContainer` und
`DeployProjection` so erweitern, dass alle neuen Felder korrekt
serialisiert werden. Kritische Korrekturen:
- `Port.to_dict()` muss `protocol` und `ip` einschließen
- `DeployContainer.to_dict()` muss `p.to_dict()` statt Inline-Dict verwenden

---

## Dateien

### Zu editieren
- `honeynet_framework/models.py` (~900 Zeilen)

### Als Kontext mitgeben
- `models.py` vollständig (direkt editiert)

---

## Änderungen

### 6b.1 — `SystemDeploy.to_dict()`: `vulnerability_profile` ergänzen

```python
def to_dict(self) -> dict:
    result = { ... }  # bestehende Felder
    if self.vulnerability_profile:
        result["vulnerability_profile"] = self.vulnerability_profile
    return result
```

### 6b.2 — `Port.to_dict()`: vollständige Serialisierung

```python
@dataclass
class Port:
    internal: int
    external: int
    protocol: str = "tcp"
    ip: Optional[str] = None

    def to_dict(self) -> dict:
        d = {"internal": self.internal, "external": self.external,
             "protocol": self.protocol}
        if self.ip is not None:
            d["ip"] = self.ip
        return d
```

### 6b.3 — `DeployContainer.to_dict()`: Ports + alle neuen Felder

**[G10-FIX] Kritisch:** bestehende Inline-Dict-Zeile ersetzen:
```python
# VORHER (falsch — schneidet protocol und ip ab):
# result["ports"] = [{"internal": p.internal, "external": p.external} for p in self.ports]

# NACHHER (korrekt):
result["ports"] = [p.to_dict() for p in self.ports]
```

**Neue Felder ergänzen:**
```python
if self.archetype:
    result["archetype"] = self.archetype
if self.network_aliases:
    result["network_aliases"] = self.network_aliases
if self.hostname:
    result["hostname"] = self.hostname
if self.healthcheck:
    result["healthcheck"] = self.healthcheck
if self.wait:
    result["wait"] = self.wait
    result["wait_timeout"] = self.wait_timeout
if self.upload_files:
    result["upload_files"] = self.upload_files
if self.network_mode:
    result["network_mode"] = self.network_mode
```

### 6b.4 — `DeployProjection.to_dict()`: `backbone_network_name`

```python
def to_dict(self) -> dict:
    result = { ... }  # bestehende Felder
    if self.backbone_network_name:
        result["backbone_network_name"] = self.backbone_network_name
    return result
```

---

## Smoke-Test nach dem Call

```python
from honeynet_framework.models import Port, DeployContainer

# Port.to_dict() enthält protocol und ip
p = Port(internal=5432, external=5433, protocol="tcp", ip="0.0.0.0")
assert p.to_dict() == {
    "internal": 5432, "external": 5433, "protocol": "tcp", "ip": "0.0.0.0"
}

# Port ohne ip
p2 = Port(internal=80, external=8080, protocol="tcp")
assert "ip" not in p2.to_dict()

# DeployContainer.to_dict() nutzt p.to_dict()
c = DeployContainer(
    name="pg", image="postgres:15",
    ports=[p]
)
d = c.to_dict()
assert d["ports"][0]["protocol"] == "tcp"
assert d["ports"][0]["ip"] == "0.0.0.0"
```
