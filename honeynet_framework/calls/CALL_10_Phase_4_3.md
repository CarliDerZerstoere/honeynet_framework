# Call 10 — Phase 4.3: tofu_renderer.py — Aliases, Hostname, network_mode Guard

**Schwierigkeit:** ★★☆ mittel  
**Abhängigkeiten:** Call 08 (Phase 4.1 — `DeployContainer.network_aliases`, `hostname`, `network_mode`)  
**Risiko:** mittel — Renderer-Logik erweitert, Guard gegen ungültige Docker-Config

---

## Ziel

`tofu_renderer.py` erweitern, um `network_aliases`, `hostname` und
`healthcheck`/`wait` im HCL-Output zu rendern. Kritischer Guard:
`networks_advanced` **nicht** setzen wenn `network_mode` gesetzt ist
(kombiniert wäre ungültige Docker-Config).

---

## Dateien

### Zu editieren
- `honeynet_framework/tofu_renderer.py` (~700 Zeilen)

### Als Kontext mitgeben
- `tofu_renderer.py` vollständig
- `models.py` — `DeployContainer`-Felder (aus Call 08): `network_aliases`, `hostname`, `network_mode`, `healthcheck`, `wait`, `wait_timeout`

---

## Änderungen

### Aliases + Hostname im Container-Block

Bestehende `networks_advanced`-Logik erweitern:

```python
networks_advanced = []
for network_name in container.networks:
    if network_name not in network_resource_names:
        continue
    net_entry: dict = {
        "name": f"${{docker_network.{network_resource_names[network_name]}.name}}"
    }
    aliases = (container.network_aliases or {}).get(network_name, [])
    if aliases:
        net_entry["aliases"] = aliases
    networks_advanced.append(net_entry)

# [G5] Potenzielle Provider-Kollision: networks_advanced + network_mode kombiniert
# ergibt ungültige Docker-Config. Guard verhindert das.
if not container.network_mode:
    container_block["networks_advanced"] = networks_advanced
if container.hostname:
    container_block["hostname"] = container.hostname
```

### Healthcheck + wait rendern

```python
if container.healthcheck:
    hc = container.healthcheck
    container_block["healthcheck"] = [{
        "test":         hc.get("test", ["NONE"]),
        "interval":     hc.get("interval", "30s"),
        "timeout":      hc.get("timeout", "10s"),
        "retries":      hc.get("retries", 3),
        "start_period": hc.get("start_period", "0s"),
    }]
if container.wait:
    container_block["wait"]         = True
    container_block["wait_timeout"] = container.wait_timeout
```

### Plattformunabhängige Bind-Mount-Erkennung (Phase 7.2 — hier einbauen)

```python
import os

def _render_volume_spec(self, volume_spec: str, volume_resource_names: dict) -> Optional[dict]:
    parts = volume_spec.split(":")
    read_only = parts[-1].strip().lower() == "ro" if len(parts) >= 3 else False
    if read_only:
        parts = parts[:-1]
    if len(parts) < 2:
        return None

    # Windows drive-letter-aware Pfad-Erkennung
    if len(parts) >= 3 and len(parts[0]) == 1 and parts[0].isalpha():
        source = parts[0] + ":" + parts[1]
        container_path = ":".join(parts[2:])
    else:
        source = parts[0]
        container_path = ":".join(parts[1:])

    source = source.strip()
    container_path = container_path.strip()
    if not source or not container_path:
        return None

    block: dict = {"container_path": container_path}
    if read_only:
        block["read_only"] = True

    if os.path.isabs(source):
        block["host_path"] = source
    elif source in volume_resource_names:
        block["volume_name"] = f"${{docker_volume.{volume_resource_names[source]}.name}}"
    else:
        block["volume_name"] = source
    return block
```

---

## Smoke-Test nach dem Call

```python
from honeynet_framework.tofu_renderer import TofuRenderer
from honeynet_framework.models import DeployContainer

# network_mode guard: kein networks_advanced wenn network_mode gesetzt
c = DeployContainer(name="gen", image="img", network_mode="host", networks=["net1"])
# renderer.render(...) → container_block darf kein "networks_advanced" haben

# Alias im HCL-Output vorhanden
c2 = DeployContainer(
    name="pg", image="postgres:15",
    networks=["hn_default"],
    network_aliases={"hn_default": ["postgres", "db"]},
)
# HCL-Block enthält aliases = ["postgres", "db"]
```

---

## Was NICHT geändert wird

- `upload`-Block in `tofu_renderer.py` → Call 16 (Phase 7.7)
- `network_mode`-Setzung im Compiler → Call 26 (Phase 16.2/16.3)
