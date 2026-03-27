# Call 26 — Phase 16: Externer Traffic-Simulator

**Schwierigkeit:** ★★★ hoch  
**Abhängigkeiten:** Call 22 (Phase 12.5 — finale `ports`-Liste), Call 25 (Phase 15 — Imports)  
**Risiko:** hoch — G5-Fix (network_mode Guard im Renderer), G14-Fix (Port aus finaler Liste)

---

## Ziel

Für Container mit `external_traffic_profile` und `expose_ports_externally=True`
einen externen Traffic-Generator-Container in die IR einbauen. Dieser nutzt
`network_mode="host"` und bindet sich gegen den externen Port auf `127.0.0.1`.

`tofu_renderer.py`: Guard gegen `networks_advanced + network_mode` kombiniert.

---

## Dateien

### Zu editieren
- `honeynet_framework/deploy_compiler.py` — externer Generator
- `honeynet_framework/tofu_renderer.py` — `network_mode`-Guard

### Als Kontext mitgeben
- `deploy_compiler.py` vollständig
- `tofu_renderer.py` vollständig
- `catalog/models.py` — Signatur `ExternalTrafficProfile`
- `models.py` — `DeployContainer.network_mode`

---

## Änderungen

### 16.1 — `catalog/models.py`: `ExternalTrafficProfile`

Falls noch nicht in Call 12 definiert:
```python
@dataclass
class ExternalTrafficProfile:
    type: str
    generator_image: str = ""
    interval_seconds: int = 60
    commands: list[str] = field(default_factory=list)
    generator_user: Optional[str] = None
    generator_password: Optional[str] = None
    generator_env_overrides: dict[str, str] = field(default_factory=dict)
    # source_ips bewusst nicht modelliert — Docker bridge ersetzt Source-IP
```

### 16.2 — `tofu_renderer.py`: `network_mode`-Guard

**[G5-FIX]** Bestehende `networks_advanced`-Ausgabe bedingen:
```python
# [G5-FIX] Potenzielle Provider-Kollision: der aktuelle Renderer emittiert
# networks_advanced immer, auch wenn network_mode gesetzt ist.
# Docker-Config mit beidem ist ungültig.
if container.network_mode:
    container_block["network_mode"] = container.network_mode
    # networks_advanced NICHT setzen — kombiniert mit network_mode ungültig
else:
    if networks_advanced:
        container_block["networks_advanced"] = networks_advanced
```

### 16.3 — `deploy_compiler.py`: Externer Generator

Im Container-Loop von `_phase4_generate_ir()`, **nach** dem Port-Loop
und dem Traffic-Sidecar (Phase 11):

```python
from dataclasses import replace

ext_tp = getattr(entry, "external_traffic_profile", None) if entry else None
vuln_exposes = active_profile and active_profile.expose_ports_externally

if ext_tp and vuln_exposes:
    if not ext_tp.generator_image:
        logger.warning(
            "Container '%s' hat external_traffic_profile ohne generator_image — übersprungen",
            c.get("name"),
        )
    else:
        # [REV31-G14] Externen Port aus finaler ports-Liste lesen.
        # c["ports"] enthält dicts nach Phase 12.5 — kein ip-Attribut.
        # c["ports"] enthält den angeforderten, nicht den zugewiesenen Port.
        # Korrekte Quelle: ports (IR, Port-Objekte, nach _allocate_unique_external_port)
        external_port = next(
            (p.external for p in ports if p.ip == "0.0.0.0"),
            None,
        )
        if external_port:
            if ext_tp.type == "postgres" and ext_tp.generator_password is not None:
                ext_tp = replace(ext_tp, generator_env_overrides={
                    **ext_tp.generator_env_overrides,
                    "PGPASSWORD": ext_tp.generator_password,
                })
            containers.append(DeployContainer(
                name=f"{c.get('name', 'svc')}-ext-traffic",
                image=ext_tp.generator_image,
                networks=[],
                network_mode="host",
                command=_build_traffic_generator_command(
                    "127.0.0.1", ext_tp, port=external_port
                ),
                env=[f"{k}={v}" for k, v in (ext_tp.generator_env_overrides or {}).items()],
                archetype="",
                depends_on=[c.get("stable_name", c["name"])],
                upload_files=[],
            ))
```

---

## Smoke-Test nach dem Call

```python
from honeynet_framework.tofu_renderer import TofuRenderer
from honeynet_framework.models import DeployContainer

# network_mode Guard: kein networks_advanced wenn network_mode="host"
c_host = DeployContainer(name="ext", image="img", network_mode="host", networks=["net"])
# renderer.render(...) → container_block hat KEIN "networks_advanced"
# container_block hat "network_mode" = "host"

# Kein network_mode → networks_advanced wie bisher
c_normal = DeployContainer(name="pg", image="postgres", networks=["hn_net"])
# renderer.render(...) → container_block hat "networks_advanced"
# container_block hat KEIN "network_mode"
```

```python
# Externer Port aus finaler Liste — REV31-G14
# Szenario: Port 5432 angefragt, _allocate_unique_external_port weist 5433 zu
# → external_port = 5433 (aus ports-Liste)
# → external_traffic Generator-Command bindet gegen 127.0.0.1:5433
```

---

## Abschluss

Mit Call 26 sind alle 26 Phasen implementiert. Danach:
1. Kritische Integrationstests aus `AUSFÜHRUNGSPLAN.md` ausführen
2. `docker inspect hn-log-aggregator` — Mounts prüfen
3. `tar -tzf <bundle>.tar.gz` — `output/honeynet.ndjson` im Archiv
4. Tippfehler-Szenario: `vulnerability_profile="defualt_creds"` → genau ein `VULN_PROFILE_UNKNOWN`, kein `POLICY_ZONE_ISOLATION`
