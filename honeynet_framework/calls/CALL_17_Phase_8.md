# Call 17 — Phase 8: Honeywall — Netzwerk-Isolation

**Schwierigkeit:** ★★☆ mittel  
**Abhängigkeiten:** Call 09 (Phase 4.2 — Backbone bereits mit `internal=True` angelegt)  
**Risiko:** gering — Phase 8 ist durch Phase 4.2 bereits vollständig implementiert

---

## Ziel

Formale Verifikation dass das Backbone-Netzwerk korrekt mit `internal=True`
angelegt wird. Phase 8 fügt keine neuen Features hinzu — das Backbone ist
die vollständige Honeywall-Implementierung.

---

## Dateien

### Zu prüfen (kein Edit nötig wenn Phase 4.2 korrekt implementiert)
- `honeynet_framework/deploy_compiler.py` — `_phase2_normalize()`: `"internal": True` im Backbone-Netzwerk-Dict

---

## Überprüfung

Sicherstellen dass in `_phase2_normalize()` (Call 09) das Backbone-Netzwerk
wie folgt definiert wird:
```python
normalized["networks"]["_backbone"] = {
    "name": backbone_name,
    "driver": "bridge",
    "internal": True,   # ← Honeywall: kein Internetzugang für Container
    "zone_name": "_backbone",
}
```

Falls `internal: True` fehlt → hier ergänzen.

---

## Hintergrund

In einem Honeynet müssen simulierte Services füreinander erreichbar sein
(sonst wirkt die Täuschung nicht glaubwürdig). Zone-Isolation ist für die
**Außengrenze** gedacht — nicht für die interne Kommunikation.

`internal=True` bedeutet:
- Container erreichen sich gegenseitig (DNS via Docker-Aliases funktioniert)
- Container haben **keinen** direkten Internetzugang
- Begrenzt Datenexfiltration und verhindert Missbrauch als Angriffs-Sprungbrett

---

## Smoke-Test

```python
from honeynet_framework.deploy_compiler import DeployCompiler

compiler = DeployCompiler(config)
result = compiler.compile(world_model, catalog)

backbone = next(
    (n for n in result.networks if n.name == result.backbone_network_name),
    None
)
assert backbone is not None
assert backbone.internal is True
```
