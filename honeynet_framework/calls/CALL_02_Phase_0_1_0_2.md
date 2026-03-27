# Call 02 — Phase 0.1 + 0.2: Katalogerweiterung — neue Felder + Schema-Version

**Schwierigkeit:** ★★☆ mittel  
**Abhängigkeiten:** keine (parallel zu Call 01 möglich)  
**Risiko:** mittel — bestehende Datenklassen erweitert, Roundtrip-Kompatibilität prüfen

---

## Ziel

`catalog/models.py` um drei neue Felder erweitern: `schema_version`,
`network_aliases`, `required_companion_archetypes`. Neue Schema-Versions-Konstante
einführen. `ArchetypeContract` synchron halten. `to_dict()` + `from_dict()`
für alle drei Felder ergänzen.

---

## Dateien

### Zu editieren
- `honeynet_framework/catalog/models.py` (~1.000 Zeilen)

### Als Kontext mitgeben
- Gesamte Datei `catalog/models.py` (wird direkt editiert)

---

## Änderungen

### 0.1 — `ImageCatalogEntry` + Konstante

Konstante am Anfang der Datei (nach Importen) ergänzen:

```python
CATALOG_SCHEMA_VERSION = "2"
```

In `ImageCatalogEntry` neue Felder ergänzen (nach bestehenden Feldern):

```python
@dataclass
class ImageCatalogEntry:
    # --- alle bestehenden Felder unverändert ---
    schema_version: str = ""
    network_aliases: list[str] = field(default_factory=list)
    # Liste statt String — mehrere Pflicht-Abhängigkeiten möglich
    # (z.B. Notary benötigt notary-signer UND mysql)
    required_companion_archetypes: list[str] = field(default_factory=list)
```

In `ImageCatalogEntry.to_dict()` ergänzen:
```python
if self.schema_version:
    result["schema_version"] = self.schema_version
if self.network_aliases:
    result["network_aliases"] = list(self.network_aliases)
if self.required_companion_archetypes:
    result["required_companion_archetypes"] = list(self.required_companion_archetypes)
```

In `ImageCatalogEntry.from_dict()` ergänzen (nach dem bestehenden `entry = cls(...)` Block):
```python
entry.schema_version = data.get("schema_version", "")
entry.network_aliases = list(data.get("network_aliases", []))
entry.required_companion_archetypes = list(data.get("required_companion_archetypes", []))
```

Falls in `catalog/models.py` eine `PROVENANCE_LLM_ENRICHED`-Liste existiert (ca. L914),
dort ergänzen:
```python
"network_aliases",                    # NEU Phase 0
"required_companion_archetypes",      # NEU Phase 0
```

### 0.2 — `ArchetypeContract` synchron halten

```python
@dataclass
class ArchetypeContract:
    # --- bestehende Felder ---
    network_aliases: list[str] = field(default_factory=list)
    required_companion_archetypes: list[str] = field(default_factory=list)
```

Funktion `entry_to_archetype_contract` (falls vorhanden) aktualisieren:
```python
def entry_to_archetype_contract(entry: ImageCatalogEntry) -> ArchetypeContract:
    return ArchetypeContract(
        # --- bestehende Zuweisungen ---
        network_aliases=list(entry.network_aliases or []),
        required_companion_archetypes=list(entry.required_companion_archetypes or []),
    )
```

---

## Smoke-Test nach dem Call

```python
from honeynet_framework.catalog.models import (
    ImageCatalogEntry, CATALOG_SCHEMA_VERSION, ArchetypeContract
)

# Konstante vorhanden
assert CATALOG_SCHEMA_VERSION == "2"

# Neue Felder auf DataClass
e = ImageCatalogEntry(archetype="postgres")
assert e.schema_version == ""
assert e.network_aliases == []
assert e.required_companion_archetypes == []

# Roundtrip
e.schema_version = "2"
e.network_aliases = ["postgres", "db"]
e.required_companion_archetypes = ["notary-signer"]
d = e.to_dict()
assert d["schema_version"] == "2"
assert d["network_aliases"] == ["postgres", "db"]
e2 = ImageCatalogEntry.from_dict(d)
assert e2.schema_version == "2"
assert e2.network_aliases == ["postgres", "db"]
assert e2.required_companion_archetypes == ["notary-signer"]
```

---

## Was NICHT geändert wird

- `builder.py` (Cache-Invalidierung → Call 03)
- `enricher.py` (LLM-Prompt → Call 04)
- Keine anderen Dateien
