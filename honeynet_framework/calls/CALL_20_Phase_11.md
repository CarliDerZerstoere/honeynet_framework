# Call 20 — Phase 11: Realistischer Hintergrund-Traffic

**Schwierigkeit:** ★★☆ mittel  
**Abhängigkeiten:** Call 12 (Phase 6b.5 — `TrafficProfile` in `ImageCatalogEntry`), Call 09 (Phase 4.2 — IR-Struktur)  
**Risiko:** mittel — neue Datenklassen + Compiler-Logik für Traffic-Sidecar

---

## Ziel

Für jeden Container mit einem `traffic_profile` im Katalog einen
Traffic-Generator-Sidecar in die IR einbauen. Der Sidecar läuft
im selben Backbone-Netzwerk wie der Ziel-Container.

---

## Dateien

### Zu editieren
- `honeynet_framework/deploy_compiler.py` — `_phase4_generate_ir()` + Hilfsmethode
- `honeynet_framework/enricher.py` — LLM-Prompt erweitern

### Als Kontext mitgeben
- `deploy_compiler.py` vollständig
- `catalog/models.py` — Signatur `TrafficProfile`
- `models.py` — Signatur `DeployContainer`

---

## Änderungen

### 11.2 — `enricher.py`: LLM-Prompt für Traffic-Profile

```
- traffic_profile: Realistic background activity for this service.
  type must be one of: "http", "postgres", "redis", "dns"
  For postgres: set generator_password to match POSTGRES_PASSWORD in the
    service config. The compiler automatically mirrors generator_password
    to PGPASSWORD in the generator container's environment.
    Do NOT set POSTGRES_HOST_AUTH_METHOD in generator_env_overrides.
```

### 11.4 — `deploy_compiler.py`: Traffic-Generator-Command

**Hilfsmethode** (außerhalb der Klasse):
```python
def _build_traffic_generator_command(
    target_name: str,
    profile: "TrafficProfile",
    port: Optional[int] = None,
) -> list[str]:
    user = profile.generator_user or ""
    password = profile.generator_password or ""

    if profile.type == "http":
        host_url = f"http://{target_name}" + (f":{port}" if port else "")
        cmds = " ".join([
            f"curl -sf {host_url}{cmd.split(' ', 1)[1]} >/dev/null 2>&1;"
            for cmd in profile.commands if cmd.startswith("GET ")
        ])
    elif profile.type == "postgres":
        port_flag = f"-p {port}" if port else ""
        cmds = " ".join([
            f'psql -h {target_name} {port_flag} -U {user} -c "{q}" >/dev/null 2>&1;'
            for q in profile.commands
        ])
    elif profile.type == "redis":
        auth_flag = f"-a {password}" if password else ""
        port_flag = f"-p {port}" if port else ""
        cmds = " ".join([
            f"redis-cli -h {target_name} {port_flag} {auth_flag} {q} >/dev/null 2>&1;"
            for q in profile.commands
        ])
    elif profile.type == "dns":
        cmds = " ".join([f"nslookup {q} >/dev/null 2>&1;" for q in profile.commands])
    else:
        cmds = "true"

    script = f"while true; do {cmds} sleep {profile.interval_seconds}; done"
    return ["sh", "-c", script]
```

### 11.4 — `deploy_compiler.py`: Sidecar in `_phase4_generate_ir()`

Im Container-Loop, nach dem Haupt-`DeployContainer`-Append:
```python
# Traffic-Sidecar für interne Simulation
tp = getattr(entry, "traffic_profile", None) if entry else None
if tp and tp.generator_image:
    # Backbone-Netzwerk des Ziel-Containers verwenden
    backbone = c.get("backbone_network", "")
    # Für postgres: generator_password → PGPASSWORD
    env_overrides = dict(tp.generator_env_overrides or {})
    if tp.type == "postgres" and tp.generator_password:
        env_overrides.setdefault("PGPASSWORD", tp.generator_password)
    # Ziel-Name: Hostname wenn gesetzt, sonst Container-Name
    target_name = hostname or c.get("stable_name", c["name"])
    # Internen Port des Diensts ermitteln (erster Port in der Liste)
    svc_port = ports[0].internal if ports else None
    containers.append(DeployContainer(
        name=f"{c.get('stable_name', c['name'])}-traffic",
        image=tp.generator_image,
        networks=[backbone] if backbone else [],
        command=_build_traffic_generator_command(target_name, tp, port=svc_port),
        env=[f"{k}={v}" for k, v in env_overrides.items()],
        archetype="",
        depends_on=[c.get("stable_name", c["name"])],
        upload_files=[],
    ))
```

---

## Smoke-Test nach dem Call

```python
from honeynet_framework.deploy_compiler import _build_traffic_generator_command

# HTTP-Traffic-Command
class MockProfile:
    type = "http"
    generator_user = None
    generator_password = None
    commands = ["GET /health"]
    interval_seconds = 30

cmd = _build_traffic_generator_command("nginx", MockProfile())
assert cmd[0] == "sh"
assert "curl" in cmd[2]
assert "while true" in cmd[2]

# Postgres
class PgProfile:
    type = "postgres"
    generator_user = "app"
    generator_password = "secret"
    commands = ["SELECT 1;"]
    interval_seconds = 60

cmd_pg = _build_traffic_generator_command("db", PgProfile(), port=5432)
assert "psql" in cmd_pg[2]
assert "-p 5432" in cmd_pg[2]
```
