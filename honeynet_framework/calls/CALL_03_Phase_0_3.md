# Call 03 — Phase 0.3: builder.py — Cache-Invalidierung per Schema-Version

**Schwierigkeit:** ★☆☆ einfach  
**Abhängigkeiten:** Call 02 (Phase 0.1 — `CATALOG_SCHEMA_VERSION` muss existieren)  
**Risiko:** gering — gezielte Erweiterung der Cache-Prüflogik

---

## Ziel

`builder.py` so erweitern, dass ein Cache-Hit nur akzeptiert wird, wenn der
gecachte Entry die aktuelle Schema-Version trägt. Veraltete Snapshots ohne
die neuen Phase-0-Felder werden damit automatisch ungültig.

---

## Dateien

### Zu editieren
- `honeynet_framework/builder.py` (~500 Zeilen)

### Als Kontext mitgeben
- `honeynet_framework/catalog/models.py` — Signatur von `CATALOG_SCHEMA_VERSION` und `ImageCatalogEntry`
- Bestehender Cache-Hit-Pfad in `builder.py` (ca. L203 + L244)

---

## Änderungen

### Import ergänzen

```python
from .models import CATALOG_SCHEMA_VERSION, ImageCatalogEntry, ...
```
(nur ergänzen falls `CATALOG_SCHEMA_VERSION` noch nicht importiert)

### Hilfsfunktion hinzufügen

```python
def _schema_ok(entry: "ImageCatalogEntry") -> bool:
    """Prüft ob ein gecachter Entry der aktuellen Schema-Version entspricht."""
    return getattr(entry, "schema_version", "") == CATALOG_SCHEMA_VERSION
```

### Cache-Hit-Bedingung erweitern (offizieller Pfad, ca. L203)

**Vorher:**
```python
if not force_rebuild and cached_entry and docs_hash == cached_hash and cached_verified:
    entry = cached_entry
```

**Nachher:**
```python
if not force_rebuild and cached_entry and docs_hash == cached_hash \
        and cached_verified and _schema_ok(cached_entry):
    entry = cached_entry
```

### Schema-Version beim Rebuild setzen (im else-Zweig nach dem Enrichment)

```python
else:
    # bestehender Rebuild ...
    if enricher and docs_content:
        entry = await enricher.enrich(entry, docs_content)
    entry.schema_version = CATALOG_SCHEMA_VERSION  # ← NEU
```

### Extended-Pfad (ca. L244) analog behandeln

Dieselbe `_schema_ok`-Prüfung in die Cache-Bedingung des Extended-Pfads einbauen
und `entry.schema_version = CATALOG_SCHEMA_VERSION` nach dem Enrichment setzen.

---

## Smoke-Test nach dem Call

```python
from honeynet_framework.builder import _schema_ok
from honeynet_framework.catalog.models import ImageCatalogEntry, CATALOG_SCHEMA_VERSION

# Veralteter Entry wird als ungültig erkannt
old = ImageCatalogEntry(archetype="postgres")
assert _schema_ok(old) is False

# Aktueller Entry wird akzeptiert
new = ImageCatalogEntry(archetype="postgres", schema_version=CATALOG_SCHEMA_VERSION)
assert _schema_ok(new) is True
```

---

## Hinweis

Nach Abschluss dieses Calls sollte einmalig `force_rebuild=True` gesetzt werden,
damit alle bestehenden Cache-Einträge mit der neuen Schema-Version versehen werden
(Woche-1-Aufgabe laut Zeitplan).
