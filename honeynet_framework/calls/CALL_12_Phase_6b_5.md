# Call 12 — Phase 6b.5: catalog/models.py — Katalog-Serialisierung (to_dict + from_dict)

**Schwierigkeit:** ★☆☆ einfach  
**Abhängigkeiten:** Call 02 (Phase 0.1 — `ImageCatalogEntry` Basisstruktur)  
**Risiko:** gering — mechanisch, aber umfangreich; Roundtrip-Tests essenziell

---

## Ziel

`ImageCatalogEntry.to_dict()` und `from_dict()` für alle Katalog-Typen
der Phasen 7–16 ergänzen. Ohne diese Ergänzungen gehen neue Felder nach
einem Builder-Cache-Roundtrip verloren. Zusätzlich die
`PROVENANCE_LLM_ENRICHED`-Liste aktualisieren.

**Hinweis:** Die neuen Datenklassen (`HoneytokenTemplate`, `VulnerabilityProfile`,
`TrafficProfile`, `ExternalTrafficProfile`, `HealthContract`) werden in diesem
Call ebenfalls in `catalog/models.py` definiert, auch wenn ihre Compiler-Logik
erst in späteren Calls implementiert wird.

---

## Dateien

### Zu editieren
- `honeynet_framework/catalog/models.py` (~1.000 Zeilen)

### Als Kontext mitgeben
- `catalog/models.py` vollständig

---

## Änderungen

### Neue Datenklassen definieren (falls noch nicht vorhanden)

```python
@dataclass
class HoneytokenTemplate:
    type: str      # "file" | "env" | "db_init_script"
    path: str
    template: str

@dataclass
class VulnerabilityProfile:
    name: str
    description: str
    env_overrides: dict[str, str] = field(default_factory=dict)
    expose_ports_externally: bool = False

@dataclass
class TrafficProfile:
    type: str          # "http" | "postgres" | "redis" | "dns"
    interval_seconds: int = 30
    commands: list[str] = field(default_factory=list)
    generator_image: str = ""
    generator_user: Optional[str] = None
    generator_password: Optional[str] = None
    generator_env_overrides: dict[str, str] = field(default_factory=dict)

@dataclass
class ExternalTrafficProfile:
    type: str
    generator_image: str = ""
    interval_seconds: int = 60
    commands: list[str] = field(default_factory=list)
    generator_user: Optional[str] = None
    generator_password: Optional[str] = None
    generator_env_overrides: dict[str, str] = field(default_factory=dict)

@dataclass
class HealthContract:
    liveness_test: list[str] = field(default_factory=list)
    liveness_interval: str = "30s"
    liveness_timeout: str = "5s"
    liveness_retries: int = 3
    readiness_test: list[str] = field(default_factory=list)
    readiness_interval: str = "10s"
    readiness_timeout: str = "3s"
    readiness_retries: int = 5
    readiness_start_period: str = "15s"
```

### Neue Felder in `ImageCatalogEntry`

```python
@dataclass
class ImageCatalogEntry:
    # ... bestehende Felder ...
    log_paths: list[str] = field(default_factory=list)
    honeytoken_templates: list[HoneytokenTemplate] = field(default_factory=list)
    traffic_profile: Optional[TrafficProfile] = None
    vulnerability_profiles: list[VulnerabilityProfile] = field(default_factory=list)
    health_contract: Optional[HealthContract] = None
    external_traffic_profile: Optional[ExternalTrafficProfile] = None
```

### `to_dict()` ergänzen

```python
if self.log_paths:
    result["log_paths"] = list(self.log_paths)
if self.honeytoken_templates:
    result["honeytoken_templates"] = [
        {"type": t.type, "path": t.path, "template": t.template}
        for t in self.honeytoken_templates
    ]
if self.vulnerability_profiles:
    result["vulnerability_profiles"] = [
        {"name": p.name, "description": p.description,
         "env_overrides": dict(p.env_overrides),
         "expose_ports_externally": p.expose_ports_externally}
        for p in self.vulnerability_profiles
    ]
if self.traffic_profile:
    tp = self.traffic_profile
    result["traffic_profile"] = {
        "type": tp.type, "interval_seconds": tp.interval_seconds,
        "commands": list(tp.commands), "generator_image": tp.generator_image,
        "generator_user": tp.generator_user,
        "generator_password": tp.generator_password,
        "generator_env_overrides": dict(tp.generator_env_overrides),
    }
if self.external_traffic_profile:
    etp = self.external_traffic_profile
    result["external_traffic_profile"] = {
        "type": etp.type, "generator_image": etp.generator_image,
        "interval_seconds": etp.interval_seconds,
        "commands": list(etp.commands), "generator_user": etp.generator_user,
        "generator_password": etp.generator_password,
        "generator_env_overrides": dict(etp.generator_env_overrides),
    }
if self.health_contract:
    hc = self.health_contract
    result["health_contract"] = {
        "liveness_test": list(hc.liveness_test),
        "liveness_interval": hc.liveness_interval,
        "liveness_timeout": hc.liveness_timeout,
        "liveness_retries": hc.liveness_retries,
        "readiness_test": list(hc.readiness_test),
        "readiness_interval": hc.readiness_interval,
        "readiness_timeout": hc.readiness_timeout,
        "readiness_retries": hc.readiness_retries,
        "readiness_start_period": hc.readiness_start_period,
    }
```

### `from_dict()` ergänzen (defensiv mit `.get()`)

```python
entry.log_paths = list(data.get("log_paths", []))

# [REV32-P2] Pflichtfelder via .get(); Einträge ohne "path" überspringen
raw_tokens = data.get("honeytoken_templates", [])
entry.honeytoken_templates = [
    HoneytokenTemplate(
        type=t.get("type", ""),
        path=t.get("path", ""),
        template=t.get("template", ""),
    )
    for t in raw_tokens
    if isinstance(t, dict) and t.get("type") and t.get("path")
]

raw_tp = data.get("traffic_profile")
if isinstance(raw_tp, dict):
    entry.traffic_profile = TrafficProfile(
        type=raw_tp.get("type", ""),
        interval_seconds=int(raw_tp.get("interval_seconds", 30)),
        commands=list(raw_tp.get("commands", [])),
        generator_image=raw_tp.get("generator_image", ""),
        generator_user=raw_tp.get("generator_user"),
        generator_password=raw_tp.get("generator_password"),
        generator_env_overrides=dict(raw_tp.get("generator_env_overrides", {})),
    )

# [REV32-P2] Einträge ohne "name" überspringen
entry.vulnerability_profiles = [
    VulnerabilityProfile(
        name=p.get("name", ""),
        description=p.get("description", ""),
        env_overrides=dict(p.get("env_overrides", {})),
        expose_ports_externally=bool(p.get("expose_ports_externally", False)),
    )
    for p in data.get("vulnerability_profiles", [])
    if isinstance(p, dict) and p.get("name")
]

raw_hc = data.get("health_contract")
if isinstance(raw_hc, dict):
    entry.health_contract = HealthContract(
        liveness_test=list(raw_hc.get("liveness_test", [])),
        liveness_interval=raw_hc.get("liveness_interval", "30s"),
        liveness_timeout=raw_hc.get("liveness_timeout", "5s"),
        liveness_retries=int(raw_hc.get("liveness_retries", 3)),
        readiness_test=list(raw_hc.get("readiness_test", [])),
        readiness_interval=raw_hc.get("readiness_interval", "10s"),
        readiness_timeout=raw_hc.get("readiness_timeout", "3s"),
        readiness_retries=int(raw_hc.get("readiness_retries", 5)),
        readiness_start_period=raw_hc.get("readiness_start_period", "15s"),
    )

raw_etp = data.get("external_traffic_profile")
if isinstance(raw_etp, dict):
    entry.external_traffic_profile = ExternalTrafficProfile(
        type=raw_etp.get("type", ""),
        generator_image=raw_etp.get("generator_image", ""),
        interval_seconds=int(raw_etp.get("interval_seconds", 60)),
        commands=list(raw_etp.get("commands", [])),
        generator_user=raw_etp.get("generator_user"),
        generator_password=raw_etp.get("generator_password"),
        generator_env_overrides=dict(raw_etp.get("generator_env_overrides", {})),
    )
```

### `PROVENANCE_LLM_ENRICHED`-Liste aktualisieren (ca. L914)

```python
PROVENANCE_LLM_ENRICHED = [
    "required_env", "default_env",
    "health_hints",             # bleibt bis Phase 15 abgeschlossen ist
    "health_contract",          # NEU Phase 15
    "network_aliases",          # NEU Phase 0
    "required_companion_archetypes",  # NEU Phase 0
    "log_paths",                # NEU Phase 7
    "honeytoken_templates",     # NEU Phase 9
    "traffic_profile",          # NEU Phase 11
    "vulnerability_profiles",   # NEU Phase 12
    "external_traffic_profile", # NEU Phase 16
    "capability_description", "category", "startup_notes",
    "startup_requirement", "health_check",
]
```

---

## Smoke-Test nach dem Call

```python
from honeynet_framework.catalog.models import (
    ImageCatalogEntry, HoneytokenTemplate, VulnerabilityProfile,
    TrafficProfile, HealthContract
)

# Vollständiger Roundtrip
entry = ImageCatalogEntry(archetype="postgres")
entry.log_paths = ["/var/log/postgresql/postgresql.log"]
entry.honeytoken_templates = [HoneytokenTemplate("file", "/etc/.pgpass", "pass={{random_password}}")]
entry.vulnerability_profiles = [VulnerabilityProfile("default_creds", "weak pw")]
entry.health_contract = HealthContract(readiness_test=["CMD", "pg_isready"])

d = entry.to_dict()
e2 = ImageCatalogEntry.from_dict(d)
assert e2.log_paths == entry.log_paths
assert e2.honeytoken_templates[0].path == "/etc/.pgpass"
assert e2.vulnerability_profiles[0].name == "default_creds"
assert e2.health_contract.readiness_test == ["CMD", "pg_isready"]

# Defensiv: fehlendes "path" → leer
e3 = ImageCatalogEntry.from_dict({"honeytoken_templates": [{"type": "file"}]})
assert e3.honeytoken_templates == []

# Defensiv: fehlendes "name" → leer
e4 = ImageCatalogEntry.from_dict({"vulnerability_profiles": [{"description": "x"}]})
assert e4.vulnerability_profiles == []
```
