# Call-Pläne: Honeynet Framework — Übersicht

Stand: REV43. 26 einzelne Call-Pläne, je eine Datei pro LLM-Call.

Grundregel: **Pro Call eine Datei lesen + editieren + Smoke-Test grün → nächster Call.**

---

## Abhängigkeitsgraph

```
Call 01 (Phase 2.1)  ──────────────────────────────────────────┐
Call 02 (Phase 0.1–0.2)  ──────┐                               │
Call 03 (Phase 0.3)  ◄── 02    │                               │
Call 04 (Phase 0.4)  ◄── 02    │                               │
                               │                               │
Call 05 (Phase 2.2–2.3)  ◄──── 01                              │
Call 06 (Phase 2.4)  ◄── 01, 05                                │
Call 07 (Phase 1.1–1.2)  ◄── 01, 02                            │
                                                               │
Call 08 (Phase 4.1)  ──────────────────────┐                   │
Call 09 (Phase 4.2)  ◄── 08, 01            │                   │
Call 10 (Phase 4.3)  ◄── 08                │                   │
                                           │                   │
Call 11 (Phase 6b.1–6b.4)  ◄── 08         │                   │
Call 12 (Phase 6b.5)  ◄── 02              │                   │
                                           │                   │
Call 13 (Phase 6.1)  ◄── 11               │                   │
Call 14 (Phase 6.2)  ◄── 13               │                   │
Call 15 (Phase 6.3–6.4)  ◄── 13, 14       │                   │
                                           │                   │
Call 16 (Phase 7)  ◄── 12, 13 ────────────┘                   │
Call 17 (Phase 8)  ◄── 09 (Verifikation)                      │
Call 18 (Phase 9)  ◄── 12, 16                                  │
Call 19 (Phase 10)  ◄── 13, 16                                 │
Call 20 (Phase 11)  ◄── 12, 09                                 │
                                                               │
Call 21 (Phase 12.1–12.6 Validator)  ◄── 02, 12, 01 ──────────┘
Call 22 (Phase 12.5–12.5b Compiler)  ◄── 21, 09
                                           │
Call 23 (Phase 13)  ◄── 15, 06            │
Call 24 (Phase 14)  ◄── 21, 22 ◄──────────┘
Call 25 (Phase 15)  ◄── 12, 22
Call 26 (Phase 16)  ◄── 22, 25
```

---

## Phasen-Tabelle

| Call | Phase | Dateien | Schwierigkeit | Risiko |
|---|---|---|---|---|
| 01 | 2.1 | `catalog_inference.py` | ★☆☆ | gering |
| 02 | 0.1–0.2 | `catalog/models.py` | ★★☆ | mittel |
| 03 | 0.3 | `builder.py` | ★☆☆ | gering |
| 04 | 0.4 | `enricher.py` | ★★☆ | mittel |
| 05 | 2.2–2.3 | `catalog_inference.py`, `world_model_extractor.py` | ★★☆ | mittel |
| 06 | 2.4 | `world_model_repair.py`, `semantic_repair.py` | ★★☆ | mittel |
| 07 | 1.1–1.2 | `world_model_validator.py` | ★★☆ | mittel |
| 08 | 4.1 | `models.py` | ★☆☆ | gering |
| 09 | 4.2 | `deploy_compiler.py` | ★★★ | hoch |
| 10 | 4.3 | `tofu_renderer.py` | ★★☆ | mittel |
| 11 | 6b.1–6b.4 | `models.py` | ★☆☆ | gering |
| 12 | 6b.5 | `catalog/models.py` | ★☆☆ | gering |
| 13 | 6.1 | `models.py` | ★★☆ | mittel |
| 14 | 6.2 | `deployer.py` | ★★★ | hoch |
| 15 | 6.3–6.4 | `orchestrator.py`, `cli.py` | ★★★ | hoch |
| 16 | 7 | `deploy_compiler.py`, `catalog/models.py`, `orchestrator.py` | ★★★ | hoch |
| 17 | 8 | `deploy_compiler.py` | ★★☆ | gering |
| 18 | 9.1–9.3 | `deploy_compiler.py`, `catalog/models.py` | ★★☆ | mittel |
| 19 | 10 | `deployer.py`, `orchestrator.py` | ★★★ | hoch |
| 20 | 11 | `deploy_compiler.py`, `catalog/models.py` | ★★☆ | mittel |
| 21 | 12.1–12.6 (Validator) | `models.py`, `world_model_validator.py` | ★★☆ | mittel |
| 22 | 12.5–12.5b (Compiler) | `deploy_compiler.py` | ★★★ | hoch |
| 23 | 13.0–13.3 | `orchestrator.py`, `world_model_repair.py` | ★★★ | hoch |
| 24 | 14.1–14.2 | `world_model_validator.py`, `deploy_compiler.py` | ★★☆ | mittel |
| 25 | 15.1–15.2 | `deploy_compiler.py`, `catalog/models.py` | ★★☆ | mittel |
| 26 | 16.1–16.3 | `deploy_compiler.py`, `tofu_renderer.py` | ★★★ | hoch |

---

## Kontext-Regel pro Call

Jede Call-Datei enthält:
1. **Ziel** — was implementiert wird
2. **Dateien zu editieren** — mit vollem Inhalt mitgeben
3. **Kontext (Signaturen)** — abhängige Dateien, nur `def`-Zeilen + Docstrings
4. **Änderungen** — exakter Code aus `UMSETZUNGSPLAN.md`
5. **Smoke-Test** — minimaler Verifikationstest nach dem Call

---

## Risiko-Ampel

| Farbe | Kriterium | Vorgehen |
|---|---|---|
| 🟢 grün | Smoke-Test grün | Nächster Call |
| 🟡 gelb | Test schlägt fehl, Fehler lokalisierbar | Korrektur-Call auf gleiche Phase |
| 🔴 rot | Fehler in Call X bricht Call Y+Z | Stopp — zuerst Call X reparieren |

---

## Kritische Integrationstests (nach den jeweiligen Calls)

**Nach Call 16 (Phase 7):**
```bash
docker inspect hn-log-aggregator --format '{{json .Mounts}}'
# Zwei Mounts: session_log_root → .../logs:ro
#              session_log_root + "_out" → .../output:rw (Geschwister!)
```

**Nach Call 19 (Phase 10):**
```bash
tar -tzf <bundle>.tar.gz | grep -E "^(logs|output)/"
# Muss beide Bäume enthalten: logs/ und output/honeynet.ndjson
```

**Nach Call 24 (Phase 14):**
```python
# Tippfehler: vulnerability_profile="defualt_creds"
# → genau VULN_PROFILE_UNKNOWN, kein POLICY_ZONE_ISOLATION
```
