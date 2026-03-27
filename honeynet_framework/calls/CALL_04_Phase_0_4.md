# Call 04 — Phase 0.4: enricher.py — LLM-Prompt + Sanitisierung

**Schwierigkeit:** ★★☆ mittel  
**Abhängigkeiten:** Call 02 (Phase 0.1 — `ImageCatalogEntry` mit neuen Feldern)  
**Risiko:** mittel — LLM-Prompt-Erweiterung + neue Sanitisierungslogik

---

## Ziel

`enricher.py` so erweitern, dass das LLM die neuen Felder `network_aliases`
und `required_companion_archetypes` befüllt. Sanitisierungshelfer für
DNS-Labels implementieren. Companion-Deduplizierung bereits beim Enrichment
vornehmen.

---

## Dateien

### Zu editieren
- `honeynet_framework/enricher.py` (~700 Zeilen)

### Als Kontext mitgeben
- Gesamte `enricher.py` (direkt editiert)
- `honeynet_framework/catalog/models.py` — Signatur `ImageCatalogEntry` (neue Felder)

---

## Änderungen

### TIER_A_SCHEMA / TIER_B_SCHEMA erweitern

In der Schema-Strings-Konstante (YAML-Format) ergänzen:
```python
TIER_A_SCHEMA = """...
network_aliases: []
required_companion_archetypes: []"""
```

### LLM-Prompt erweitern

Im Prompt-Template die zwei neuen Felder erklären:
```
- network_aliases: DNS names this service provides on the shared network.
  Examples: mongo=["mongo"], postgres=["postgres","db"].
  Leave empty if no conventional short alias exists.

- required_companion_archetypes: List of archetypes this service CANNOT
  start without. Use a list — some services need multiple companions.
  Examples: rocketchat=["mongo"], notary=["notary-signer","mysql"].
  Leave empty if the service starts independently.
```

### Sanitisierungshelfer hinzufügen (nach Imports, vor Klassendefinition)

```python
import re as _re

def _sanitize_dns_labels(raw: list) -> list[str]:
    """RFC 1123: a-z, 0-9, Bindestrich; 1–63 Zeichen."""
    result = []
    for item in raw:
        label = _re.sub(r"[^a-z0-9-]", "",
                        str(item or "").strip().lower().replace("_", "-")
                        ).strip("-")
        if 1 <= len(label) <= 63:
            result.append(label)
    return result
```

### `_merge_entry()` erweitern — vor `return entry`

```python
entry.network_aliases = _sanitize_dns_labels(
    payload.get("network_aliases", []) or []
)

# Companions: lowercase, ohne Duplikate, preserving order.
# Kanonisierung (mongodb→mongo etc.) passiert beim Validator, weil dort
# ein CatalogSnapshot vorliegt; beim Enrichment gibt es noch keinen.
# [P5] Deduplizierung schon hier — verhindert doppelte Einträge wenn das
# LLM z.B. ["postgres", "postgresql"] zurückgibt.
seen: set[str] = set()
companions: list[str] = []
for c in (payload.get("required_companion_archetypes", []) or []):
    normalized = str(c).strip().lower()
    if normalized and normalized not in seen:
        seen.add(normalized)
        companions.append(normalized)
entry.required_companion_archetypes = companions
```

---

## Smoke-Test nach dem Call

```python
from honeynet_framework.enricher import _sanitize_dns_labels

# Gültige Labels
assert _sanitize_dns_labels(["postgres", "db"]) == ["postgres", "db"]
assert _sanitize_dns_labels(["my_service"]) == ["my-service"]
assert _sanitize_dns_labels(["UPPER"]) == ["upper"]
assert _sanitize_dns_labels([""]) == []

# Zu lang → verworfen
assert _sanitize_dns_labels(["a" * 64]) == []

# Ungültige Zeichen entfernt
assert _sanitize_dns_labels(["foo.bar"]) == ["foobar"]
```
