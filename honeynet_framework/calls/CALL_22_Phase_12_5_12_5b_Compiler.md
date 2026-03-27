# Call 22 — Phase 12.5 + 12.5b: deploy_compiler.py — Schwachstellen-Profile anwenden

**Schwierigkeit:** ★★★ hoch  
**Abhängigkeiten:** Call 21 (Phase 12.1 — `VulnerabilityProfile`, `Port.ip`), Call 09 (Phase 4.2 — IR + Port-Loop)  
**Risiko:** hoch — G9-Fix (Gate-Override), G12-Fix (dict statt Port-Objekte), zwei kritische Verweise auf finale `ports`-Liste

---

## Ziel

Schwachstellen-Profile in die IR übersetzen:
- Env-Overrides anwenden
- Ports auf `ip="0.0.0.0"` setzen wenn `expose_ports_externally=True`
- Port-Governance-Gate (`_should_publish_host_ports`) via `expose_externally`-Flag übersteuern

**[G9]:** `_should_publish_host_ports()` verwirft in internen Zonen alle Ports.
`expose_externally=True` muss diese Sperre explizit übersteuern.  
**[G12]:** Port-Override als dicts mit `"ip"`-Schlüssel setzen, **nicht** als `Port`-Objekte.

---

## Dateien

### Zu editieren
- `honeynet_framework/deploy_compiler.py`

### Als Kontext mitgeben
- `deploy_compiler.py` vollständig
- `catalog/models.py` — Signatur `VulnerabilityProfile`
- `models.py` — `Port`-Dataclass mit `ip`-Feld

---

## Änderungen

### 12.3b — `_phase1_extract()`: `vulnerability_profile` in Container-Dict

```python
container = {
    ...
    "vulnerability_profile": getattr(deploy, "vulnerability_profile", None),
}
```

### 12.5 — `_phase4_generate_ir()`: Profil auflösen + anwenden

Im Container-Loop, **vor** dem Port-Loop:
```python
vuln_profile_name = c.get("vulnerability_profile") or ""
active_profile = None
if entry and vuln_profile_name and getattr(entry, "vulnerability_profiles", None):
    active_profile = next(
        (p for p in entry.vulnerability_profiles if p.name == vuln_profile_name),
        None,
    )

# expose_externally-Flag früh setzen — wird vom Port-Loop gebraucht
expose_externally = bool(active_profile and active_profile.expose_ports_externally)

if active_profile:
    for key, value in active_profile.env_overrides.items():
        env = [e for e in c.get("env", []) if not e.startswith(f"{key}=")]
        env.append(f"{key}={value}")
        c["env"] = env

    # [G12-FIX] Ports als dicts mit "ip"-Schlüssel — NICHT als Port-Objekte.
    # Der bestehende Port-Loop kennt nur int und dict.
    if active_profile.expose_ports_externally:
        new_ports = []
        for p in c.get("ports", []):
            if isinstance(p, Port):
                new_ports.append({
                    "internal": p.internal, "external": p.external,
                    "protocol": p.protocol, "ip": "0.0.0.0",
                })
            elif isinstance(p, dict):
                new_ports.append({**p, "ip": "0.0.0.0"})
            elif isinstance(p, int):
                new_ports.append({
                    "internal": p, "external": p,
                    "protocol": "tcp", "ip": "0.0.0.0",
                })
        c["ports"] = new_ports
```

### 12.5b — Port-Loop ersetzen

Den bestehenden Port-Loop in `_phase4_generate_ir()` durch diesen ersetzen:
```python
publish_host_ports = self._should_publish_host_ports(
    c, governed["networks"], public_reverse_proxy_present,
)

ports = []
for p in c.get("ports", []):
    if isinstance(p, Port):
        internal_port = p.internal
        external_port = p.external
        protocol = p.protocol
        port_ip = p.ip
    elif isinstance(p, int):
        internal_port = p
        external_port = p
        protocol = "tcp"
        port_ip = None
    elif isinstance(p, dict):
        internal_port = p.get("internal", p.get("port", 0))
        external_port = p.get("external", p.get("port", 0))
        protocol = p.get("protocol", "tcp")
        port_ip = p.get("ip")  # "0.0.0.0" wenn expose_externally gesetzt
    else:
        continue

    if not (1 <= internal_port <= 65535 and 1 <= external_port <= 65535):
        logger.warning(
            "Skipping invalid port internal=%s external=%s for %s",
            internal_port, external_port, c.get("name", "?"),
        )
        continue

    # [G9-FIX] expose_externally übersteuert _should_publish_host_ports().
    if not publish_host_ports and not expose_externally:
        continue

    allocated_external = self._allocate_unique_external_port(
        external_port, used_external_ports
    )
    ports.append(Port(
        internal=internal_port,
        external=allocated_external,
        protocol=protocol,
        ip=port_ip,
    ))
```

### 12.6 (Renderer) — `tofu_renderer.py`: `ip`-Feld nur schreiben wenn gesetzt

```python
port_block = {
    "internal": port.internal,
    "external": port.external,
    "protocol": port.protocol,
}
if port.ip is not None:
    port_block["ip"] = port.ip
```

---

## Smoke-Test nach dem Call

```python
# Container in interner Zone MIT vulnerability_profile + expose_ports_externally=True
# → Port erscheint in DeployContainer.ports (Gate bypassed)

# Container in interner Zone OHNE vulnerability_profile
# → Port verworfen (Gate greift normal)

# Port.ip="0.0.0.0" → im Renderer: ip-Feld im Terraform-Block vorhanden
# Port.ip=None → im Renderer: kein ip-Feld
```

---

## Wichtige Folgeverweise

- **Phase 14.2** (Call 24): Port-Exposition-Warning prüft `active_profile` (nicht `vuln_profile_name`) auf der finalen `ports`-Liste
- **Phase 16.3** (Call 26): Externer Port aus finaler `ports`-Liste lesen: `next((p.external for p in ports if p.ip == "0.0.0.0"), None)`
