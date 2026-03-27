# Call 01 — Phase 2.1: PEP-503-Normalisierer

**Schwierigkeit:** ★☆☆ einfach  
**Abhängigkeiten:** keine  
**Risiko:** gering — isolierte neue Funktionen, kein bestehender Code verändert

---

## Ziel

Drei neue Funktionen am Ende von `catalog_inference.py` implementieren, die
Archetype-Namen normalisieren und einen Lookup-Index über den Katalog aufbauen.
Diese Funktionen existieren **noch nicht** im Code (rg-Suche ergibt 0 Treffer).
Sie sind das Fundament für Phase 2.2–2.5, Phase 1 und Phase 13.

---

## Dateien

### Zu editieren
- `honeynet_framework/catalog_inference.py` (~850 Zeilen)

### Als Kontext mitgeben (nur Signaturen + Docstrings)
- `honeynet_framework/catalog/models.py` — Klassen-Header von `CatalogSnapshot` und `ImageCatalogEntry`
- `honeynet_framework/catalog_inference.py` — bestehende Funktion `catalog_entry_identifier_values` (Signatur + Body, da direkt aufgerufen)
- `honeynet_framework/catalog_inference.py` — bestehende Funktion `_technology_lookup_variants` (Signatur, da im Fallback verwendet)

---

## Änderungen

Diese drei Funktionen **vor** `apply_catalog_defaults_to_system` einfügen:

```python
import re  # bereits importiert prüfen — ggf. ergänzen

def _pep503_normalize(value: str) -> str:
    """PEP 503: lowercase, collapse runs of [-_.] to single '-'."""
    return re.sub(r"[-_.]+", "-", str(value or "").lower()).strip("-")


def build_archetype_lookup_index(
    catalog: "CatalogSnapshot",
) -> dict[str, str]:
    """Baut einen {pep503_key: canonical_archetype}-Index.

    Ein leerer String als Wert bedeutet "mehrdeutig" — mehrere Einträge
    passen auf diesen Key. normalize_expanded_archetype gibt dann None zurück.
    """
    index: dict[str, str] = {}

    def _register(key: str, canonical: str) -> None:
        if not key:
            return
        if key in index and index[key] != canonical:
            index[key] = ""  # Kollision → mehrdeutig
        else:
            index[key] = canonical

    for entry in catalog.entries:
        canonical = entry.archetype
        for ident in catalog_entry_identifier_values(entry):
            _register(_pep503_normalize(ident), canonical)
            # Basename (nach letztem /) zusätzlich registrieren
            if "/" in ident:
                basename = ident.rsplit("/", 1)[-1]
                _register(_pep503_normalize(basename), canonical)

    return index


def normalize_expanded_archetype(
    archetype: str,
    catalog: "CatalogSnapshot",
    archetype_index: Optional[dict] = None,
) -> Optional[str]:
    """Löst einen rohen Archetype-String auf den kanonischen Katalog-Eintrag auf.

    Gibt None zurück wenn:
    - der Archetype unbekannt ist (kein Treffer im Katalog), oder
    - mehrdeutig ist (mehrere Einträge passen — Kollision im Index).

    Der Aufrufer muss None explizit behandeln (logger.warning + Downstream-Error
    vom Validator wenn strict_catalog=True).
    """
    if not archetype or not catalog:
        return None

    index = archetype_index if archetype_index is not None \
        else build_archetype_lookup_index(catalog)

    key = _pep503_normalize(archetype)
    canonical = index.get(key)
    if canonical:  # "" = mehrdeutig → None
        return canonical or None

    # Basename-Fallback
    if "/" in archetype:
        base_key = _pep503_normalize(archetype.rsplit("/", 1)[-1])
        canonical = index.get(base_key)
        if canonical:
            return canonical or None

    # Technology-Alias-Fallback (mongodb→mongo etc.)
    for alias in _technology_lookup_variants(archetype):
        alias_key = _pep503_normalize(alias)
        canonical = index.get(alias_key)
        if canonical:
            return canonical or None

    return None
```

**Wichtig:** `Optional` muss importiert sein (`from typing import Optional`).
Prüfen ob bereits importiert — nicht doppelt hinzufügen.

---

## Smoke-Test nach dem Call

```python
from honeynet_framework.catalog_inference import (
    _pep503_normalize,
    build_archetype_lookup_index,
    normalize_expanded_archetype,
)

# Normalisierung
assert _pep503_normalize("apache-kafka") == "apache-kafka"
assert _pep503_normalize("apache_kafka") == "apache-kafka"
assert _pep503_normalize("Apache.Kafka") == "apache-kafka"
assert _pep503_normalize("postgresql") == "postgresql"

# Mit echtem Katalog:
# assert build_archetype_lookup_index(catalog)["apache-kafka"] == "apache/kafka"
# assert normalize_expanded_archetype("kafka", catalog) == "apache/kafka"
# assert normalize_expanded_archetype("postgresql", catalog) == "postgres"
# assert normalize_expanded_archetype("db", catalog) is None  # mehrdeutig
# assert normalize_expanded_archetype("unknown_xyz", catalog) is None
```

---

## Was NICHT geändert wird

- `apply_catalog_defaults_to_system` wird in diesem Call nicht angefasst (→ Call 05)
- Keine Änderungen an anderen Dateien
