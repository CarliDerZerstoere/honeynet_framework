# Call 09 — Phase 4.2: deploy_compiler.py — Backbone + Alias-Policy

**Schwierigkeit:** ★★★ hoch  
**Abhängigkeiten:** Call 08 (Phase 4.1 — `DeployContainer` mit Netzwerk-Feldern), Call 01 (Phase 2.1 — `get_catalog_entry_for_archetype`)  
**Risiko:** hoch — zentrale `_phase4_generate_ir()` wird umgebaut

---

## Ziel

Backbone-Netzwerk einführen, Archetype-Zählung NACH Replikation verschieben
und globale Alias-Eindeutigkeit sicherstellen. Neue Hilfsmethoden
`_backbone_network_name`, `_sanitize_hostname`, `_sanitize_aliases`,
`_get_network_aliases_from_catalog` in `DeployCompiler` implementieren.

---

## Dateien

### Zu editieren
- `honeynet_framework/deploy_compiler.py` (~1.100 Zeilen)

### Als Kontext mitgeben
- `deploy_compiler.py` vollständig
- `models.py` — Signatur `DeployContainer` (neue Felder aus Call 08)
- `catalog_inference.py` — Signatur `get_catalog_entry_for_archetype`
- `models.py` — `CompilerError`-Dataclass

---

## Änderungen

### Neue statische Methoden in `DeployCompiler`

```python
@staticmethod
def _backbone_network_name(project_name: str) -> str:
    """Gemeinsames Netzwerk für alle Honeynet-Container.
    internal=True: Container erreichen sich gegenseitig,
    haben aber keinen Internetzugang (Honeywall).
    """
    safe = re.sub(r"[^a-z0-9_]", "_", project_name.lower())
    return f"hn_{safe}_default"

@staticmethod
def _sanitize_hostname(raw: str) -> Optional[str]:
    if not raw:
        return None
    h = re.sub(r"[^a-z0-9.-]", "",
               raw.strip().lower().replace("_", "-")).strip("-.")
    return h[:253] if 1 <= len(h) <= 253 else None

@staticmethod
def _sanitize_aliases(aliases: list[str]) -> list[str]:
    """Sanitize + dedupliziere DNS-Labels an der IR-Grenze."""
    seen: set[str] = set()
    result: list[str] = []
    for raw in aliases:
        label = re.sub(r"[^a-z0-9-]", "",
                       str(raw or "").strip().lower().replace("_", "-")
                       ).strip("-")
        if 1 <= len(label) <= 63 and label not in seen:
            seen.add(label)
            result.append(label)
    return result

def _get_network_aliases_from_catalog(
    self,
    container: dict,
    catalog: Optional["CatalogSnapshot"],
    archetype_counts: dict[str, int],
    allocated_aliases: set[str],
) -> list[str]:
    """Aliases nur wenn: (1) Einzelinstanz, (2) deployment-weit eindeutig."""
    if not catalog:
        return []
    archetype = (container.get("archetype") or "").strip().lower()
    if not archetype or archetype_counts.get(archetype, 0) != 1:
        return []

    raw: list[str] = []
    entry = get_catalog_entry_for_archetype(archetype, catalog)
    if entry and entry.network_aliases:
        raw = list(entry.network_aliases)
    else:
        contract = catalog.archetypes.get(archetype)
        if contract and getattr(contract, "network_aliases", None):
            raw = list(contract.network_aliases)

    sanitized = self._sanitize_aliases(raw)
    free = [a for a in sanitized if a not in allocated_aliases]
    lost = [a for a in sanitized if a in allocated_aliases]

    if lost:
        container_name = container.get("stable_name") or container.get("name", "?")
        msg = (
            f"Alias collision: '{container_name}' (archetype '{archetype}') "
            f"lost aliases {lost} — already claimed by an earlier container."
        )
        self.warnings.append(CompilerError(phase="IR", message=msg, system=container_name))
        logger.warning(msg)

    allocated_aliases.update(free)
    return free
```

### `_phase2_normalize()` — Backbone-Zuweisung (ohne `archetype_counts`)

```python
backbone_name = self._backbone_network_name(normalized["project_name"])
if "_backbone" not in normalized["networks"]:
    normalized["networks"]["_backbone"] = {
        "name": backbone_name, "driver": "bridge",
        "internal": True, "zone_name": "_backbone",
    }
zone = world_model.zones.get(norm_container["zone"])
zone_net = zone.deploy.network_name if zone and zone.deploy else None
norm_container["networks"] = [backbone_name]
if zone_net and zone_net != backbone_name:
    norm_container["networks"].append(zone_net)
norm_container["backbone_network"] = backbone_name
# KEIN archetype_counts hier — Replikation passiert erst in Phase 2b
```

### `_phase4_generate_ir()` — Zählung + Aliases + Hostname

```python
def _phase4_generate_ir(self, governed, world_model, catalog=None):
    # [F2] Archetype-Zählung NACH Replikation (Phase 2b) berechnen
    archetype_counts: dict[str, int] = {}
    for c in governed["containers"]:
        arch = (c.get("archetype") or "").lower()
        if arch:
            archetype_counts[arch] = archetype_counts.get(arch, 0) + 1

    allocated_aliases: set[str] = set()

    for c in governed["containers"]:
        # ... Port-Logik unverändert ...
        aliases = self._get_network_aliases_from_catalog(
            c, catalog, archetype_counts, allocated_aliases
        )
        backbone = c.get("backbone_network", "")
        network_aliases = {backbone: aliases} if aliases and backbone else {}

        sys_obj = world_model.systems.get(c["name"])
        raw_hostname = (
            (sys_obj.simulate.hostname or "").strip()
            if sys_obj and sys_obj.simulate else ""
        )
        hostname = self._sanitize_hostname(raw_hostname)

        containers.append(DeployContainer(
            name=c.get("stable_name", c["name"]),
            image=c["image"],
            networks=c.get("networks", []),
            network_aliases=network_aliases,
            hostname=hostname,
            env=c.get("env", []),
            ports=ports,
            volumes=c.get("volumes", []),
            depends_on=c.get("depends_on", []),
            command=c.get("command"),
        ))
```

### `compile()` — `catalog` an `_phase4_generate_ir` übergeben

```python
deploy_ir = self._phase4_generate_ir(governed, world_model, catalog=catalog)
```

---

## Smoke-Test nach dem Call

```python
from honeynet_framework.deploy_compiler import DeployCompiler

# Backbone-Name-Format
assert DeployCompiler._backbone_network_name("my-project") == "hn_my_project_default"

# Sanitize
assert DeployCompiler._sanitize_hostname("MY.HOST") == "my.host"
assert DeployCompiler._sanitize_aliases(["postgres", "postgres"]) == ["postgres"]
```
