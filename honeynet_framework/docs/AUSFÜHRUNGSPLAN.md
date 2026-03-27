# Ausführungsplan: Honeynet Framework — Call-by-Call-Strategie

Stand: REV43. Begleitdokument zu `UMSETZUNGSPLAN.md`.

---

## Warum ein einziger LLM-Call nicht funktioniert

### Kontextfenster-Grenze

Die direkt betroffenen Dateien liegen zusammen bei ~10.000+ Zeilen:

| Datei | ca. Zeilen | Kritische Phasen |
|---|---|---|
| `deploy_compiler.py` | ~1.100 | 2.2, 4.2, 9.3, 12.5, 12.5b, 14.2, 15.1, 16.3 |
| `models.py` | ~900 | 1, 4.1, 6.1, 6b.1–6b.4 |
| `orchestrator.py` | ~950 | 6.3, 13.0 |
| `catalog/models.py` | ~1.000 | 0.1, 6b.5, G11 |
| `world_model_validator.py` | ~900 | 1.1, 1.2, 2.5 |
| `catalog_inference.py` | ~850 | 2.1, 2.2 |
| `tofu_renderer.py` | ~700 | 4.3, 16.2 |
| `world_model_repair.py` | ~500 | 2.4, 13.1 |
| `semantic_repair.py` | ~400 | 2.4 |
| `deployer.py` | ~600 | 6.2 |
| `enricher.py` | ~700 | 0.4 |
| `builder.py` | ~500 | 0.3 |

Ein Modell müsste alle diese Dateien gleichzeitig vollständig im Kontext halten,
korrekt lesen **und** konsistent in mehrere Dateien zurückschreiben. Das übersteigt
zuverlässig das, was ein einzelner Call fehlerfrei leisten kann.

### Abhängigkeitskette

Die Phasen bauen aufeinander auf — ein Call müsste alle Zwischenzustände korrekt
im Kopf halten:

```
Phase 0  (Katalog-Schema)
  └─▶ Phase 2.1  (PEP-503-Normalisierer)
        └─▶ Phase 2.2–2.5  (Normalisierung in allen Repair-Pfaden)
              └─▶ Phase 1  (Companion-Validator nutzt Normalisierer)
                    └─▶ Phase 4  (IR-Generierung nutzt Companion-Kontrakte)
                          └─▶ Phase 6b  (Serialisierung neuer Modellfelder)
                                └─▶ Phase 7–10  (Log, Tokens, Beweise)
                                      └─▶ Phase 11–16  (Traffic, Schwachstellen)
```

Ein Fehler in Phase 2.1 (z.B. falscher Rückgabetyp in `normalize_expanded_archetype`)
bricht Phase 1, Phase 2.2–2.5, Phase 4 und Phase 13 stillschweigend — alle
importieren aus `catalog_inference.py`.

### Verifikationslücke

Ohne Zwischen-Tests nach jeder Phase ist ein Tippfehler in einer frühen Datei
nicht sofort sichtbar. Er zeigt sich erst, wenn eine spätere Phase fehlschlägt —
dann ist der Ursprung unklar, und es beginnt eine aufwendige Rückverfolgung.

Beispiel: Ein falsch indentierter `return`-Block in `_pep503_normalize` (Phase 2.1)
würde alle nachgelagerten Imports in Phase 4, 6, 13 stilllschweigend brechen —
die Funktion wird importiert, verhält sich aber anders als erwartet.

---

## Call-by-Call-Strategie

### Grundregel

**Pro Phase ein Call.** Der Call bekommt als Kontext:
1. Den relevanten Abschnitt aus `UMSETZUNGSPLAN.md`
2. Den vollständigen Inhalt der zu editierenden Datei(en)
3. Den Inhalt der Abhängigkeiten (nur Signaturen + Docstrings, nicht den vollen Body)

Nach jedem Call: minimaler Smoke-Test (Import + einfacher Funktionsaufruf) bevor
der nächste Call beginnt.

---

## Phasen-Tabelle

| Reihenfolge | Phase | Dateien | Schwierigkeit | Abhängig von | Verifikation |
|---|---|---|---|---|---|
| 1 | **2.1** | `catalog_inference.py` | ★☆☆ einfach | — | Unit-Test 3 Funktionen |
| 2 | **0.1–0.2** | `catalog/models.py` | ★★☆ mittel | — | Import + Roundtrip |
| 3 | **0.3** | `builder.py` | ★☆☆ einfach | 0.1 | Cache-Test |
| 4 | **0.4** | `enricher.py` | ★★☆ mittel | 0.1 | Sanitize-Test |
| 5 | **2.2–2.3** | `catalog_inference.py`, `world_model_extractor.py` | ★★☆ mittel | 2.1 | Archetype-Roundtrip |
| 6 | **2.4** | `world_model_repair.py`, `semantic_repair.py` | ★★☆ mittel | 2.1, 2.2 | Repair-Aufruf mit Index |
| 7 | **1.1–1.2** | `world_model_validator.py` | ★★☆ mittel | 2.1, 0.1 | Companion-Validation-Test |
| 8 | **4.1** | `models.py` | ★☆☆ einfach | — | Import-Test, Feld `network_mode` vorhanden |
| 9 | **4.2** | `deploy_compiler.py` | ★★★ hoch | 4.1, 2.1 | Backbone im IR-Output |
| 10 | **4.3** | `tofu_renderer.py` | ★★☆ mittel | 4.1 | Alias + Hostname im HCL |
| 11 | **6b.1–6b.4** | `models.py` | ★☆☆ einfach | 4.1 | `to_dict()`-Roundtrip |
| 12 | **6b.5** | `catalog/models.py` | ★☆☆ einfach | 0.1 | Katalog-Roundtrip aller neuen Felder |
| 13 | **6.1** | `models.py` | ★★☆ mittel | 6b | Import + Enum-Werte |
| 14 | **6.2** | `deployer.py` | ★★★ hoch | 6.1 | `RuntimeVerifier.verify()` Mock-Test |
| 15 | **6.3–6.4** | `orchestrator.py`, `cli.py` | ★★★ hoch | 6.1, 6.2 | End-to-End-Status-Test |
| 16 | **7** | `deploy_compiler.py`, `catalog/models.py`, `orchestrator.py` | ★★★ hoch | 6b.5 | Log-Paths im IR; `_log_safe_name`-Konsistenz (`_attach_log_mounts` ↔ `_render_filebeat_config`); Host-Pfad-Trennung (`session_log_root` vs. `+ "_out"`) |
| 17 | **8** | `deploy_compiler.py` | ★★☆ mittel | 7 | Log-Aggregator-Container im IR |
| 18 | **9.1–9.3** | `deploy_compiler.py`, `catalog/models.py` | ★★☆ mittel | 6b.5 | Upload-Files + `seen_paths`; env-Honeytoken-Deduplizierung |
| 19 | **10** | `deployer.py`, `orchestrator.py` | ★★★ hoch | 7, 9 | Integrationstest: `export_evidence` → `EvidenceSigner(output_base)` → `output/honeynet.ndjson` im Archiv; `output_base` nicht existent → kein Fehler |
| 20 | **11** | `deploy_compiler.py`, `catalog/models.py` | ★★☆ mittel | 6b.5 | Traffic-Sidecar im IR |
| 21 | **12.1–12.6** | `catalog/models.py`, `world_model_validator.py` | ★★☆ mittel | 0.1, 2.1 | `VulnerabilityProfile`-Validation; `VULN_PROFILE_UNKNOWN` bei Tippfehler; keine doppelte Warnung bei unbekanntem Archetyp |
| 22 | **12.5–12.5b** | `deploy_compiler.py` | ★★★ hoch | 12.1, G9-Fix | Port mit `ip="0.0.0.0"` im IR; `active_profile` vs. `vuln_profile_name` korrekt getrennt |
| 23 | **13.0–13.3** | `orchestrator.py`, `world_model_repair.py` | ★★★ hoch | 6.3 | Repair-Loop-Abbruch-Test; Stage-2-Fortschritts-Heuristik feuert bei `attempt > MAX_STAGE_1` |
| 24 | **14.1–14.2** | `world_model_validator.py`, `deploy_compiler.py` | ★★☆ mittel | 12.1–12.6, 12.5 | Zone-Check nur bei aufgelöstem Profil; Port-Warning auf finaler `ports`-Liste (nicht `c["ports"]`) |
| 25 | **15.1–15.2** | `deploy_compiler.py`, `catalog/models.py` | ★★☆ mittel | 6b.5, 12.5 | HealthContract im IR |
| 26 | **16.1–16.3** | `deploy_compiler.py`, `tofu_renderer.py` | ★★★ hoch | 12.5, 15.2 | Externer Generator + HCL |

---

## Empfohlene Einstiegssequenz (Wochen 1–2)

### Call 1 — Phase 2.1: PEP-503-Normalisierer

**Warum zuerst:** Drei klar abgegrenzte Funktionen in einer Datei, null
Abhängigkeiten nach außen, sofort testbar. Dieser Call hat die kleinste
Risikofläche und legt das Fundament für alles danach.

**Datei:** `catalog_inference.py`

**Neue Funktionen:**
```python
def _pep503_normalize(value: str) -> str: ...
def build_archetype_lookup_index(catalog: "CatalogSnapshot") -> dict[str, str]: ...
def normalize_expanded_archetype(
    archetype: str,
    catalog: "CatalogSnapshot",
    archetype_index: Optional[dict] = None,
) -> Optional[str]: ...
```

**Smoke-Test nach dem Call:**
```python
from catalog_inference import _pep503_normalize, build_archetype_lookup_index, normalize_expanded_archetype
assert _pep503_normalize("Apache.Kafka") == "apache-kafka"
assert _pep503_normalize("postgresql") == "postgresql"
```

---

### Call 2 — Phase 6b: Mechanische `to_dict()`-Ergänzungen

**Warum zweiter:** Reines Schreiben ohne Logik — ideal als zweiter isolierter Call.
Keine Abhängigkeit auf Phase 2.1. Behebt G10 und G11.

**Dateien:** `models.py`, `catalog/models.py`

**Umfang:** `Port.to_dict()`, `DeployContainer.to_dict()` (G10), `DeployProjection.to_dict()`,
`ImageCatalogEntry.to_dict()` + `from_dict()` für alle Phase-7–16-Felder (G11).

**Smoke-Test nach dem Call:**
```python
p = Port(internal=5432, external=5433, protocol="tcp", ip="0.0.0.0")
assert p.to_dict() == {"internal": 5432, "external": 5433, "protocol": "tcp", "ip": "0.0.0.0"}
```

---

### Call 3 — Phase 0: Katalogerweiterung

**Dateien:** `catalog/models.py`, `builder.py`, `enricher.py`

Erst nach Phase 6b, damit die neuen Schema-Felder (`network_aliases`,
`required_companion_archetypes`, `schema_version`) direkt mit korrekter
Serialisierung eingeführt werden.

---

### Call 4 — Phase 4.1 + 4.3: Netzwerk-Modell (Einstieg)

**Dateien:** `models.py` (nur neue Felder: `networks`, `network_aliases`,
`hostname`, `network_mode`), `tofu_renderer.py`

**Hinweis zu `network_mode`:** Muss hier eingeführt werden (REV31-G15), damit
Phase 4.3 (Renderer-Guard) sofort funktioniert. Phase 16.2 nutzt das Feld,
führt es nicht erneut ein.

---

## Kontextpakete pro Call

Um den Kontext eines Calls minimal und präzise zu halten:

### Was immer mitgegeben wird
- Relevanter Abschnitt aus `UMSETZUNGSPLAN.md` (nur die betreffende Phase)
- Vollständiger Inhalt der zu editierenden Datei

### Was nur als Signaturen mitgegeben wird
- Abhängige Dateien (nur `def`-Zeilen + Docstrings, kein Body)
- Aktuelle Fehlermeldung / Testausgabe falls vorhanden

### Was weggelassen wird
- Alle Dateien die nicht direkt bearbeitet oder importiert werden
- Der gesamte Rest von `UMSETZUNGSPLAN.md`

---

## Risiko-Ampel

| Farbe | Kriterium | Vorgehen |
|---|---|---|
| 🟢 grün | Call produziert Code, Smoke-Test grün | Nächster Call |
| 🟡 gelb | Smoke-Test schlägt fehl, Fehler lokalisierbar | Korrektur-Call auf gleiche Phase |
| 🔴 rot | Fehler in Phase X bricht Phase Y+Z | Stopp — zuerst Phase X reparieren, dann fortsetzen |

**Faustregel:** Nie zwei Phasen in einem Call kombinieren wenn eine von der
anderen abhängt. Nur parallelisierbar wenn beide keine gemeinsamen Abhängigkeiten
haben (z.B. Phase 0.3 und Phase 2.1 können theoretisch gleichzeitig laufen).

---

## Einstieg in die Implementierung

Der Plan ist vollständig (REV43). Implementierung beginnt mit Call 1.

**Call 1 — Phase 2.1** — `catalog_inference.py`, drei neue Funktionen, isoliert,
sofort testbar. Kontext für diesen Call:

1. `UMSETZUNGSPLAN.md` §Phase 2.1
2. Vollständiger Inhalt `catalog_inference.py` (~850 Zeilen)
3. Signatur von `CatalogSnapshot` aus `catalog/models.py` (nur Klassen-Header + Felder)

**Kritische Integrationstests** (nicht durch Unit-Tests abgedeckt — erfordern
dedizierte Verifikations-Calls nach den jeweiligen Phasen):

- Nach Phase 7: `docker inspect hn-log-aggregator` — `/usr/share/filebeat/logs/` darf keinen `filebeat_out`- oder `output`-Unterordner enthalten
- Nach Phase 10: `tar -tzf <bundle>.tar.gz` — muss sowohl `logs/`- als auch `output/`-Einträge enthalten
- Nach Phase 12.6/14.1: Tippfehler-Szenario (`vulnerability_profile="defualt_creds"`) → genau ein `VULN_PROFILE_UNKNOWN`-Error, kein `POLICY_ZONE_ISOLATION`
