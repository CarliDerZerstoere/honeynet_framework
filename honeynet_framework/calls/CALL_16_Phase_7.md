# Call 16 — Phase 7: Log-Aggregation (deploy_compiler, catalog/models, orchestrator)

**Schwierigkeit:** ★★★ hoch  
**Abhängigkeiten:** Call 12 (Phase 6b.5 — `log_paths` in `ImageCatalogEntry`), Call 13 (Phase 6.1 — `OrchestratorConfig`)  
**Risiko:** hoch — drei Dateien; kritische Pfadtrennungslogik (Self-Ingestion-Prävention)

---

## Ziel

Log-Aggregation implementieren:
- `log_paths`-Feld im Katalog (bereits in Call 12 angelegt)
- `archetype`-Feld in `DeployContainer`
- `backbone_network_name` in `DeployProjection`
- `OrchestratorConfig`-Erweiterung
- `_log_safe_name()` + `_attach_log_mounts()` + `_render_filebeat_config()` + `_inject_log_aggregator()` in `orchestrator.py`
- `upload`-Block in `tofu_renderer.py`

**Kritische Architektur:** Output-Pfad von Filebeat (`session_log_root + "_out"`)
ist ein **Geschwisterverzeichnis** von `session_log_root` — kein Unterordner.
Damit kann der Input-Mount `/usr/share/filebeat/logs` den Output physisch
nicht erreichen. Self-Ingestion ist architektonisch ausgeschlossen.

---

## Dateien

### Zu editieren
- `honeynet_framework/models.py` — `DeployContainer.archetype`, `DeployProjection.backbone_network_name`, `OrchestratorConfig`
- `honeynet_framework/deploy_compiler.py` — `archetype` in IR setzen, `backbone_network_name` in `DeployProjection`
- `honeynet_framework/orchestrator.py` — alle Log-Aggregation-Helfer
- `honeynet_framework/tofu_renderer.py` — `upload`-Block

### Als Kontext mitgeben
- Alle vier Dateien vollständig
- `catalog/models.py` — Signatur `ImageCatalogEntry.log_paths`
- `catalog_inference.py` — Signatur `get_catalog_entry_for_archetype`

---

## Änderungen

### 7.3 — `models.py`: `archetype`-Feld in `DeployContainer`

```python
@dataclass
class DeployContainer:
    # ... bestehende Felder ...
    archetype: str = ""
    upload_files: list[dict] = field(default_factory=list)
```

### 7.5 — `models.py`: `DeployProjection` um `backbone_network_name`

```python
@dataclass
class DeployProjection:
    project_name: str
    networks: list[DeployNetwork] = field(default_factory=list)
    containers: list[DeployContainer] = field(default_factory=list)
    volumes: list[str] = field(default_factory=list)
    constraints: TerraformConstraints = field(default_factory=TerraformConstraints)
    backbone_network_name: str = ""
    validation_passed: bool = False
```

### 7.6 — `models.py`: `OrchestratorConfig` erweitern

```python
@dataclass
class OrchestratorConfig:
    # ... bestehende Felder ...
    enable_log_aggregation: bool = False
    log_aggregation_output: str = "file"
    log_aggregation_host: Optional[str] = None
    log_aggregator_image: str = ""
    log_base_dir: str = ""
    evidence_dir: str = ""
```

### 7.3 — `deploy_compiler.py`: `archetype` in IR setzen

```python
containers.append(DeployContainer(
    name=c.get("stable_name", c["name"]),
    image=c["image"],
    archetype=c.get("archetype", ""),   # ← NEU
    ...
))
```

`backbone_network_name` in `DeployProjection` setzen:
```python
return DeployProjection(
    ...
    backbone_network_name=backbone_name,   # ← NEU
)
```

### 7.4 — `orchestrator.py`: Slug-Helfer

```python
import re as _re
import hashlib as _hashlib

def _container_safe_name(name: str) -> str:
    return _re.sub(r"[^a-z0-9_]", "_", name.lower())

def _log_safe_name(container_name: str) -> str:
    """Deterministisch eindeutiger Slug für das Log-Verzeichnis eines Containers.

    Hängt einen 8-Zeichen SHA-256-Präfix an für volle Eindeutigkeit.
    Beide Funktionen (_attach_log_mounts, _render_filebeat_config) MÜSSEN
    diesen Helper verwenden — nie _container_safe_name() direkt.
    """
    slug = _container_safe_name(container_name)
    h = _hashlib.sha256(container_name.encode()).hexdigest()[:8]
    return f"{slug}_{h}"
```

### 7.4 — `orchestrator.py`: `_attach_log_mounts`

```python
def _attach_log_mounts(
    containers: list["DeployContainer"],
    catalog: Optional["CatalogSnapshot"],
    session_log_root: str,
) -> None:
    """Post-Compile-Mutation auf deepcopy — nie auf dem Original."""
    if not catalog:
        return
    import os, re
    for container in containers:
        if not container.archetype:
            continue
        entry = get_catalog_entry_for_archetype(container.archetype, catalog)
        if not entry or not getattr(entry, "log_paths", None):
            continue
        safe_name = _log_safe_name(container.name)
        log_dirs: set[str] = set()
        for log_path in entry.log_paths:
            log_dirs.add(os.path.dirname(log_path))
        for container_log_dir in sorted(log_dirs):
            dir_slug = re.sub(r"[^a-z0-9_]", "_", container_log_dir.strip("/"))
            host_dir = f"{session_log_root}/{safe_name}/{dir_slug}"
            container.volumes.append(f"{host_dir}:{container_log_dir}")
```

### 7.6 — `orchestrator.py`: `_inject_log_aggregator`

```python
def _inject_log_aggregator(
    projection: "DeployProjection",
    config: "OrchestratorConfig",
    session_log_root: str,
    catalog: Optional["CatalogSnapshot"] = None,
) -> None:
    if not config.log_aggregator_image:
        raise ValueError(
            "log_aggregator_image must be set in OrchestratorConfig "
            "when enable_log_aggregation=True"
        )
    filebeat_yml = _render_filebeat_config(projection.containers, config, catalog=catalog)
    backbone = projection.backbone_network_name
    import os
    filebeat_out_host = session_log_root.rstrip("/").rstrip(os.sep) + "_out"
    projection.containers.append(DeployContainer(
        name="hn-log-aggregator",
        image=config.log_aggregator_image,
        networks=[backbone] if backbone else [],
        volumes=[
            f"{session_log_root}:/usr/share/filebeat/logs:ro",
            f"{filebeat_out_host}:/usr/share/filebeat/output",
        ],
        env=[],
        depends_on=[],
        upload_files=[{
            "content": filebeat_yml,
            "file": "/usr/share/filebeat/filebeat.yml",
            "executable": False,
        }],
    ))
```

### 7.8 — `orchestrator.py`: `_render_filebeat_config`

```python
def _render_filebeat_config(
    containers: list["DeployContainer"],
    config: "OrchestratorConfig",
    catalog: Optional["CatalogSnapshot"] = None,
) -> str:
    import os, re as _re
    log_paths: list[str] = []
    for c in containers:
        if c.name == "hn-log-aggregator":
            continue
        safe = _log_safe_name(c.name)

        def _host_path_from_vol(vol: str) -> str:
            parts = vol.split(":")
            if len(parts) >= 3 and len(parts[0]) == 1 and parts[0].isalpha():
                return parts[0] + ":" + parts[1]
            return parts[0]

        has_log_mount = any(
            safe in _re.split(r"[/\\]", _host_path_from_vol(vol))
            for vol in c.volumes
            if ":" in vol
        )
        if not has_log_mount:
            continue

        entry = (
            get_catalog_entry_for_archetype(c.archetype, catalog)
            if catalog and c.archetype else None
        )
        if entry and getattr(entry, "log_paths", None):
            for lp in entry.log_paths:
                dir_slug = _re.sub(r"[^a-z0-9_]", "_", os.path.dirname(lp).strip("/"))
                basename = os.path.basename(lp)
                log_paths.append(
                    f"/usr/share/filebeat/logs/{safe}/{dir_slug}/{basename}"
                )
        else:
            log_paths.append(f"/usr/share/filebeat/logs/{safe}/**/*")

    # catalog=None → log_paths leer → Fallback feuert.
    # Self-Ingestion ausgeschlossen: filebeat_out_host = session_log_root + "_out"
    # ist Geschwisterverzeichnis, nicht Unterordner von session_log_root.
    paths_yaml = "\n".join(f"    - {p}" for p in log_paths) if log_paths \
        else "    - /usr/share/filebeat/logs/**/*"

    if config.log_aggregation_output == "elasticsearch" and config.log_aggregation_host:
        output_section = f"""
output.elasticsearch:
  hosts: ["{config.log_aggregation_host}"]
  index: "honeynet-%{{+yyyy.MM.dd}}"
"""
    else:
        output_section = """
output.file:
  path: "/usr/share/filebeat/output"
  filename: "honeynet.ndjson"
  rotate_every_kb: 10240
  number_of_files: 5
"""

    return f"""filebeat.inputs:
- type: log
  enabled: true
  paths:
{paths_yaml}
  json.keys_under_root: false
  fields_under_root: true
  fields:
    source: honeynet
{output_section}
logging.level: warning
logging.to_files: false
"""
```

### 7.7 — `tofu_renderer.py`: `upload`-Block

```python
if container.upload_files:
    container_block["upload"] = [
        {
            "content":    u["content"],
            "file":       u["file"],
            "executable": u.get("executable", False),
        }
        for u in container.upload_files
    ]
```

### Integration in `_deploy_deterministic_v2()`

```python
import uuid as _uuid, copy

session_id = str(_uuid.uuid4())
log_base = self.config.log_base_dir or str(self.config.work_dir / "logs")
session_log_root = f"{log_base}/{session_id}"

deploy_projection_for_render = deploy_projection
if self.config.enable_log_aggregation:
    log_containers = copy.deepcopy(deploy_projection.containers)
    _attach_log_mounts(log_containers, catalog_snapshot, session_log_root)
    deploy_projection_for_render = copy.copy(deploy_projection)
    deploy_projection_for_render.containers = log_containers
    _inject_log_aggregator(
        deploy_projection_for_render, self.config, session_log_root,
        catalog=catalog_snapshot,
    )

tofu_renderer.render(deploy_projection_for_render, ...)
result.deploy_projection = deploy_projection   # saubere IR, ohne Aggregator
```

---

## Kritischer Integrationstest nach dem Call

```bash
# Nach Docker-Deploy mit enable_log_aggregation=True:
docker inspect hn-log-aggregator --format '{{json .Mounts}}'
# MUSS zeigen: session_log_root → /usr/share/filebeat/logs:ro
#              session_log_root + "_out" → /usr/share/filebeat/output:rw
# session_log_root + "_out" DARF KEIN Unterordner von session_log_root sein!
```

```python
# Slug-Konsistenz-Test
from honeynet_framework.orchestrator import _log_safe_name
slug1 = _log_safe_name("nginx")
# _attach_log_mounts und _render_filebeat_config liefern denselben Slug
assert _log_safe_name("nginx") == _log_safe_name("nginx")  # deterministisch
assert _log_safe_name("nginx-1") != _log_safe_name("nginx_1")  # kollisionsfrei

# filebeat_out nicht in Input-Pfaden
config = OrchestratorConfig(log_aggregation_output="file")
yaml = _render_filebeat_config([], config, catalog=None)
assert "filebeat_out" not in yaml.split("output.file")[0]
assert "/usr/share/filebeat/output" in yaml  # aber in output-Sektion
```
