# Umsetzungsplan: Honeynet Framework — Infrastruktur-Qualität

Stand: REV43 eingearbeitet. Findings G1–G15, P1–P3, REV33–REV43 adressiert.

## Scope-Abgrenzung

`iac_mode="llm"` ist explizit **außerhalb des Scope** dieses Plans.

Der llm-Pfad in `deploy_v2()` (L442–501) extrahiert, validiert und
kompiliert ohne `catalog_context`. Companion-Checks, Alias-Contracts und
Archetyp-Normalisierung greifen dort deshalb nicht. Das ist ein bewusster
Entscheid: der llm-Pfad ist Legacy und wird nicht aktiv weiterentwickelt.
Alle neuen Features gelten für `iac_mode="deterministic"` (Default).

---

## Phase 0 — Katalogerweiterung

### 0.1 — `catalog/models.py`: Neue Felder + Schema-Version

```python
CATALOG_SCHEMA_VERSION = "2"

@dataclass
class ImageCatalogEntry:
    # --- alle bestehenden Felder unverändert ---
    schema_version: str = ""
    network_aliases: list[str] = field(default_factory=list)
    # [F1b] Liste statt String — mehrere Pflicht-Abhängigkeiten möglich
    # (z.B. Notary benötigt notary-signer UND mysql)
    required_companion_archetypes: list[str] = field(default_factory=list)
```

`to_dict()` / `from_dict()` und `CatalogSnapshot`-Serialisierung alle drei
Felder einbeziehen.

**[F6-Teil] Provenance-Liste:** Wenn `catalog/models.py#L914` eine explizite
Feldliste für Versionierung/Diff enthält, dort alle drei Felder ergänzen.

### 0.2 — `catalog/models.py`: `ArchetypeContract` synchron halten

```python
@dataclass
class ArchetypeContract:
    # --- bestehende Felder ---
    network_aliases: list[str] = field(default_factory=list)
    required_companion_archetypes: list[str] = field(default_factory=list)

def entry_to_archetype_contract(entry: ImageCatalogEntry) -> ArchetypeContract:
    return ArchetypeContract(
        # --- bestehende Zuweisungen ---
        network_aliases=list(entry.network_aliases or []),
        required_companion_archetypes=list(entry.required_companion_archetypes or []),
    )
```

### 0.3 — `builder.py`: Cache-Invalidierung per Schema-Version

```python
from .models import CATALOG_SCHEMA_VERSION, ...

def _schema_ok(entry) -> bool:
    return getattr(entry, "schema_version", "") == CATALOG_SCHEMA_VERSION

# Offizieller Pfad (L203):
if not force_rebuild and cached_entry and docs_hash == cached_hash \
        and cached_verified and _schema_ok(cached_entry):
    entry = cached_entry
else:
    # bestehender Rebuild ...
    if enricher and docs_content:
        entry = await enricher.enrich(entry, docs_content)
    entry.schema_version = CATALOG_SCHEMA_VERSION

# Extended-Pfad (L244) analog.
```

### 0.4 — `enricher.py`: LLM-Prompt + Sanitisierung

**TIER_A/B_SCHEMA erweitern:**
```python
TIER_A_SCHEMA = """...
network_aliases: []
required_companion_archetypes: []"""
```

**LLM-Prompt:**
```
- network_aliases: DNS names this service provides on the shared network.
  Examples: mongo=["mongo"], postgres=["postgres","db"].
  Leave empty if no conventional short alias exists.

- required_companion_archetypes: List of archetypes this service CANNOT
  start without. Use a list — some services need multiple companions.
  Examples: rocketchat=["mongo"], notary=["notary-signer","mysql"].
  Leave empty if the service starts independently.
```

**Sanitisierungshelfer:**
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

**`_merge_entry()` — vor `return entry`:**
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

## Phase 2 — Archetyp-Normalisierung

**[F1] Normalisierung greift nur im Expansionspfad — Fix:**

Der Plan adressierte bisher nur `_merge_expansion_payload()`. Aber auch
der normale Parse-Pfad in `_parse_world_model()` (L1985–2001) schreibt
`archetype=str(deploy_data.get("archetype", ""))` direkt in `SystemDeploy`,
ohne Normalisierung. Danach ruft `_apply_catalog_defaults()` zwar
`apply_catalog_defaults_to_system()` auf, aber das macht keinen
Basename-Lookup: `get_catalog_entry_for_archetype("kafka", catalog)` findet
`apache/kafka` nicht, weil `_catalog_lookup_variants` nur `_normalize_text`
(lowercase/Whitespace) anwendet, nicht den PEP-503-Index.

**Lösung:** Normalisierung in `apply_catalog_defaults_to_system()` einbauen —
dem einzigen Ort der nach dem Parse für alle Systeme aufgerufen wird.

### 2.1 — `catalog_inference.py`: PEP-503-Normalisierer + Index

**[G1-KORREKTUR] Diese Funktionen existieren NICHT im aktuellen Code.**
REV4-F6 und REV5-F1 haben fälschlicherweise behauptet sie seien bereits
implementiert. Eine `rg`-Suche im aktuellen `catalog_inference.py` ergibt
0 Treffer für `_pep503_normalize`, `build_archetype_lookup_index` und
`normalize_expanded_archetype`. **Phase 2.1 muss die vollständige
Implementierung enthalten.**

Diese drei Funktionen am Ende von `catalog_inference.py` ergänzen (vor
`apply_catalog_defaults_to_system`):

```python
def _pep503_normalize(value: str) -> str:
    """PEP 503: lowercase, collapse runs of [-_.] to single '-'."""
    return re.sub(r"[-_.]+", "-", str(value or "").lower()).strip("-")


def build_archetype_lookup_index(
    catalog: "CatalogSnapshot",
) -> dict[str, str]:
    """Baut einen {pep503_key: canonical_archetype}-Index.

    Ein leerer String als Wert bedeutet "mehrdeutig" — mehrere Einträge
    passen auf diesen Key. normalize_expanded_archetype gibt dann None zurück.
    """
    index: dict[str, str] = {}

    def _register(key: str, canonical: str) -> None:
        if not key:
            return
        if key in index and index[key] != canonical:
            index[key] = ""  # Kollision → mehrdeutig
        else:
            index[key] = canonical

    for entry in catalog.entries:
        canonical = entry.archetype
        for ident in catalog_entry_identifier_values(entry):
            _register(_pep503_normalize(ident), canonical)
            # Basename (nach letztem /) zusätzlich registrieren
            if "/" in ident:
                basename = ident.rsplit("/", 1)[-1]
                _register(_pep503_normalize(basename), canonical)

    return index


def normalize_expanded_archetype(
    archetype: str,
    catalog: "CatalogSnapshot",
    archetype_index: Optional[dict] = None,
) -> Optional[str]:
    """Löst einen rohen Archetype-String auf den kanonischen Katalog-Eintrag auf.

    Gibt None zurück wenn:
    - der Archetype unbekannt ist (kein Treffer im Katalog), oder
    - mehrdeutig ist (mehrere Einträge passen — Kollision im Index).

    Der Aufrufer muss None explizit behandeln (logger.warning + Downstream-Error
    vom Validator wenn strict_catalog=True).
    """
    if not archetype or not catalog:
        return None

    index = archetype_index if archetype_index is not None \
        else build_archetype_lookup_index(catalog)

    key = _pep503_normalize(archetype)
    canonical = index.get(key)
    if canonical:  # "" = mehrdeutig → None
        return canonical or None

    # Basename-Fallback
    if "/" in archetype:
        base_key = _pep503_normalize(archetype.rsplit("/", 1)[-1])
        canonical = index.get(base_key)
        if canonical:
            return canonical or None

    # Technology-Alias-Fallback (mongodb→mongo etc.)
    for alias in _technology_lookup_variants(archetype):
        alias_key = _pep503_normalize(alias)
        canonical = index.get(alias_key)
        if canonical:
            return canonical or None

    return None
```

### 2.2 — `catalog_inference.py`: `apply_catalog_defaults_to_system` normalisiert Archetype

**Vor dem bestehenden `get_catalog_entry_for_archetype`-Aufruf (L878) einfügen:**

```python
def apply_catalog_defaults_to_system(
    system: System,
    catalog: Optional["CatalogSnapshot"],
    archetype_index: Optional[dict] = None,  # NEU — gecachter Index
) -> System:
    if not system.deploy or not catalog:
        # ... bestehende Kind-Inferenz ...

    deploy = system.deploy
    archetype = str(deploy.archetype or "").strip()

    # [F1] Normalisierung vor dem Katalog-Lookup.
    # Löst "kafka" → "apache/kafka" für ALLE Parsepfade,
    # nicht nur für den Expansionspfad.
    if archetype:
        normalized = normalize_expanded_archetype(archetype, catalog, archetype_index)
        if normalized and normalized != archetype:
            logger.debug(
                "Archetype normalised: '%s' → '%s' for system '%s'",
                archetype, normalized, system.name,
            )
            archetype = normalized
            deploy = replace(deploy, archetype=archetype)
        elif normalized is None and archetype:
            # normalize_expanded_archetype gibt None zurück wenn der Name
            # entweder unbekannt ODER mehrdeutig ist (mehrere Katalogeinträge
            # passen). Statt still weiterzumachen: logger.warning ausgeben.
            #
            # WICHTIG — strict_catalog-Interaktion:
            # Im deterministischen Pfad läuft der Validator standardmäßig mit
            # strict_catalog=True (world_model_validator.py L74). Wenn der
            # Archetype nach dieser Normalisierung immer noch nicht im Katalog
            # gefunden wird, erzeugt der Validator anschließend einen
            # POLICY_ARCHETYPE-Error (L861) — kein stilles Fallback.
            # Das logger.warning hier ist für frühe Sichtbarkeit im Log;
            # der harte Fehler kommt vom Validator.
            logger.warning(
                "Archetype '%s' for system '%s' could not be resolved to a "
                "unique catalog entry — using as-is. Validator will raise "
                "POLICY_ARCHETYPE error if strict_catalog=True (default).",
                archetype, system.name,
            )

    entry = get_catalog_entry_for_archetype(archetype, catalog) if archetype else None
    # ... restlicher Code unverändert ...
```

`_apply_catalog_defaults()` im Extractor baut den Index einmal und reicht
ihn durch:

```python
def _apply_catalog_defaults(
    self,
    world_model: WorldModel,
    catalog_context: "CatalogSnapshot",
) -> WorldModel:
    index = build_archetype_lookup_index(catalog_context)
    for name, system in list(world_model.systems.items()):
        world_model.systems[name] = apply_catalog_defaults_to_system(
            system, catalog_context, archetype_index=index
        )
    return world_model
```

### 2.3 — `world_model_extractor.py`: Expansion-Pfad

```python
from .catalog_inference import (
    normalize_expanded_archetype,
    build_archetype_lookup_index,
)
```

In `_expand_world_model_scope()`:
```python
archetype_index = (
    build_archetype_lookup_index(catalog_context) if catalog_context else None
)
added = self._merge_expansion_payload(
    ..., catalog=catalog_context, archetype_index=archetype_index,
)
```

`_merge_expansion_payload()` normalisiert wie gehabt (Signatur mit
`catalog` + `archetype_index`-Parameter, `normalize_expanded_archetype`
vor `SystemDeploy()`).

Die Normalisierung greift jetzt an allen drei Eintrittspunkten:
- normaler Parse → über `apply_catalog_defaults_to_system`
- Expansions-Payload → direkt in `_merge_expansion_payload`
- `_apply_catalog_defaults` nach Parse → wiederum über
  `apply_catalog_defaults_to_system`

### 2.4 — `world_model_repair.py` + `semantic_repair.py`: Repair-Loops

`apply_catalog_defaults_to_system` wird an sechs bestehenden Stellen ohne
`archetype_index` aufgerufen. Nur diese sechs Stellen werden geändert —
kein neuer Sweep über alle Systeme, kein verändertes Reparaturverhalten.

**`world_model_repair.py` — Index einmal in `repair()` bauen:**

L134 liegt in der Hilfsmethode `_add_system_for_candidate()`. Um den Index
dort zu nutzen, muss deren Signatur um `archetype_index` erweitert werden.

```python
def repair(self, world_model, validation_result, catalog):
    if not catalog:
        return world_model
    from .catalog_inference import build_archetype_lookup_index
    index = build_archetype_lookup_index(catalog)  # einmal hier

    new_systems = dict(world_model.systems)

    # L42
    for name, system in list(new_systems.items()):
        new_systems[name] = apply_catalog_defaults_to_system(
            system, catalog, archetype_index=index
        )

    for err in list(validation_result.errors) + list(validation_result.warnings):
        if err.rule == 'CONTRACT_REQUIRED_ENV':
            system_name = self._extract_system_name(err.details)
            if system_name and system_name in new_systems:
                # L48
                new_systems[system_name] = apply_catalog_defaults_to_system(
                    new_systems[system_name], catalog, archetype_index=index
                )
        # L95 (in anderem elif-Zweig, gleiche Signatur)
        # new_systems[sys_name] = apply_catalog_defaults_to_system(
        #     replace(system, deploy=new_deploy), catalog, archetype_index=index
        # )

    # L134 liegt in _add_system_for_candidate — Signatur erweitern:
    # self._add_system_for_candidate(world_model, new_systems, candidate,
    #                                catalog, archetype_index=index)
```

```python
@staticmethod
def _add_system_for_candidate(
    world_model, systems, candidate, catalog,
    archetype_index=None,  # NEU
) -> None:
    # ... unverändert bis zur letzten Zeile ...
    systems[name] = apply_catalog_defaults_to_system(
        system, catalog, archetype_index=archetype_index  # L134
    )
```

**`semantic_repair.py` — Index einmal in `repair()` bauen:**

L166 liegt in `_add_missing_core_service()`. Auch hier Signatur erweitern.

```python
def repair(self, world_model, judge_result, catalog):
    from .catalog_inference import build_archetype_lookup_index
    index = build_archetype_lookup_index(catalog) if catalog else None

    # L88 — Archetype-Tausch
    systems[system_name] = apply_catalog_defaults_to_system(
        replace(system, deploy=new_deploy), catalog, archetype_index=index
    )

    # L166 liegt in _add_missing_core_service — Signatur erweitern:
    # self._add_missing_core_service(world_model, systems, context,
    #                                catalog, archetype_index=index)
```

```python
def _add_missing_core_service(
    self, world_model, systems, context, catalog,
    archetype_index=None,  # NEU
) -> bool:
    # ... unverändert bis zur letzten Zeile vor return True ...
    systems[name] = apply_catalog_defaults_to_system(
        system, catalog, archetype_index=archetype_index  # L166
    )
```

Alle sechs bestehenden Stellen bekommen `archetype_index=index`.
Kein neuer globaler Sweep, kein verändertes Reparaturverhalten.

### 2.5 — `world_model_validator.py`: Backstop bleibt unverändert

---

## Phase 4 — Netzwerk-Modell

### 4.1 — `models.py`: `DeployContainer` erweitern

**[REV31-G15] `network_mode` muss hier eingeführt werden, nicht erst in
Phase 16.2.** Phase 4.3 nutzt `container.network_mode` im Renderer (Guard für
`networks_advanced`). Wenn das Feld erst in Phase 16.2 (Woche 10) hinzukommt,
ist Phase 4 (Woche 3) nicht standalone ausführbar — `AttributeError` im
Renderer. Lösung: `network_mode` in Phase 4.1 ergänzen; Phase 16.2 nutzt dann
das bereits vorhandene Feld, ohne es nochmals einzuführen.

```python
@dataclass
class DeployContainer:
    name: str
    image: str
    networks: list[str] = field(default_factory=list)
    network_aliases: dict[str, list[str]] = field(default_factory=dict)
    hostname: Optional[str] = None
    network_mode: Optional[str] = None
    # "host" = Container nutzt Host-Netzwerk direkt.
    # Nur für externe Traffic-Generatoren (Phase 16) — nie für Honeynet-Services.
    # Feld hier in Phase 4 eingeführt, damit Phase 4.3 (Renderer-Guard) sofort
    # funktioniert. Phase 16.2 nutzt das Feld, führt es nicht erneut ein.
    # ... restliche Felder unverändert
```

### 4.2 — `deploy_compiler.py`: Backbone + korrigierte Alias-Policy

**Honeynet-Kontext: warum Backbone richtig ist:**

In einem Honeynet sollen simulierte Services füreinander erreichbar sein —
sonst wirkt die Täuschung nicht glaubwürdig. Zone-Isolation ist für die
*Außengrenze* gedacht (Honeynet vs. echte Produktionsumgebung), nicht für
die interne Kommunikation der simulierten Services untereinander. Das
entspricht etablierter Honeynet-Praxis: interne Services kommunizieren
normal, das Netzwerk-Segment als Ganzes ist vom Rest isoliert.

**[F2] Alias-Policy an der falschen Stelle — Fix:**

`compile()` läuft in der Reihenfolge:
```
_phase2_normalize() → _phase2_scope_alignment() [Replikation!]
→ _phase3_governance() → _phase4_generate_ir()
```

Wenn `archetype_counts` in `_phase2_normalize()` berechnet wird, sind die
Zählungen in `_phase4_generate_ir()` für replizierte Container veraltet.
Scope-Alignment (`deploy_compiler.py#L451`) fügt Repliken nach Phase 2 hinzu.

**Fix:** `archetype_counts` erst in `_phase4_generate_ir()` berechnen — nach
allen Phasen die Container hinzufügen.

**[F6] Sanitisierung an der Compiler-/IR-Grenze:**

Alte Snapshots oder manuell editierte Entries können ungültige Aliases
enthalten. Sanitisierung nur im Enricher reicht nicht — am Compiler-Rand
nochmals sanitizen und duplizierte Werte deduplizieren.

**Neue Methoden in `DeployCompiler`:**

```python
@staticmethod
def _backbone_network_name(project_name: str) -> str:
    """Gemeinsames Netzwerk für alle Honeynet-Container.
    internal=True: Container erreichen sich gegenseitig,
    haben aber keinen Internetzugang. Das ist die Honeywall —
    begrenzt Datenexfiltration und verhindert Missbrauch als
    Angriffs-Sprungbrett. Bandbreitenlimitierung (tc/iptables)
    ist außerhalb des Scope dieses Plans.
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
    """Sanitize + dedupliziere DNS-Labels an der IR-Grenze.
    Zweite Sicherheitslinie nach dem Enricher — fängt alte oder
    manuell editierte Snapshot-Werte ab.
    """
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
```

**`_phase2_normalize()` — nur Backbone-Zuweisung, keine Zählung:**

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
# Keine archetype_counts hier — Replikation passiert erst in Phase 2b
```

**`_phase4_generate_ir()` — Zählung + globale Alias-Eindeutigkeit:**

```python
def _phase4_generate_ir(
    self,
    governed: dict,
    world_model: WorldModel,
    catalog: Optional["CatalogSnapshot"] = None,
) -> DeployProjection:
    # [F2] Archetype-Zählung NACH Replikation (Phase 2b) berechnen
    archetype_counts: dict[str, int] = {}
    for c in governed["containers"]:
        arch = (c.get("archetype") or "").lower()
        if arch:
            archetype_counts[arch] = archetype_counts.get(arch, 0) + 1

    allocated_aliases: set[str] = set()  # deployment-weit eindeutig

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

**`_get_network_aliases_from_catalog` — mit Sanitisierung und Collision-Warning:**

```python
def _get_network_aliases_from_catalog(
    self,
    container: dict,
    catalog: Optional["CatalogSnapshot"],
    archetype_counts: dict[str, int],
    allocated_aliases: set[str],
) -> list[str]:
    """Aliases nur wenn: (1) Einzelinstanz, (2) deployment-weit eindeutig.

    Kollisionsregel: der erste Container in der finalen Reihenfolge der
    governed["containers"]-Liste gewinnt einen strittigen Alias. Diese Liste
    entsteht nach _phase2_scope_alignment() und _phase3_governance() — also
    NACH möglicher Replikation und Governance-Filterung, nicht direkt aus
    world_model.systems. Die Reihenfolge ist deterministisch für gleichen
    Input (Python dict-insertion-order + stabiles Appending in Phase 2/3),
    aber nicht inhaltlich priorisiert (z.B. nicht nach SystemKind oder Zone).

    Kollisionsverluste werden als CompilerError-Warning emittiert (sichtbar
    über get_warnings() und damit für CLI- und API-Konsumenten) UND per
    logger.warning geloggt.
    """
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
            f"lost aliases {lost} — already claimed by an earlier container. "
            f"Service reachable only under its container name."
        )
        # [P4] Strukturierte Warning über self.warnings — sichtbar für
        # get_warnings() und damit für CLI und API-Konsumenten.
        self.warnings.append(CompilerError(
            phase="IR",
            message=msg,
            system=container_name,
        ))
        logger.warning(msg)

    allocated_aliases.update(free)
    return free
```

In `compile()`:
```python
deploy_ir = self._phase4_generate_ir(governed, world_model, catalog=catalog)
```

### 4.3 — `tofu_renderer.py`: Aliases und Hostname rendern

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

---

## Phase 1 — Companion-Validator

**[O1] depends_on — nur Warnung, keine automatische Ableitung:**

Der Validator prüft ob `depends_on` gesetzt ist und emittiert eine Warnung
wenn nicht. Es gibt **keine Compiler-Phase die `depends_on` automatisch
ableitet** — `depends_on` wird in `_phase1_extract` (L204) als raw copy
übernommen und in `_phase4_generate_ir` (L800) unverändert gerendert.

Das ist eine bewusste Entscheidung: automatische Ableitung braucht eine
deterministische Regel bei Mehrfachinstanzen ("erster Treffer",
"alle Provider", "nur bei Einzelinstanz"). Diese Regel ist noch nicht
definiert. Bis dahin: Warnung emittieren, manuell handeln.

**Hinweis für die Repair-Loop-Integration:** Der Repair-Loop startet nur wenn
`validation_result.passed == False` (orchestrator.py L587), und `passed` hängt
ausschließlich an `errors`, nicht an `warnings` (world_model_validator.py L173).
Eine reine `CONTRACT_COMPANION_NO_DEPENDS_ON`-Warning löst den Repair-Loop also
gar nicht aus — sie ist einfach im Ergebnis vorhanden und wird dem Nutzer
angezeigt, aber nicht auto-repariert. Das ist das gewünschte Verhalten solange
keine automatische `depends_on`-Ableitung implementiert ist.

**Bereitschafts-Prüfung via `health_hints` + `wait` (OpenTofu-nativ):**

`depends_on` allein sagt OpenTofu "erstelle Ressource A vor B" — nicht
"warte bis A wirklich bereit ist". Der kreuzwerker/docker-Provider löst das
nativ: `wait = true` und `wait_timeout` lassen OpenTofu warten bis der
Healthcheck des Containers besteht, bevor der Plan als erfolgreich gilt.
Das ersetzt Docker-Compose-Semantik (`condition: service_healthy`) vollständig.

Der Enricher befüllt `health_hints` bereits (enricher.py L513) — dieses
bestehende Feld ist der Ausgangspunkt. Es wird nicht durch ein neues
`health_contract`-Feld ersetzt, sondern genutzt.

**`models.py`: `DeployContainer` um Healthcheck-Felder erweitern:**

```python
@dataclass
class DeployContainer:
    # ... bestehende Felder ...
    healthcheck: Optional[dict] = None
    # Beispiel: {"test": ["CMD", "pg_isready"], "interval": "10s", "retries": 5}
    wait: bool = False          # True = OpenTofu wartet auf healthy
    wait_timeout: int = 60      # Sekunden
```

**`deploy_compiler.py` — `_phase4_generate_ir()`: healthcheck + wait aus Katalog ableiten:**

```python
entry = get_catalog_entry_for_archetype(archetype, catalog)
healthcheck = None
wait = False
if entry and getattr(entry, "health_hints", None):
    primary_hint = entry.health_hints[0] if entry.health_hints else None
    if primary_hint:
        healthcheck = {
            "test": ["CMD-SHELL", primary_hint],
            "interval": "10s",
            "timeout":  "5s",
            "retries":  5,
            "start_period": "15s",
        }
        wait = True

containers.append(DeployContainer(
    ...
    healthcheck=healthcheck,
    wait=wait,
    wait_timeout=120,
))
```

**`tofu_renderer.py`: healthcheck-Block und wait rendern:**

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

### 1.1 — `world_model_validator.py`: Neue Methode

```python
def _validate_service_dependencies(
    self,
    wm: WorldModel,
    catalog: Optional["CatalogSnapshot"],
) -> tuple[list[ValidationError], list[ValidationError]]:
    if not catalog:
        return [], []

    errors: list[ValidationError] = []
    warnings: list[ValidationError] = []

    index = build_archetype_lookup_index(catalog)

    def _canonical(raw: str) -> str:
        result = normalize_expanded_archetype(raw, catalog, index)
        return result if result else raw.strip().lower()

    deployed_archetypes: set[str] = {
        _canonical(sys.deploy.archetype)
        for sys in wm.systems.values()
        if sys.deploy and sys.deploy.archetype
    }

    archetype_to_names: dict[str, list[str]] = {}
    for name, sys in wm.systems.items():
        if sys.deploy and sys.deploy.archetype:
            canon = _canonical(sys.deploy.archetype)
            archetype_to_names.setdefault(canon, []).append(name)

    for sys_name, system in wm.systems.items():
        if not system.deploy or not system.deploy.archetype:
            continue

        canonical_self = _canonical(system.deploy.archetype)
        entry = get_catalog_entry_for_archetype(canonical_self, catalog)
        if not entry:
            continue

        seen_companions: set[str] = set()
        companions: list[str] = []
        for c in (entry.required_companion_archetypes or []):
            if not str(c).strip():
                continue
            canon_c = _canonical(c)
            if canon_c not in seen_companions:
                seen_companions.add(canon_c)
                companions.append(canon_c)

        for companion in companions:
            if companion not in deployed_archetypes:
                errors.append(ValidationError(
                    rule="CONTRACT_COMPANION_MISSING",
                    details=(
                        f"System '{sys_name}' "
                        f"(archetype '{system.deploy.archetype}') "
                        f"benötigt Companion-Service '{companion}', "
                        f"der im WorldModel fehlt."
                    ),
                    severity="ERROR",
                    fix_hint=f"Füge archetype='{companion}' zum WorldModel hinzu.",
                ))
            else:
                providers = archetype_to_names.get(companion, [])
                existing_depends_on = set(system.deploy.depends_on or [])

                if len(providers) == 1:
                    provider_name = providers[0]
                    if provider_name not in existing_depends_on:
                        warnings.append(ValidationError(
                            rule="CONTRACT_COMPANION_NO_DEPENDS_ON",
                            details=(
                                f"System '{sys_name}' benötigt '{companion}' "
                                f"('{provider_name}'), hat aber keinen "
                                f"depends_on-Eintrag."
                            ),
                            severity="WARNING",
                            fix_hint=(
                                f"Ergänze '{provider_name}' in "
                                f"deploy.depends_on für '{sys_name}'."
                            ),
                        ))
                elif len(providers) > 1:
                    if not existing_depends_on.intersection(providers):
                        warnings.append(ValidationError(
                            rule="CONTRACT_COMPANION_NO_DEPENDS_ON",
                            details=(
                                f"System '{sys_name}' benötigt '{companion}', "
                                f"aber es existieren {len(providers)} Provider "
                                f"({', '.join(providers[:3])}). Kein eindeutiger "
                                f"depends_on ableitbar."
                            ),
                            severity="WARNING",
                            fix_hint=(
                                f"Setze depends_on manuell auf den gewünschten "
                                f"Provider für '{sys_name}'."
                            ),
                        ))

    return errors, warnings
```

**Import ergänzen** in `world_model_validator.py`:
```python
from .catalog_inference import (
    ...,
    build_archetype_lookup_index,
    normalize_expanded_archetype,
)
```

### 1.2 — Einbindung in `validate()`

```python
# 10. Service-Dependency-Contracts
if catalog:
    dep_errors, dep_warnings = self._validate_service_dependencies(wm, catalog)
    errors.extend(dep_errors)
    warnings.extend(dep_warnings)
```

---

## Phase 6 — Ehrliche Deploy-Ergebnisse

### 6.1 — `models.py`: Status + Ergebnis-Typen

```python
class DeploymentStatus(Enum):
    PENDING = "pending"
    PLANNING = "planning"
    DEPLOYING = "deploying"
    DEPLOYED = "deployed"
    RUNTIME_DEGRADED = "runtime_degraded"
    PARTIAL = "partial"
    FAILED = "failed"

@dataclass
class ContainerDiagnosis:
    name: str
    exit_code: int = 0
    oom_killed: bool = False
    category: str = "UNKNOWN"
    one_line: str = ""

@dataclass
class RuntimeSummary:
    total_expected: int = 0
    total_running: int = 0
    degraded: list[ContainerDiagnosis] = field(default_factory=list)

    @property
    def unhealthy_count(self) -> int:
        return sum(1 for d in self.degraded if d.category == "UNHEALTHY")

    @property
    def healthy_running(self) -> int:
        return self.total_running - self.unhealthy_count

    @property
    def is_healthy(self) -> bool:
        return len(self.degraded) == 0

    @property
    def is_total_failure(self) -> bool:
        return self.total_running == 0 and self.total_expected > 0

    @property
    def failure_groups(self) -> dict[str, list[str]]:
        groups: dict[str, list[str]] = {}
        for d in self.degraded:
            groups.setdefault(d.category, []).append(d.name)
        return groups

    def human_summary(self) -> str:
        if self.is_healthy:
            return (
                f"Alle {self.total_running}/{self.total_expected} "
                f"Container laufen"
            )
        lines = [
            f"{self.healthy_running}/{self.total_expected} Container "
            f"gesund laufend ({self.total_running} prozessseitig running)"
        ]
        for cat, names in self.failure_groups.items():
            lines.append(f"  {len(names)}x {cat}: {', '.join(names)}")
        return "\n".join(lines)

@dataclass
class DeploymentResult:
    # --- bestehende Felder ---
    runtime_summary: Optional[RuntimeSummary] = None

    @property
    def success(self) -> bool:
        return self.status in {
            DeploymentStatus.DEPLOYED,
            DeploymentStatus.RUNTIME_DEGRADED,
        }

    @property
    def fully_healthy(self) -> bool:
        return self.status == DeploymentStatus.DEPLOYED
```

### 6.2 — `deployer.py`: `RuntimeVerifier`

```python
class RuntimeVerifier:
    _CATEGORIES = [
        "MISSING_ENV", "DEPENDENCY_MISSING", "INTERACTIVE_IMAGE",
        "LICENSE_REQUIRED", "CONFIG_ERROR", "UNKNOWN",
    ]
    _MAX_LLM_BATCH = 10

    def __init__(self, llm: Optional["LLMProvider"] = None):
        self._llm = llm

    async def verify(
        self,
        verification: dict,
    ) -> "RuntimeSummary":
        from .models import RuntimeSummary, ContainerDiagnosis

        expected_containers = verification.get("expected", [])
        unhealthy_list = verification.get("unhealthy", [])
        missing = verification.get("missing", [])

        summary = RuntimeSummary(
            total_expected=verification.get("expected_count", len(expected_containers)),
            total_running=verification.get("running_count", 0),
        )

        diagnoses: list[ContainerDiagnosis] = []
        unhealthy_set = set(unhealthy_list)

        for name in unhealthy_set:
            diagnoses.append(ContainerDiagnosis(
                name=name,
                category="UNHEALTHY",
                one_line="Läuft, besteht aber den Docker-Healthcheck nicht",
            ))

        dead = missing

        if dead:
            inspections = await self._inspect_batch(dead)
            needs_llm: list[str] = []

            for name in dead:
                insp = inspections.get(name, {})
                if insp.get("oom_killed"):
                    diagnoses.append(ContainerDiagnosis(
                        name=name,
                        exit_code=insp.get("exit_code", 0),
                        oom_killed=True,
                        category="OOM",
                        one_line="OOM-Kill: nicht genug Arbeitsspeicher",
                    ))
                else:
                    needs_llm.append(name)

            if needs_llm:
                batch = needs_llm[:self._MAX_LLM_BATCH]
                overflow = needs_llm[self._MAX_LLM_BATCH:]
                logs = {n: await self._get_logs(n) for n in batch}
                if self._llm:
                    diagnoses.extend(
                        await self._classify_batch(batch, inspections, logs)
                    )
                else:
                    diagnoses.extend([
                        ContainerDiagnosis(name=n, category="UNKNOWN",
                                           one_line="Kein LLM")
                        for n in batch
                    ])
                diagnoses.extend([
                    ContainerDiagnosis(name=n, category="UNKNOWN",
                                       one_line="Diagnose übersprungen (Batch-Limit)")
                    for n in overflow
                ])

        summary.degraded = diagnoses
        return summary

    async def _inspect_batch(self, names: list[str]) -> dict[str, dict]:
        """Alle Container in einem einzigen docker inspect-Aufruf."""
        if not names:
            return {}

        r = await self._docker(["inspect"] + names)

        result: dict[str, dict] = {}
        if r.stdout.strip():
            import json
            try:
                items = json.loads(r.stdout)
                for item in items:
                    name = item.get("Name", "").lstrip("/") or item.get("Id", "")[:12]
                    state = item.get("State", {})
                    result[name] = {
                        "exit_code": state.get("ExitCode", 0),
                        "oom_killed": bool(state.get("OOMKilled", False)),
                        "error": state.get("Error", ""),
                    }
            except Exception:
                pass

        missing_from_batch = [n for n in names if n not in result]
        for name in missing_from_batch:
            single = await self._docker([
                "inspect", "--format",
                "{{.State.ExitCode}}|{{.State.OOMKilled}}|{{.State.Error}}",
                name,
            ])
            if single.success:
                parts = single.stdout.strip().split("|")
                result[name] = {
                    "exit_code": int(parts[0]) if parts and parts[0].lstrip("-").isdigit() else 0,
                    "oom_killed": len(parts) > 1 and parts[1].lower() == "true",
                    "error": parts[2].strip() if len(parts) > 2 else "",
                }
            else:
                result[name] = {}

        return result

    async def _classify_batch(
        self, names: list[str], inspections: dict, logs: dict,
    ) -> list["ContainerDiagnosis"]:
        from .models import ContainerDiagnosis

        blocks = []
        for name in names:
            insp = inspections.get(name, {})
            blocks.append(
                f"Container: {name}\n"
                f"ExitCode: {insp.get('exit_code', '?')}\n"
                f"DockerError: {insp.get('error', '') or ''}\n"
                f"Logs:\n{(logs.get(name, '') or '')[:1500]}"
            )

        cats = " | ".join(self._CATEGORIES)
        prompt = (
            f"Analysiere diese Docker-Container die nach dem Start beendet wurden.\n"
            f"JSON-Array zurückgeben, ein Objekt pro Container:\n"
            f"  name, category (einer von: {cats}), one_line (Deutsch)\n"
            f"Antworte für JEDEN der {len(names)} Container. Kein Markdown.\n\n"
            + "\n---\n".join(blocks)
        )
        try:
            raw, _usage = await self._llm.generate(prompt=prompt, temperature=0)
            clean = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            import json
            items = json.loads(clean)
            result = [
                ContainerDiagnosis(
                    name=i["name"],
                    exit_code=inspections.get(i["name"], {}).get("exit_code", 0),
                    category=i.get("category", "UNKNOWN"),
                    one_line=i.get("one_line", ""),
                )
                for i in items if i.get("name") in names
            ]
            # [P2] LLM hat möglicherweise nicht alle Namen zurückgegeben.
            returned_names = {d.name for d in result}
            for name in names:
                if name not in returned_names:
                    result.append(ContainerDiagnosis(
                        name=name,
                        exit_code=inspections.get(name, {}).get("exit_code", 0),
                        category="UNKNOWN",
                        one_line="Keine LLM-Diagnose erhalten",
                    ))
            return result
        except Exception as e:
            logger.warning("Batch-Klassifikation fehlgeschlagen: %s", e)
            return [ContainerDiagnosis(name=n, category="UNKNOWN",
                                       one_line="Fehler") for n in names]

    async def _get_logs(self, name: str, tail: int = 30) -> str:
        r = await self._docker(["logs", "--tail", str(tail), name])
        return ((r.stderr or "") + "\n" + (r.stdout or "")).strip()

    async def _docker(self, args: list[str]) -> "CommandResult":
        proc = await asyncio.create_subprocess_exec(
            "docker", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            proc.kill()
            return CommandResult(False, "", "timeout", -1)
        return CommandResult(
            success=proc.returncode == 0,
            stdout=out.decode("utf-8", errors="replace"),
            stderr=err.decode("utf-8", errors="replace"),
            exit_code=proc.returncode or 0,
        )
```

### 6.3 — `orchestrator.py`: Nur der deterministische Pfad

**Scope-Klarstellung:** `_deploy_with_repair_loop_v2()` (L789) wird
ausschließlich vom `iac_mode="llm"`-Zweig aufgerufen. Da `iac_mode="llm"`
explizit außerhalb des Scope ist, werden dort **keine** Phase-6-Änderungen
vorgenommen.

**[F1] `validation_result` nach Revert neu berechnen:**

```python
if not validation_result.passed:
    logger.warning("Semantic repair produced an invalid world model; reverting ...")
    world_model = pre_repair_world_model
    # [F1] validation_result muss ebenfalls auf das revertierte Modell
    # zurückgesetzt werden.
    validation_result = self.world_model_validator.validate(
        world_model, catalog=catalog_snapshot
    )
    break
```

**In `initialize()`:**
```python
from .deployer import RuntimeVerifier
self._runtime_verifier = RuntimeVerifier(llm=self.llm)
```

**Warning-Policy (enger gefasst):**

Nur der erfolgreiche/degradierte Ergebnis-Return am Ende des
deterministischen Pfads garantiert `result.warnings`. FAILED-Early-Returns
(L707, L742, L773) werden nicht angefasst.

**[F1] Akkumulation beginnt bei L687 — nach dem Semantic-Repair-Block:**

```python
# L687 — result wird gebaut, validation_result ist jetzt final
result = DeploymentResult(status=DeploymentStatus.PENDING, judge_result=judge_result)
accumulated_warnings: list[str] = []

if validation_result.warnings:
    accumulated_warnings.extend(w.details for w in validation_result.warnings)

# Nach compiler.compile() (nach L704):
accumulated_warnings.extend(str(w) for w in compiler.get_warnings())

# Port-Remap (L712):
if remapped_ports:
    remap_msg = (
        "Host-Port-Remaps vor dem Render: "
        + ", ".join(f"{name}:{old}→{new}" for name, old, new in remapped_ports[:20])
    )
    logger.warning(remap_msg)
    accumulated_warnings.append(remap_msg)

if self.config.enable_registry_resolution:
    ...
    if soft_unresolved:
        soft_msg = (
            "Weiche Image-Resolution-Fehler (Deployment fortgesetzt): "
            + ", ".join(f"{e.image_ref}:{e.status.value}" for e in soft_unresolved[:10])
        )
        logger.warning(soft_msg)
        accumulated_warnings.append(soft_msg)
```

**Ergebnis-Bau:**

```python
verification = await self.deployer.verify_deployment(container_names) \
    if self.deployer else {}
runtime_summary = await self._runtime_verifier.verify(verification)

if runtime_summary.is_total_failure:
    status = DeploymentStatus.FAILED
elif runtime_summary.is_healthy and verification.get("success"):
    status = DeploymentStatus.DEPLOYED
else:
    status = DeploymentStatus.RUNTIME_DEGRADED

result.status = status
result.runtime_summary = runtime_summary
result.running_containers = runtime_summary.total_running
result.expected_containers = runtime_summary.total_expected
result.warnings = accumulated_warnings
result.summary = runtime_summary.human_summary()
return result
```

### 6.4 — Ausgabe-Stellen

**`cli.py` — vollständiges Muster:**
```python
if result.fully_healthy:
    print(f"\nOK: Deployment vollständig erfolgreich")
    print(f"  Container: {result.running_containers}/{result.expected_containers}")
elif result.success:
    rs = result.runtime_summary
    print(f"\nWARN: Apply erfolgreich — Runtime degradiert")
    print(f"  Container: {rs.total_running}/{rs.total_expected} laufen")
    for category, names_in_cat in rs.failure_groups.items():
        print(f"  {len(names_in_cat)}x {category}: {', '.join(names_in_cat)}")
else:
    print(f"\nFAIL: Deployment fehlgeschlagen")
    for error in result.errors[:5]:
        print(f"  - {error}")

# [P2] Warnings in ALLEN Pfaden anzeigen
if result.warnings:
    print("\nWarnungen:")
    for w in result.warnings[:10]:
        print(f"  ! {w}")

return 0 if result.success else 1
```

---

## Phase 6b — Serialisierung neuer Modellfelder

`to_dict()` auf `SystemDeploy`, `DeployContainer`, `Port` und `DeployProjection`
muss alle neuen Felder einschließen.

### 6b.1 — `models.py`: `SystemDeploy.to_dict()`

```python
if self.vulnerability_profile:
    result["vulnerability_profile"] = self.vulnerability_profile
```

### 6b.2 — `models.py`: `Port.to_dict()`

```python
def to_dict(self) -> dict:
    d = {"internal": self.internal, "external": self.external,
         "protocol": self.protocol}
    if self.ip is not None:
        d["ip"] = self.ip
    return d
```

### 6b.3 — `models.py`: `DeployContainer.to_dict()`

**[G10-KORREKTUR]** `models.py#L870` hat `[{"internal": p.internal, "external":
p.external} for p in self.ports]` — `protocol` fehlt bereits, `ip` würde
ebenfalls fehlen. Selbst wenn `Port.to_dict()` (Phase 6b.2) korrekt serialisiert,
schneidet diese Stelle alle Felder ab. Die Zeile muss auf `p.to_dict()` umgestellt
werden, damit protocol und ip im JSON landen.

```python
# [G10-FIX] Inline-Dict ersetzen — uses Port.to_dict() statt hartkodierter Felder.
# Bisherige Zeile: [{"internal": p.internal, "external": p.external} for p in self.ports]
# Neue Zeile:
result["ports"] = [p.to_dict() for p in self.ports]

# Neue Felder ergänzen:
if self.archetype:
    result["archetype"] = self.archetype
if self.network_aliases:
    result["network_aliases"] = self.network_aliases
if self.hostname:
    result["hostname"] = self.hostname
if self.healthcheck:
    result["healthcheck"] = self.healthcheck
if self.wait:
    result["wait"] = self.wait
    result["wait_timeout"] = self.wait_timeout
if self.upload_files:
    result["upload_files"] = self.upload_files
if self.network_mode:
    result["network_mode"] = self.network_mode
```

### 6b.4 — `models.py`: `DeployProjection.to_dict()`

```python
if self.backbone_network_name:
    result["backbone_network_name"] = self.backbone_network_name
```

### 6b.5 — [G6] `catalog/models.py`: Katalog-Serialisierung für neue Typen

**[G6-KORREKTUR]** Phase 6b adressierte bisher nur `DeployContainer`-Felder.
Die neuen Katalog-Typen aus Phasen 7–16 haben keine `to_dict()`/`from_dict()`-
Implementierung. Ohne diese driften Katalog-Snapshots (persistiert in
`CatalogSnapshot`) von der Runtime-Darstellung ab.

Jeder neue Typ in `catalog/models.py` braucht serialisierbare Felder
in `ImageCatalogEntry.to_dict()` / `from_dict()`:

```python
# In ImageCatalogEntry.to_dict():
if self.log_paths:
    result["log_paths"] = list(self.log_paths)
if self.honeytoken_templates:
    result["honeytoken_templates"] = [
        {"type": t.type, "path": t.path, "template": t.template}
        for t in self.honeytoken_templates
    ]
if self.vulnerability_profiles:
    result["vulnerability_profiles"] = [
        {
            "name": p.name,
            "description": p.description,
            "env_overrides": dict(p.env_overrides),
            "expose_ports_externally": p.expose_ports_externally,
        }
        for p in self.vulnerability_profiles
    ]
if self.traffic_profile:
    tp = self.traffic_profile
    result["traffic_profile"] = {
        "type": tp.type,
        "interval_seconds": tp.interval_seconds,
        "commands": list(tp.commands),
        "generator_image": tp.generator_image,
        "generator_user": tp.generator_user,
        "generator_password": tp.generator_password,
        "generator_env_overrides": dict(tp.generator_env_overrides),
    }
if self.external_traffic_profile:
    etp = self.external_traffic_profile
    result["external_traffic_profile"] = {
        "type": etp.type,
        "generator_image": etp.generator_image,
        "interval_seconds": etp.interval_seconds,
        "commands": list(etp.commands),
        "generator_user": etp.generator_user,
        "generator_password": etp.generator_password,
        "generator_env_overrides": dict(etp.generator_env_overrides),
    }
if self.health_contract:
    hc = self.health_contract
    result["health_contract"] = {
        "liveness_test": list(hc.liveness_test),
        "liveness_interval": hc.liveness_interval,
        "liveness_timeout": hc.liveness_timeout,
        "liveness_retries": hc.liveness_retries,
        "readiness_test": list(hc.readiness_test),
        "readiness_interval": hc.readiness_interval,
        "readiness_timeout": hc.readiness_timeout,
        "readiness_retries": hc.readiness_retries,
        "readiness_start_period": hc.readiness_start_period,
    }
```

**[G11] `from_dict()` explizit aktualisieren (L782):**

`from_dict()` baut den Entry manuell auf — neue Felder die nicht explizit
geladen werden, fallen nach einem Roundtrip weg (Builder-Cache → Orchestrator-
Reload). Alle neuen Felder aus den Phasen 7–16 müssen auch hier ergänzt werden:

```python
# In ImageCatalogEntry.from_dict() — nach dem bestehenden entry = cls(...) Block ergänzen:

# Phase 0 — neue Basisfelder
entry.schema_version = data.get("schema_version", "")
entry.network_aliases = list(data.get("network_aliases", []))
entry.required_companion_archetypes = list(data.get("required_companion_archetypes", []))

# Phase 7
entry.log_paths = list(data.get("log_paths", []))

# Phase 9
# [REV32-P2] Ursprünglicher Guard prüft nur "type" in t — t["path"] und
# t["template"] können trotzdem KeyError werfen (kaputter Snapshot, manuell
# editiertes JSON). Alle Pflichtfelder auf .get() umstellen; Einträge ohne
# "path" überspringen (ein Template ohne Zielpfad ist nicht verwendbar).
raw_tokens = data.get("honeytoken_templates", [])
entry.honeytoken_templates = [
    HoneytokenTemplate(
        type=t.get("type", ""),
        path=t.get("path", ""),
        template=t.get("template", ""),
    )
    for t in raw_tokens
    if isinstance(t, dict) and t.get("type") and t.get("path")
]

# Phase 11
raw_tp = data.get("traffic_profile")
if isinstance(raw_tp, dict):
    entry.traffic_profile = TrafficProfile(
        type=raw_tp.get("type", ""),
        interval_seconds=int(raw_tp.get("interval_seconds", 30)),
        commands=list(raw_tp.get("commands", [])),
        generator_image=raw_tp.get("generator_image", ""),
        generator_user=raw_tp.get("generator_user"),
        generator_password=raw_tp.get("generator_password"),
        generator_env_overrides=dict(raw_tp.get("generator_env_overrides", {})),
    )

# Phase 12
# [REV32-P2] p["name"] wirft KeyError bei kaputtem Snapshot.
# Einträge ohne "name" überspringen — ein namenloses Profil ist nicht
# referenzierbar (deploy_compiler.py sucht per Name).
entry.vulnerability_profiles = [
    VulnerabilityProfile(
        name=p.get("name", ""),
        description=p.get("description", ""),
        env_overrides=dict(p.get("env_overrides", {})),
        expose_ports_externally=bool(p.get("expose_ports_externally", False)),
    )
    for p in data.get("vulnerability_profiles", [])
    if isinstance(p, dict) and p.get("name")
]

# Phase 15
raw_hc = data.get("health_contract")
if isinstance(raw_hc, dict):
    entry.health_contract = HealthContract(
        liveness_test=list(raw_hc.get("liveness_test", [])),
        liveness_interval=raw_hc.get("liveness_interval", "30s"),
        liveness_timeout=raw_hc.get("liveness_timeout", "5s"),
        liveness_retries=int(raw_hc.get("liveness_retries", 3)),
        readiness_test=list(raw_hc.get("readiness_test", [])),
        readiness_interval=raw_hc.get("readiness_interval", "10s"),
        readiness_timeout=raw_hc.get("readiness_timeout", "3s"),
        readiness_retries=int(raw_hc.get("readiness_retries", 5)),
        readiness_start_period=raw_hc.get("readiness_start_period", "15s"),
    )

# Phase 16
raw_etp = data.get("external_traffic_profile")
if isinstance(raw_etp, dict):
    entry.external_traffic_profile = ExternalTrafficProfile(
        type=raw_etp.get("type", ""),
        generator_image=raw_etp.get("generator_image", ""),
        interval_seconds=int(raw_etp.get("interval_seconds", 60)),
        commands=list(raw_etp.get("commands", [])),
        generator_user=raw_etp.get("generator_user"),
        generator_password=raw_etp.get("generator_password"),
        generator_env_overrides=dict(raw_etp.get("generator_env_overrides", {})),
    )
```

**Provenance-Listen (`PROVENANCE_LLM_ENRICHED` bei L914) aktualisieren:**

```python
PROVENANCE_LLM_ENRICHED = [
    "required_env",
    "default_env",
    "health_hints",         # bleibt bis Phase 15 abgeschlossen ist
    "health_contract",      # NEU Phase 15
    "network_aliases",      # NEU Phase 0
    "required_companion_archetypes",  # NEU Phase 0
    "log_paths",            # NEU Phase 7
    "honeytoken_templates", # NEU Phase 9
    "traffic_profile",      # NEU Phase 11
    "vulnerability_profiles", # NEU Phase 12
    "external_traffic_profile", # NEU Phase 16
    "capability_description",
    "category",
    "startup_notes",
    "startup_requirement",
    "health_check",
]
```

---

## Phase 7 — Angriffsdaten sammeln (Log-Aggregation)

### 7.1 — `catalog/models.py`: Log-Pfade im Katalog

```python
@dataclass
class ImageCatalogEntry:
    # ... bestehende Felder ...
    log_paths: list[str] = field(default_factory=list)
```

### 7.2 — `tofu_renderer.py`: Bind Mounts plattformunabhängig erkennen

```python
import os

def _render_volume_spec(self, volume_spec: str, volume_resource_names: dict) -> Optional[dict]:
    parts = volume_spec.split(":")
    read_only = parts[-1].strip().lower() == "ro" if len(parts) >= 3 else False
    if read_only:
        parts = parts[:-1]
    if len(parts) < 2:
        return None

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

### 7.3 — `models.py` + `deploy_compiler.py`: `archetype`-Feld in `DeployContainer`

```python
@dataclass
class DeployContainer:
    # ... bestehende Felder ...
    archetype: str = ""
```

```python
# deploy_compiler.py — _phase4_generate_ir()
containers.append(DeployContainer(
    name=c.get("stable_name", c["name"]),
    image=c["image"],
    archetype=c.get("archetype", ""),   # ← NEU
    ...
))
```

### 7.4 — `orchestrator.py`: Session-Log-Root, `_attach_log_mounts`, Determinismus

```python
def _container_safe_name(name: str) -> str:
    import re
    return re.sub(r"[^a-z0-9_]", "_", name.lower())

# [REV40-P2 / REV41-P2] Slug-Allokation für Log-Verzeichnisse.
#
# Problem: _container_safe_name() ist nicht injektiv — verschiedene Namen
# können auf denselben Slug abbilden (z.B. "nginx-1" und "nginx_1" → "nginx_1",
# oder "filebeat-out" → "filebeat_out"). Für die Beweissicherung müssen
# Log-Verzeichnisse eindeutig per Container sein.
#
# Lösung: kurzer SHA-256-Hash des Originalnamens als Suffix — deterministisch,
# kollisionsfrei (mit vernachlässigbarer Wahrscheinlichkeit), stabil über Runs.
# Ein fester "_hn"-Suffix reicht nicht: "filebeat-out" und "filebeat_out_hn"
# würden beide auf "filebeat_out_hn" abbilden.
#
# Zusätzlich: Output-Pfad von Filebeat liegt unter /usr/share/filebeat/output/
# (NICHT unter /usr/share/filebeat/logs/). Damit kann kein Fallback-Pattern
# auf /usr/share/filebeat/logs/**/* den Output miterfassen — P1 ist
# architektonisch ausgeschlossen, unabhängig von Slug-Logik.
import hashlib as _hashlib

def _log_safe_name(container_name: str) -> str:
    """Deterministisch eindeutiger Slug für das Log-Verzeichnis eines Containers.

    Hängt einen 8-Zeichen SHA-256-Präfix an falls der normalisierte Name
    zu einer Kollision mit dem reservierten Filebeat-Output-Namespace führen
    könnte, oder allgemein als Suffix für volle Eindeutigkeit.

    Beide Funktionen (_attach_log_mounts, _render_filebeat_config) MÜSSEN
    diesen Helper verwenden — nie _container_safe_name() direkt.
    """
    slug = _container_safe_name(container_name)
    h = _hashlib.sha256(container_name.encode()).hexdigest()[:8]
    return f"{slug}_{h}"
```

```python
def _attach_log_mounts(
    containers: list["DeployContainer"],
    catalog: Optional[CatalogSnapshot],
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
        # [REV40-P2] _log_safe_name statt _container_safe_name —
        # reserviert "filebeat_out" als Namespace, verhindert Host-Pfad-Kollision.
        safe_name = _log_safe_name(container.name)
        log_dirs: set[str] = set()
        for log_path in entry.log_paths:
            log_dirs.add(os.path.dirname(log_path))
        for container_log_dir in sorted(log_dirs):
            dir_slug = re.sub(r"[^a-z0-9_]", "_", container_log_dir.strip("/"))
            host_dir = f"{session_log_root}/{safe_name}/{dir_slug}"
            container.volumes.append(f"{host_dir}:{container_log_dir}")
```

### 7.5 — `models.py`: `DeployProjection` um `backbone_network_name` erweitern

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

### 7.6 — `models.py` + `orchestrator.py`: `OrchestratorConfig` + Filebeat-Injektion

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

```python
def _inject_log_aggregator(
    projection: DeployProjection,
    config: OrchestratorConfig,
    session_log_root: str,
    catalog: Optional["CatalogSnapshot"] = None,  # NEU — für katalogbasierte Pfade
) -> None:
    if not config.log_aggregator_image:
        raise ValueError(
            "log_aggregator_image must be set in OrchestratorConfig "
            "when enable_log_aggregation=True"
        )
    # [REV38-P2] catalog durchreichen, damit _render_filebeat_config
    # katalogisierte Pfade statt pauschalem **/*.log verwenden kann.
    filebeat_yml = _render_filebeat_config(projection.containers, config, catalog=catalog)
    backbone = projection.backbone_network_name

    # [REV38-P2 / REV42-P1] Zwei strikt getrennte Mount-Bäume auf dem Host:
    # - session_log_root          → /usr/share/filebeat/logs  :ro
    #   Enthält ausschließlich Service-Log-Verzeichnisse (angelegt von
    #   _attach_log_mounts). Kein filebeat_out-Unterordner — dieser liegt
    #   auf dem Host AUSSERHALB von session_log_root (Geschwisterverzeichnis).
    # - session_log_root + "_out" → /usr/share/filebeat/output :rw
    #   Filebeat schreibt honeynet.ndjson hierhin. Da der Host-Pfad kein
    #   Unterordner von session_log_root ist, kann der Input-Mount
    #   /usr/share/filebeat/logs/**/* ihn physisch nicht erreichen.
    #   Self-Ingestion ist damit architektonisch ausgeschlossen.
    #
    # [REV41-P1-KORREKTUR] Vorherige Versionen legten filebeat_out als
    # Unterordner von session_log_root an. Docker mountet den gesamten
    # session_log_root einschließlich aller Unterordner; filebeat_out war
    # damit weiterhin unter /usr/share/filebeat/logs/filebeat_out sichtbar.
    import os
    filebeat_out_host = session_log_root.rstrip("/").rstrip(os.sep) + "_out"
    projection.containers.append(DeployContainer(
        name="hn-log-aggregator",
        image=config.log_aggregator_image,
        networks=[backbone] if backbone else [],
        volumes=[
            f"{session_log_root}:/usr/share/filebeat/logs:ro",
            f"{filebeat_out_host}:/usr/share/filebeat/output",  # :rw, Geschwister-Host-Pfad
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

### 7.8 — `orchestrator.py`: `_render_filebeat_config()`

```python
def _render_filebeat_config(
    containers: list["DeployContainer"],
    config: "OrchestratorConfig",
    catalog: Optional["CatalogSnapshot"] = None,  # NEU — für katalogbasierte Pfade
) -> str:
    import os, re as _re
    log_paths: list[str] = []
    for c in containers:
        if c.name == "hn-log-aggregator":
            continue
        # [REV40-P2] _log_safe_name statt _container_safe_name — identische
        # Slug-Logik wie _attach_log_mounts, inkl. "filebeat_out"-Schutz.
        # _attach_log_mounts legt Volumes unter session_log_root/{slug}/...
        # ab; hier wird derselbe Slug gesucht. Konsistenz ist zwingend.
        safe = _log_safe_name(c.name)
        # [G5-FIX] Explizite Erkennung: nur Container einschließen die tatsächlich
        # ein Log-Volume haben (Volume-Pfad enthält safe_name als Pfadsegment).
        # [REV32-P1] drive-letter-aware Logik wie in Phase 7.2.
        def _host_path_from_vol(vol: str) -> str:
            parts = vol.split(":")
            if len(parts) >= 3 and len(parts[0]) == 1 and parts[0].isalpha():
                return parts[0] + ":" + parts[1]  # Windows: C:\path\to\dir
            return parts[0]

        has_log_mount = any(
            safe in _re.split(r"[/\\]", _host_path_from_vol(vol))
            for vol in c.volumes
            if ":" in vol
        )
        if not has_log_mount:
            continue

        # [REV38-P2] Katalogisierte log_paths verwenden, wenn verfügbar.
        # Pauschal **/*.log würde Logs ohne .log-Suffix verpassen.
        # Fallback auf **/* wenn kein Katalog-Entry vorhanden.
        # Self-Ingestion ist durch _log_safe_name() strukturell ausgeschlossen:
        # kein Container-Slug kann "filebeat_out" sein → Fallback-Pfade können
        # nie auf /usr/share/filebeat/logs/filebeat_out/ zeigen.
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
            # Fallback: alle Dateien im gemounteten Log-Verzeichnis dieses Containers.
            log_paths.append(f"/usr/share/filebeat/logs/{safe}/**/*")

    # [REV42-P1] Der Fallback /usr/share/filebeat/logs/**/* greift wenn
    # log_paths leer ist (kein Katalog, keine Service-Mounts erkannt).
    # Self-Ingestion ist ausgeschlossen weil der Output-Host-Pfad ein
    # Geschwisterverzeichnis von session_log_root ist (nicht darunter).
    # Docker mountet session_log_root nach /usr/share/filebeat/logs; der
    # Output-Host-Pfad (session_log_root + "_out") ist KEIN Unterordner
    # davon und daher physisch nicht unter /usr/share/filebeat/logs/ sichtbar.
    #
    # [REV42-P3] catalog=None → log_paths bleibt leer → Fallback feuert,
    # aber es gibt keine Service-Log-Mounts (→ _attach_log_mounts bricht ab).
    # Filebeat scannt einen leeren /usr/share/filebeat/logs/-Baum.
    # Das ist kein Fehler — es ist "kein Log-Capture"-Modus, dokumentiert in
    # Phase 7 Scope-Abgrenzung. Der Fallback-Pfad ist harmlos (leeres Ergebnis).
    paths_yaml = "\n".join(f"    - {p}" for p in log_paths) if log_paths \
        else "    - /usr/share/filebeat/logs/**/*"

    if config.log_aggregation_output == "elasticsearch" and config.log_aggregation_host:
        output_section = f"""
output.elasticsearch:
  hosts: ["{config.log_aggregation_host}"]
  index: "honeynet-%{{+yyyy.MM.dd}}"
"""
    else:
        # [REV41-P1] Output-Pfad zeigt auf /usr/share/filebeat/output —
        # separater Mount-Baum, nicht unter /usr/share/filebeat/logs/.
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

In `_deploy_deterministic_v2()`:

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
    # [REV38-P2] catalog_snapshot durchreichen — für katalogbasierte Filebeat-Pfade.
    _inject_log_aggregator(
        deploy_projection_for_render, self.config, session_log_root,
        catalog=catalog_snapshot,
    )

tofu_renderer.render(deploy_projection_for_render, ...)
result.deploy_projection = deploy_projection   # saubere IR
```

### 7.7 — `models.py` + `tofu_renderer.py`: `upload`-Block

```python
@dataclass
class DeployContainer:
    # ... bestehende Felder ...
    upload_files: list[dict] = field(default_factory=list)
```

```python
# tofu_renderer.py
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

---

## Phase 8 — Honeywall: Netzwerk-Isolation

### 8.1 — `deploy_compiler.py`: Backbone standardmäßig internal

Das Backbone-Netzwerk wird bereits mit `internal: True` angelegt (Phase 4.2).
Das ist die vollständige Honeywall-Implementierung — keine zusätzlichen Felder.

---

## Phase 9 — Honeytokens: Köder in den Services

### 9.1 — `catalog/models.py`: Honeytoken-Templates

```python
@dataclass
class HoneytokenTemplate:
    type: str      # "file" | "env" | "db_init_script"
    path: str
    template: str

@dataclass
class ImageCatalogEntry:
    # ... bestehende Felder ...
    honeytoken_templates: list[HoneytokenTemplate] = field(default_factory=list)
```

### 9.2 — `enricher.py`: LLM-Prompt erweitern

```
- honeytoken_templates: Lure assets to place inside this service.
  Templates support: {{random_password}}, {{random_hex_16}}, {{random_upper_20}}.
```

### 9.3 — `deploy_compiler.py`: Honeytokens als `upload`-Blöcke

```python
import secrets, string

def _render_honeytoken_template(template: str) -> str:
    pw = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(16))
    hex16 = secrets.token_hex(8)
    upper20 = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(20))
    return (template
        .replace("{{random_password}}", pw)
        .replace("{{random_hex_16}}", hex16)
        .replace("{{random_upper_20}}", upper20))

upload_files = []
env_extras = []
if entry and getattr(entry, "honeytoken_templates", None):
    # [G7-FIX] Pfad-Deduplizierung für Datei-Templates: mehrere Templates auf
    # demselben Pfad würden sich gegenseitig überschreiben. Letztes Template
    # gewinnt (reversed-Iteration + insert(0, ...) = letztes Vorkommen in
    # Originalreihenfolge gewinnt).
    # [REV32-P3] Dieselbe Policy auch für env-Templates: t.path ist der
    # Env-Key (z.B. "SECRET_KEY"). Zwei Templates mit demselben Env-Key
    # erzeugten bisher doppelte KEY=value-Einträge — reihenfolgeabhängiges
    # Verhalten in os.environ / Docker --env. Deduplizierung via seen_env_keys
    # mit identischer "letztes gewinnt"-Policy wie bei Dateien.
    seen_paths: set[str] = set()
    seen_env_keys: set[str] = set()
    for t in reversed(entry.honeytoken_templates):
        rendered = _render_honeytoken_template(t.template)
        if t.type in ("file", "db_init_script"):
            if t.path not in seen_paths:
                seen_paths.add(t.path)
                upload_files.insert(0, {
                    "content":    rendered,
                    "file":       t.path,
                    "executable": False,
                })
        elif t.type == "env":
            if t.path not in seen_env_keys:
                seen_env_keys.add(t.path)
                env_extras.insert(0, f"{t.path}={rendered}")
```

---

## Phase 10 — Beweissicherung: kryptographische Log-Signierung

### 10.1 — `models.py`: `EvidenceBundle` + Feld in `DeploymentResult`

```python
@dataclass
class EvidenceBundle:
    session_id: str
    log_root: str
    started_at: str
    ended_at: Optional[str]
    log_archive_path: str
    sha256_manifest: str
    bundle_signature: str

@dataclass
class DeploymentResult:
    # ... bestehende Felder ...
    evidence_bundle: Optional["EvidenceBundle"] = None
```

### 10.2 — `deployer.py`: `EvidenceSigner`

**[G2-KORREKTUR]** `create_bundle(session_id)` war in früheren Revisionen
definiert, wird aber nirgendwo aufgerufen — `export_evidence()` ruft
`finalize_bundle(bundle)` auf und der Deploy-Stub in 10.3 erstellt das
EvidenceBundle direkt. `create_bundle` wird entfernt. Die einzige öffentliche
Methode ist `finalize_bundle`.

```python
class EvidenceSigner:
    def __init__(self, log_base: str, evidence_dir: str, output_base: Optional[str] = None):
        self._log_base = log_base
        # [REV42-P1] output_base ist der Host-Pfad des Filebeat-Output-Verzeichnisses
        # (= session_log_root + "_out"). Liegt außerhalb von log_base → muss
        # explizit übergeben werden, damit finalize_bundle beide Bäume archiviert.
        # None = kein separater Output-Baum (z.B. Elasticsearch-Output-Modus).
        self._output_base = output_base
        self._evidence_dir = evidence_dir

    def finalize_bundle(self, bundle: "EvidenceBundle") -> "EvidenceBundle":
        """Erstellt ein signiertes Archiv aller Logs der Session."""
        import hashlib, tarfile, json, datetime, os
        from pathlib import Path

        os.makedirs(self._evidence_dir, exist_ok=True)
        archive_path = os.path.join(self._evidence_dir, f"{bundle.session_id}.tar.gz")
        manifest: dict[str, str] = {}

        def _add_tree(tar: tarfile.TarFile, root: Path, prefix: str) -> None:
            """Alle Dateien unter root ins Archiv aufnehmen, relativ zu root."""
            for log_file in sorted(root.rglob("*")):
                if not log_file.is_file():
                    continue
                arcname = prefix + "/" + str(log_file.relative_to(root))
                tar.add(log_file, arcname=arcname)
                manifest[arcname] = hashlib.sha256(log_file.read_bytes()).hexdigest()

        # [REV42-P1] Beide Host-Bäume archivieren:
        # - "logs/" → Service-Logs (session_log_root)
        # - "output/" → Filebeat-Output (session_log_root + "_out"), falls vorhanden
        with tarfile.open(archive_path, "w:gz") as tar:
            _add_tree(tar, Path(self._log_base), "logs")
            if self._output_base and Path(self._output_base).exists():
                _add_tree(tar, Path(self._output_base), "output")

        manifest_json = json.dumps(manifest, sort_keys=True)
        return EvidenceBundle(
            session_id=bundle.session_id,
            log_root=bundle.log_root,
            started_at=bundle.started_at,
            ended_at=datetime.datetime.utcnow().isoformat(),
            log_archive_path=archive_path,
            sha256_manifest=manifest_json,
            bundle_signature=hashlib.sha256(manifest_json.encode()).hexdigest(),
        )
```

### 10.3 — `orchestrator.py`: Session-Start und expliziter Export

```python
if self.config.enable_log_aggregation and result.success:
    result.evidence_bundle = EvidenceBundle(
        session_id=session_id,
        log_root=session_log_root,
        started_at=datetime.datetime.utcnow().isoformat(),
        ended_at=None,
        log_archive_path="",
        sha256_manifest="",
        bundle_signature="",
    )
```

```python
async def export_evidence(self, bundle: EvidenceBundle) -> EvidenceBundle:
    evidence_dir = self.config.evidence_dir or str(self.config.work_dir / "evidence")
    # [REV43-P1] output_base durchreichen — REV42 fügte EvidenceSigner.output_base
    # hinzu, aber export_evidence() übergab None, sodass honeynet.ndjson nie
    # archiviert wurde. Der Output-Host-Pfad ist session_log_root + "_out"
    # (REV42-Konvention: Geschwisterverzeichnis außerhalb des Input-Baums).
    output_base = bundle.log_root.rstrip("/").rstrip("\\") + "_out"
    signer = EvidenceSigner(
        log_base=bundle.log_root,
        evidence_dir=evidence_dir,
        output_base=output_base,
    )
    return signer.finalize_bundle(bundle)
```

---

## Phase 11 — Realistischer Hintergrund-Traffic

### 11.1 — `catalog/models.py`: Traffic-Profile im Katalog

```python
@dataclass
class TrafficProfile:
    type: str          # "http" | "postgres" | "redis" | "dns"
    interval_seconds: int = 30
    commands: list[str] = field(default_factory=list)
    generator_image: str = ""
    generator_user: Optional[str] = None
    generator_password: Optional[str] = None
    generator_env_overrides: dict[str, str] = field(default_factory=dict)

@dataclass
class ImageCatalogEntry:
    # ... bestehende Felder ...
    traffic_profile: Optional[TrafficProfile] = None
```

### 11.2 — `enricher.py`: LLM-Prompt für Traffic-Profile

```
- traffic_profile: Realistic background activity for this service.
  type must be one of: "http", "postgres", "redis", "dns"
  For postgres: set generator_password to match POSTGRES_PASSWORD in the
    service config. The compiler automatically mirrors generator_password
    to PGPASSWORD in the generator container's environment.
    Do NOT set POSTGRES_HOST_AUTH_METHOD in generator_env_overrides.
```

### 11.4 — `deploy_compiler.py`: Traffic-Generator

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

---

## Phase 12 — Angriffspunkte: dynamische Schwachstellen-Konfiguration

### 12.1 — `catalog/models.py`: Schwachstellen-Profile

```python
@dataclass
class VulnerabilityProfile:
    name: str
    description: str
    env_overrides: dict[str, str] = field(default_factory=dict)
    expose_ports_externally: bool = False

@dataclass
class ImageCatalogEntry:
    # ... bestehende Felder ...
    vulnerability_profiles: list[VulnerabilityProfile] = field(default_factory=list)
```

### 12.3 — `models.py`: Gewähltes Profil im WorldModel

```python
@dataclass
class SystemDeploy:
    # ... bestehende Felder ...
    vulnerability_profile: Optional[str] = None
```

### 12.3b — `deploy_compiler.py`: `_phase1_extract`

```python
container = {
    ...
    "vulnerability_profile": getattr(deploy, "vulnerability_profile", None),  # ← NEU
}
```

### 12.4 — `models.py`: `Port` um `ip`-Feld erweitern

```python
@dataclass
class Port:
    internal: int
    external: int
    protocol: str = "tcp"
    ip: Optional[str] = None
```

### 12.5 — `deploy_compiler.py`: Profil anwenden

**[G9] `_should_publish_host_ports()` ist eine Governance-Gating-Funktion
(L578) die VOR der Port-Erstellung läuft. Sie gibt für interne Zonen immer
`False` zurück und verwirft den Port per `continue` (L783), unabhängig von
`Port.ip`. `expose_ports_externally=True` muss diese Sperre explizit
übersteuern — sonst ist das Feature wirkungslos und Phase 16 findet keinen
externen Port.**

**[G12] Der bestehende Port-Loop (L764–774) behandelt nur `int` und `dict`,
nicht `Port`-Objekte (`else: continue`). Phase 12.5 darf deshalb NICHT
`Port`-Objekte in `c["ports"]` setzen — Format bleibt `dict` mit optionalem
`"ip"`-Schlüssel.**

```python
vuln_profile_name = c.get("vulnerability_profile") or ""
active_profile = None
if entry and vuln_profile_name and getattr(entry, "vulnerability_profiles", None):
    active_profile = next(
        (p for p in entry.vulnerability_profiles if p.name == vuln_profile_name),
        None,
    )

# expose_externally-Flag früh setzen — wird vom Port-Loop in 12.5b gebraucht
expose_externally = bool(active_profile and active_profile.expose_ports_externally)

if active_profile:
    for key, value in active_profile.env_overrides.items():
        env = [e for e in c.get("env", []) if not e.startswith(f"{key}=")]
        env.append(f"{key}={value}")
        c["env"] = env

    # [G12-FIX] Ports als dicts mit "ip"-Schlüssel setzen, NICHT als Port-Objekte.
    # Der bestehende Port-Loop (L764) kennt nur int und dict.
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

### 12.5b — `deploy_compiler.py`: Port-Loop erweitern + Gate übersteuern

Der bestehende Port-Loop in `_phase4_generate_ir()` (L764–791) muss für
Phase 12 erweitert werden. Dieser Snippet ersetzt den bestehenden Port-Loop:

```python
# expose_externally kommt aus Phase 12.5 (oben im Container-Loop)
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
    # Vulnerability-Profile exponieren Ports absichtlich außerhalb der
    # normalen Governance-Regeln (interne Zone / Proxy-Gating).
    if not publish_host_ports and not expose_externally:
        continue

    allocated_external = self._allocate_unique_external_port(
        external_port, used_external_ports
    )
    ports.append(Port(
        internal=internal_port,
        external=allocated_external,
        protocol=protocol,
        ip=port_ip,  # None = kein Override; "0.0.0.0" = intentional exposure
    ))
```

### 12.6 — `tofu_renderer.py`: `ip`-Feld nur schreiben wenn gesetzt

```python
port_block = {
    "internal": port.internal,
    "external": port.external,
    "protocol": port.protocol,
}
if port.ip is not None:
    port_block["ip"] = port.ip
```

### 12.6 — `WorldModelValidator`: Profil-Auflösung + Warnung

**[REV34-P2] Roher String-Check reicht nicht.** `system.deploy.vulnerability_profile`
kann einen Tippfehler enthalten. Ein unbekannter Name soll als `VULN_PROFILE_UNKNOWN`-
Fehler landen, nicht als `HONEYNET_VULNERABILITY_ACTIVE`-Warning. Auflösung via
Katalog — dieselbe Logik wie im Compiler (Phase 12.5, `next(..., None)`-Lookup).
`catalog` wird bereits als Parameter an `validate()` übergeben und muss hier
durchgereicht werden.

```python
vuln_name = system.deploy.vulnerability_profile
if vuln_name:
    archetype = getattr(deploy, "archetype", None) or ""

    # Einmaliger Katalog-Lookup — Ergebnis wird für beide Entscheidungen genutzt.
    entry = None
    active_vuln = None
    if catalog and archetype:
        entry = get_catalog_entry_for_archetype(archetype, catalog)
        if entry:
            active_vuln = next(
                (p for p in (getattr(entry, "vulnerability_profiles", None) or [])
                 if p.name == vuln_name),
                None,
            )

    # Vier klar getrennte Fälle:
    #
    # 1. kein Katalog → gutgläubig warnen (keine Auflösung möglich)
    # 2. Katalog vorhanden, Entry nicht gefunden → Archetyp unbekannt/nicht
    #    kanonisiert; POLICY_ARCHETYPE spricht bereits — hier schweigen.
    #    [REV36-P2] Die else-Branch der Vorgängerversion feuerte HONEYNET_-
    #    VULNERABILITY_ACTIVE auch in diesem Fall, obwohl der Kommentar
    #    "nur Archetyp-Validator spricht" sagte. Jetzt explizit ausgeschlossen.
    # 3. Entry vorhanden, Profil darin nicht gefunden → Tippfehler → Error.
    # 4. Profil aufgelöst → Warning.
    if not catalog:
        # Fall 1: kein Katalog — gutgläubig warnen.
        warnings.append(ValidationError(
            rule="HONEYNET_VULNERABILITY_ACTIVE",
            details=(
                f"System '{sys_name}' hat aktives Schwachstellenprofil "
                f"'{vuln_name}'. "
                f"Nur in isolierten Honeynet-Umgebungen deployen."
            ),
            severity="WARNING",
            fix_hint="Stelle sicher dass das Deployment-Netzwerk isoliert ist (Honeywall).",
        ))
    elif entry is None:
        pass  # Fall 2: Archetyp unbekannt — Archetyp-Validator spricht allein.
    elif active_vuln is None:
        # Fall 3: Entry gefunden, Profil darin nicht → Tippfehler im Namen.
        errors.append(ValidationError(
            rule="VULN_PROFILE_UNKNOWN",
            details=(
                f"System '{sys_name}' referenziert vulnerability_profile "
                f"'{vuln_name}', das für archetype '{archetype}' im Katalog "
                f"nicht gefunden wurde."
            ),
            severity="ERROR",
            fix_hint=(
                f"Tippfehler im Profilnamen prüfen oder Katalog-Eintrag für "
                f"'{archetype}' um das Profil ergänzen."
            ),
        ))
    else:
        # Fall 4: Profil aufgelöst → Warning.
        warnings.append(ValidationError(
            rule="HONEYNET_VULNERABILITY_ACTIVE",
            details=(
                f"System '{sys_name}' hat aktives Schwachstellenprofil "
                f"'{vuln_name}'. "
                f"Nur in isolierten Honeynet-Umgebungen deployen."
            ),
            severity="WARNING",
            fix_hint="Stelle sicher dass das Deployment-Netzwerk isoliert ist (Honeywall).",
        ))
```

---

## Phase 13 — Zweistufiger Repair-Loop

### 13.0 — Scope-Klarstellung

**[G3-KORREKTUR]** Der aktuelle `WorldModelRepairLoop` in `world_model_repair.py`
ist **rule-based** und ignoriert `repair_hint` explizit ("für künftige
LLM-gestützte Erweiterung"). Die IaCGen-Forschungszahlen (20–30% → 54–91%)
gelten ausschließlich für LLM-basierte Repair-Loops. Für den rule-based
Fall bringt Phase 13 **keinen Effekt auf die Erfolgsquote** — der Nutzen
liegt allein in der `_repair_is_making_progress`-Abbruch-Heuristik, die
Endlosschleifen verhindert.

**Außerdem:** `_build_repair_prompt` und `_repair_is_making_progress` gehören
**nicht** in `repair/strategies.py`. Diese Datei enthält bereits ein vollständiges
Terraform-Repair-System (`AdaptiveRepairSelector`, `ReflectiveRepair`,
`SelfConsistencyRepair`, `TreeOfThoughtsRepair`) — das ist ein anderes
Repair-Domain. WorldModel-Repair-Helfer kommen in `world_model_repair.py`.

**[G4-KORREKTUR]** `repair/strategies.py` wird **nicht** angefasst.
Die Helfer-Funktionen kommen direkt in `world_model_repair.py`.

### 13.1 — `world_model_repair.py`: Zweistufige Eskalation

```python
MAX_REPAIR_ATTEMPTS_STAGE_1 = 2
MAX_REPAIR_ATTEMPTS_STAGE_2 = 4


def _build_repair_prompt(
    errors: list["ValidationError"],
    attempt: int,
    full_error_output: Optional[str] = None,
) -> str:
    if attempt <= MAX_REPAIR_ATTEMPTS_STAGE_1:
        summary = "; ".join(
            f"{e.rule}: {e.details[:80]}" for e in errors[:5]
        )
        return f"Fix these validation errors: {summary}"
    else:
        lines = [f"- [{e.rule}] {e.details}" for e in errors]
        prompt = "Fix ALL of the following errors:\n" + "\n".join(lines)
        if full_error_output:
            prompt += f"\n\nFull error output:\n{full_error_output}"
        return prompt


def _repair_is_making_progress(
    prev_error_count: int,
    curr_error_count: int,
    attempt: int,
) -> bool:
    # [REV33-P1] Die Schutzgrenze umfasst nur Stage 1, nicht Stage 1 + Stage 2.
    # Stage 1 läuft bedingungslos durch — hier sammeln wir Informationen.
    # Ab Stage 2 (attempt > MAX_REPAIR_ATTEMPTS_STAGE_1) muss die Fehlerzahl
    # sinken, sonst wird abgebrochen.
    #
    # [REV34-P3] Kommentar-Konsistenz: Beispiele mit konkreten Zahlen (attempt=3,
    # attempt=6) hingen implizit an Konstantenwerten (z.B. MAX_STAGE_1=2 → 3
    # wäre bereits Stage 2, nicht Stage 1 wie behauptet). Beispiele jetzt
    # konstanten-relativ formuliert:
    #
    #   attempt <= MAX_REPAIR_ATTEMPTS_STAGE_1, prev=10, curr=10
    #       → True  (Stage 1, letzter freier Versuch)
    #   attempt = MAX_REPAIR_ATTEMPTS_STAGE_1 + 1, prev=10, curr=5
    #       → True  (erster Stage-2-Versuch, Fehler gesunken)
    #   attempt = MAX_REPAIR_ATTEMPTS_STAGE_1 + 1, prev=10, curr=10
    #       → False (erster Stage-2-Versuch, kein Fortschritt → Abort)
    if attempt <= MAX_REPAIR_ATTEMPTS_STAGE_1:
        return True  # Stage 1: immer weitermachen
    return curr_error_count < prev_error_count  # Stage 2: Fortschritt erforderlich
```

### 13.2 — `world_model_repair.py`: `repair()` um `repair_hint`-Parameter erweitern

```python
def repair(
    self,
    world_model: WorldModel,
    validation_result: "ValidationResult",
    catalog: Optional["CatalogSnapshot"] = None,
    repair_hint: Optional[str] = None,  # ignoriert (rule-based Repair)
) -> WorldModel:
    # ... bestehende Logik unverändert ...
    # repair_hint legt die API für künftige LLM-gestützte Erweiterung fest.
```

### 13.3 — `orchestrator.py`: Zweistufige Eskalation einbinden

**Import ergänzen:**
```python
from .world_model_repair import (
    _build_repair_prompt,
    _repair_is_making_progress,
    MAX_REPAIR_ATTEMPTS_STAGE_1,
    MAX_REPAIR_ATTEMPTS_STAGE_2,
)
```

**Repair-Loop-Block ersetzen (L587–595):**
```python
if self.config.world_model_repair_mode and not validation_result.passed:
    wm_repair = WorldModelRepairLoop()
    prev_error_count = len(validation_result.errors)
    max_attempts = MAX_REPAIR_ATTEMPTS_STAGE_1 + MAX_REPAIR_ATTEMPTS_STAGE_2

    for attempt in range(1, max_attempts + 1):
        if validation_result.passed:
            break
        if not _repair_is_making_progress(prev_error_count,
                                           len(validation_result.errors),
                                           attempt):
            logger.warning(
                "Repair-Loop abgebrochen: kein Fortschritt in Attempt %s "
                "(%s → %s Fehler)", attempt, prev_error_count,
                len(validation_result.errors),
            )
            break

        repair_prompt = _build_repair_prompt(
            validation_result.errors,
            attempt=attempt,
        )
        world_model = wm_repair.repair(
            world_model, validation_result, catalog_snapshot,
            repair_hint=repair_prompt,
        )
        prev_error_count = len(validation_result.errors)
        validation_result = self.world_model_validator.validate(
            world_model, catalog=catalog_snapshot
        )
```

---

## Phase 14 — Security-Compliance-Validator

### 14.1 — `world_model_validator.py`: Zone-Isolation-Prüfung

**[REV34-P2] `_validate_security_policy` prüft jetzt das aufgelöste Profil,
nicht den rohen String.** Ein Tippfehler im Profilnamen löst bereits in Phase
12.6 einen `VULN_PROFILE_UNKNOWN`-Fehler aus. Phase 14.1 soll denselben
Katalog-Lookup verwenden und `POLICY_ZONE_ISOLATION` nur emittieren wenn das
Profil tatsächlich aufgelöst werden konnte — sonst feuert der Fehler auf einem
Name der sowieso schon als unbekannt markiert ist, was irreführend ist.

```python
def _validate_security_policy(
    self,
    wm: WorldModel,
    catalog: Optional[CatalogSnapshot],
) -> tuple[list[ValidationError], list[ValidationError]]:
    errors: list[ValidationError] = []
    warnings: list[ValidationError] = []
    EXTERNAL_EXPOSURES = {"internet", "dmz"}

    for sys_name, system in wm.systems.items():
        if not system.deploy:
            continue
        deploy = system.deploy
        zone_name = getattr(deploy, "zone", "") or ""
        vuln_profile_name = getattr(deploy, "vulnerability_profile", None)

        zone_obj = wm.zones.get(zone_name) if zone_name else None
        exposure = (
            zone_obj.simulate.exposure
            if zone_obj and zone_obj.simulate
            else "internal"
        )

        # [REV34-P2] Nur aufgelöstes Profil für Zone-Isolation-Check verwenden.
        # Roher Name reicht nicht — Tippfehler hätte sonst einen POLICY_ZONE_ISOLATION-
        # Error für ein Profil das gar nicht aktiv ist. VULN_PROFILE_UNKNOWN
        # (Phase 12.6) deckt den Tippfehler-Fall bereits ab.
        active_vuln = None
        if vuln_profile_name and catalog:
            archetype = getattr(deploy, "archetype", None) or ""
            if archetype:
                entry = get_catalog_entry_for_archetype(archetype, catalog)
                if entry:
                    active_vuln = next(
                        (p for p in (getattr(entry, "vulnerability_profiles", None) or [])
                         if p.name == vuln_profile_name),
                        None,
                    )

        if active_vuln and exposure not in EXTERNAL_EXPOSURES:
            errors.append(ValidationError(
                rule="POLICY_ZONE_ISOLATION",
                details=(
                    f"System '{sys_name}' hat vulnerability_profile "
                    f"'{vuln_profile_name}' in Zone '{zone_name}' "
                    f"(exposure='{exposure}'). "
                    f"Externe Port-Exposition in nicht-externer Zone."
                ),
                severity="ERROR",
                fix_hint=(
                    f"Verschiebe '{sys_name}' in eine Zone mit "
                    f"exposure='internet' oder 'dmz', oder entferne "
                    f"das vulnerability_profile."
                ),
            ))

    return errors, warnings
```

### 14.2 — `deploy_compiler.py`: Port-ip-Prüfung auf IR-Ebene

**Reihenfolge in `_phase4_generate_ir()`:** Diese Prüfung muss NACH dem
Port-Loop (Phase 12.5b) stehen, der die finale `ports`-Liste aufbaut.

**[REV31-G13] `c.get("ports", [])` ist die falsche Quelle.** Nach Phase 12.5
ist `c["ports"]` ein dict-basiertes Zwischenformat — `getattr(p, "ip", None)`
liefert bei dicts immer `None`. Die Warning wäre ein no-op genau auf dem
Pfad den Phase 12 eingeführt hat. Korrekte Quelle ist die finale `ports`-Liste
(Port-Objekte, nach `_allocate_unique_external_port`):

```python
# Nach dem Port-Loop aus Phase 12.5b — ports ist die finale IR-Liste
# [REV33-P2] not vuln_profile_name war falsch: ein Tippfehler wie
# vulnerability_profile="defualt_creds" liefert vuln_profile_name != ""
# → Warning wird unterdrückt, obwohl active_profile=None ist und kein
# Profil aktiviert wurde. Korrekte Bedingung: not active_profile.
# active_profile ist nur nicht-None wenn der Name tatsächlich im Katalog
# aufgelöst werden konnte (Phase 12.5, next(..., None)-Lookup).
for p in ports:
    if p.ip == "0.0.0.0" and not active_profile:
        self.warnings.append(CompilerError(
            phase="IR",
            message=(
                f"Container '{c.get('name')}' hat Port {p.external} auf "
                f"0.0.0.0 gebunden ohne aktives vulnerability_profile. "
                f"Unbeabsichtigte externe Exposition?"
            ),
            system=c.get("name", ""),
        ))
```

---

## Phase 15 — Liveness vs. Readiness

### 15.1 — `catalog/models.py`: Zwei Health-Check-Typen

```python
@dataclass
class HealthContract:
    liveness_test: list[str] = field(default_factory=list)
    liveness_interval: str = "30s"
    liveness_timeout: str = "5s"
    liveness_retries: int = 3

    readiness_test: list[str] = field(default_factory=list)
    readiness_interval: str = "10s"
    readiness_timeout: str = "3s"
    readiness_retries: int = 5
    readiness_start_period: str = "15s"

@dataclass
class ImageCatalogEntry:
    # ... bestehende Felder ...
    health_contract: Optional[HealthContract] = None
    # Ersetzt health_hints vollständig — health_hints aus Enricher-Schema entfernen
```

**[G8] Import-Statement für `deploy_compiler.py`:**
```python
from .catalog.models import (
    HealthContract,
    HoneytokenTemplate,      # Phase 9
    VulnerabilityProfile,    # Phase 12
    TrafficProfile,          # Phase 11
    ExternalTrafficProfile,  # Phase 16
)
```

### 15.2 — `deploy_compiler.py`: Phase-1-Snippet durch Phase-15.2-Snippet ersetzen

```python
hc_from_catalog = entry.health_contract if entry and entry.health_contract else None
sys_obj = world_model.systems.get(c["name"])
hc_manual_dict = (
    sys_obj.deploy.health_contract
    if sys_obj and sys_obj.deploy and sys_obj.deploy.health_contract
    else {}
)

if hc_manual_dict:
    hc = HealthContract(**{
        k: v for k, v in hc_manual_dict.items()
        if k in HealthContract.__dataclass_fields__
    })
else:
    hc = hc_from_catalog

healthcheck = None
wait = False

if hc and hc.readiness_test:
    healthcheck = {
        "test":         hc.readiness_test,
        "interval":     hc.readiness_interval,
        "timeout":      hc.readiness_timeout,
        "retries":      hc.readiness_retries,
        "start_period": hc.readiness_start_period,
    }
    wait = True
elif hc and hc.liveness_test:
    healthcheck = {
        "test":     hc.liveness_test,
        "interval": hc.liveness_interval,
        "timeout":  hc.liveness_timeout,
        "retries":  hc.liveness_retries,
    }
    wait = True
```

---

## Phase 16 — Externer Traffic-Simulator

### 16.1 — `catalog/models.py`: Externer Traffic im Profil

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

@dataclass
class ImageCatalogEntry:
    # ... bestehende Felder ...
    external_traffic_profile: Optional[ExternalTrafficProfile] = None
```

### 16.2 — `models.py`: `network_mode` in `DeployContainer`

**[REV31-G15] `network_mode` wurde bereits in Phase 4.1 eingeführt.**
Phase 16 führt das Feld nicht erneut ein — es ist bereits vorhanden.
Phase 16 nutzt es nur für externe Traffic-Generatoren.

**`tofu_renderer.py`:**
```python
# [G5-FIX] Potenzielle Provider-Kollision: der aktuelle Renderer emittiert
# networks_advanced immer, auch wenn network_mode gesetzt ist. Docker-Config
# mit beidem ist ungültig. Guard verhindert das:
if container.network_mode:
    container_block["network_mode"] = container.network_mode
    # networks_advanced NICHT setzen — kombiniert mit network_mode ungültig
else:
    if networks_advanced:
        container_block["networks_advanced"] = networks_advanced
```

### 16.3 — `deploy_compiler.py`: Externer Generator

```python
ext_tp = getattr(entry, "external_traffic_profile", None) if entry else None
vuln_exposes = active_profile and active_profile.expose_ports_externally

if ext_tp and vuln_exposes:
    if not ext_tp.generator_image:
        logger.warning(
            "Container '%s' hat external_traffic_profile ohne generator_image — übersprungen",
            c.get("name"),
        )
    else:
        # [REV31-G14] Zwei Fehler der alten Version:
        # 1. c.get("ports") enthält dicts nach Phase 12.5 — getattr liefert None
        # 2. c["ports"] enthält den angeforderten Port, NICHT den zugewiesenen.
        #    _allocate_unique_external_port() kann den Port remappen.
        #    Der tatsächlich publizierte Port ist nur in der finalen ports-Liste.
        # Korrekte Quelle: ports (IR, Port-Objekte, nach Allokation)
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

## Zeitplan

```
Woche 1 — Phase 0
  catalog/models.py, builder.py, enricher.py
  Danach: force_rebuild=True einmalig

Woche 2 — Phase 2
  catalog_inference.py (NEU: _pep503_normalize, build_archetype_lookup_index,
    normalize_expanded_archetype implementieren — existieren noch nicht!)
  catalog_inference.py (apply_catalog_defaults_to_system erweitern)
  world_model_extractor.py (_apply_catalog_defaults + Expansion)

Woche 3 — Phase 4
  models.py, deploy_compiler.py, tofu_renderer.py (ein Commit)
  models.py: RuntimeSummary/ContainerDiagnosis/RUNTIME_DEGRADED schon hier

Woche 4a — Phase 1
  world_model_validator.py (Companion-Validator + depends_on-Warnung)
  deploy_compiler.py + tofu_renderer.py (health_hints → wait/wait_timeout)

Woche 4b — Phase 6 (parallel)
  deployer.py (RuntimeVerifier), orchestrator.py (nur deterministischer Pfad!)
  cli.py, interactive.py, basic_usage.py

Woche 5 — Phase 6b + Phase 7
  models.py: to_dict() für alle neuen Felder (6b + 6b.5 Katalog)
  catalog/models.py (log_paths), orchestrator.py (_attach_log_mounts, _inject_log_aggregator)
  models.py (upload_files, backbone_network_name, archetype in DeployContainer)

Woche 6 — Phase 8 (Honeywall, bereits durch Phase 4 abgedeckt)
            + Phase 9 (Honeytokens, parallel)
  catalog/models.py (honeytoken_templates), enricher.py, deploy_compiler.py
  Voraussetzung: Phase 7 (upload-Block muss existieren)

Woche 7 — Phase 10 (Beweissicherung) + Phase 13 (Zweistufiger Repair-Loop)
  deployer.py (EvidenceSigner.finalize_bundle — create_bundle entfernt)
  orchestrator.py (export_evidence)
  world_model_repair.py (_build_repair_prompt, _repair_is_making_progress, repair_hint)
  Voraussetzung: Phase 7 (für 10), Phase 2+6 (für 13)

Woche 8 — Phase 11 (Traffic-Generator) + Phase 15 (Liveness/Readiness)
  catalog/models.py (traffic_profile + HealthContract), enricher.py, deploy_compiler.py
  Imports für neue Katalog-Typen in deploy_compiler.py (G8)
  Voraussetzung: Phase 4

Woche 9 — Phase 12 (Angriffspunkte) + Phase 14 (Security-Compliance-Validator)
  catalog/models.py (vulnerability_profiles), enricher.py, deploy_compiler.py
  tofu_renderer.py (Port.ip-Override), world_model_validator.py
  Voraussetzung: Phase 8

Woche 10 — Phase 16 (Externer Traffic)
  catalog/models.py (external_traffic_profile)
  deploy_compiler.py, tofu_renderer.py (network_mode Guard — G5)
  Voraussetzung: Phase 12
```

---

## Vollständige Korrektur-Übersicht

| Runde | Finding | Fix | Datei |
|---|---|---|---|
| REV1 | Cache-Invalidierung | schema_version | builder.py |
| REV1 | Backbone-Architektur explizit | Kommentar | deploy_compiler.py |
| REV1 | Alias nur Einzelinstanz | archetype_counts | deploy_compiler.py |
| REV1 | Kein Instance-State im Extractor | Index als Parameter | world_model_extractor.py |
| REV1 | generate() / self.llm | API-Anpassung | deployer.py, orchestrator.py |
| REV1 | success/fully_healthy | Property | models.py, cli.py, interactive.py |
| REV1 | DNS-Sanitisierung | _sanitize_dns_labels | enricher.py |
| REV2 | unhealthy in RuntimeSummary | UNHEALTHY-Kategorie | deployer.py |
| REV2 | required_companion_archetypes als Liste | Schema-Änderung | catalog/models.py |
| REV2 | Globale Alias-Eindeutigkeit | allocated_aliases | deploy_compiler.py |
| REV2 | basic_usage.py + README | fully_healthy | basic_usage.py |
| REV2 | Provenance-Liste | Felder ergänzen | catalog/models.py L914 |
| REV3-F1 | Normalisierung nur Expansion | apply_catalog_defaults | catalog_inference.py |
| REV3-F2 | Alias-Zählung vor Replikation | Counts in Phase 4 | deploy_compiler.py |
| REV3-F3 | RUNTIME_DEGRADED fehlte | RuntimeVerifier im det. Pfad | orchestrator.py |
| REV3-F4 | unhealthy in det. Pfad | verify + RuntimeVerifier | orchestrator.py |
| REV3-F5 | iac_mode=llm außer Scope | explizit dokumentiert | Plan |
| REV3-F6 | Sanitisierung IR-Grenze | _sanitize_aliases | deploy_compiler.py |
| REV3-O1 | Companion ohne depends_on | WARNING + Mehrfach-Policy | world_model_validator.py |
| REV3-O2 | N+1 Verifikation | _MAX_LLM_BATCH | deployer.py |
| REV4-F1 | Companions nicht kanonisiert | normalize_expanded_archetype im Validator | world_model_validator.py |
| REV4-F2 | depends_on nicht abgeleitet | Klare Scope-Abgrenzung | Plan |
| REV4-F3 | archetype_to_name nicht deterministisch | dict[str, list[str]] | world_model_validator.py |
| REV4-F4 | total_running semantisch falsch | healthy_running Property | models.py, deployer.py |
| REV4-F5 | N+1 nur abgeschwächt | _inspect_batch | deployer.py |
| REV4-F6 | PEP-503-Helfer angeblich fehlend | ✗ Falsch — aber tatsächlich fehlend (REV29-G1) | — |
| REV5-F1 | PEP-503-Helfer erneut gemeldet | ✗ Abermals falsch abgewiesen — tatsächlich fehlend | — |
| REV5-F2 | _inspect_batch all-or-nothing | Fallback auf Einzel-Inspect | deployer.py |
| REV5-F3 | Repair-Loop-Erklärung falsch | Loop läuft MIT Katalog | Plan |
| REV5-F4 | README nicht abgedeckt | README L148 in Migration | README.md |
| REV6–REV28 | (wie bisher dokumentiert) | (wie bisher) | (wie bisher) |
| REV29-G1 | **KRITISCH**: `_pep503_normalize`, `build_archetype_lookup_index`, `normalize_expanded_archetype` existieren NICHT | Vollständige Implementierung in Phase 2.1 — rg-Suche ergibt 0 Treffer im aktuellen Code | catalog_inference.py |
| REV29-G2 | `create_bundle` war toter **geplanter** Code (EvidenceSigner existiert noch nicht im Code) | Aus dem Plan entfernt — einzige geplante öffentliche Methode ist `finalize_bundle` | deployer.py (geplant) |
| REV29-G3 | Phase 13 Prompt-Eskalation ist no-op für rule-based Repair (`WorldModelRepairLoop` hat kein `repair_hint`-Argument) | Scope-Klarstellung in 13.0; belastbarer Nutzen liegt allein in `_repair_is_making_progress` | Plan |
| REV29-G4 | Phase 13: `_build_repair_prompt` + `_repair_is_making_progress` gehörten in die falsche Datei (`repair/strategies.py` ist Terraform-Repair-Domäne) | Nach `world_model_repair.py` verschoben — Domänen bleiben getrennt | world_model_repair.py |
| REV29-G5 | Phase 16: `network_mode="host"` + `networks_advanced` — aktueller Renderer emittiert `networks_advanced` immer, potenzielle Provider-Kollision (kein garantierter Laufzeitfehler, aber kein valides Docker-Config) | Guard in `tofu_renderer.py`: `networks_advanced` nur wenn `network_mode` nicht gesetzt | tofu_renderer.py |
| REV29-G6 | Katalog-Serialisierung für neue Typen (Phasen 7–16) fehlte im Plan | Im Plan adressiert (Phase 6b.5): `to_dict()`/`from_dict()` + PROVENANCE-Listen für alle neuen Felder | catalog/models.py (geplant) |
| REV29-G7 | Phase 9: `upload_files` Pfad-Duplikate nicht behandelt | Im Plan adressiert: Deduplizierung via `seen_paths`-Set in Phase 9.3 | deploy_compiler.py (geplant) |
| REV29-G8 | Fehlende Import-Statements für neue Katalog-Typen im Compiler fehlten im Plan | Im Plan adressiert: konsolidierte Import-Liste in Phase 15.1 für alle neuen Typen | deploy_compiler.py (geplant) |
| REV30-G9 | `_should_publish_host_ports()` (L578) verwirft Ports in internen Zonen VOR Port-Erstellung — `Port.ip="0.0.0.0"` greift nie; Phase 16 findet keinen externen Port | `expose_externally`-Flag; Port-Loop: `if not publish_host_ports and not expose_externally: continue` | deploy_compiler.py |
| REV30-G10 | `DeployContainer.to_dict()` (L870): `[{"internal":..., "external":...}]` — `protocol` und `ip` werden abgeschnitten auch wenn `Port.to_dict()` sie liefert | `result["ports"] = [p.to_dict() for p in self.ports]` in Phase 6b.3 | models.py |
| REV30-G11 | `ImageCatalogEntry.from_dict()` (L782) lädt neue Felder nicht — alle Phase-7–16-Felder verschwinden nach Roundtrip | Vollständige from_dict()-Ergänzung + PROVENANCE_LLM_ENRICHED in Phase 6b.5 | catalog/models.py |
| REV30-G12 | Phase 12.5 setzt `Port`-Objekte in `c["ports"]`; Port-Loop (L764) kennt nur int+dict — Port-Objekte werden durch `else: continue` lautlos verworfen | Port-Override als dicts mit `"ip"`-Schlüssel; Port-Loop erweitert auf Port-Objekte (Phase 12.5b) | deploy_compiler.py |
| REV30-G3-bestätigt | health_contract-Merge-Regel in Phase 15.2: original Plan hatte `hc = entry.health_contract` ohne manuellen Override | Bereits in REV29 korrekt als Merge-Logik mit `hc_manual_dict` implementiert — kein weiterer Fix nötig | deploy_compiler.py |
| REV31-G13 | **Phase 14.2** prüft `c.get("ports", [])` mit `getattr(p, "ip", None)` — nach Phase 12.5 sind das dicts, `getattr` liefert `None`, Warning ist ein no-op | Prüfung auf finale `ports`-Liste (Port-Objekte): `for p in ports: if p.ip == "0.0.0.0"` | deploy_compiler.py |
| REV31-G14 | **Phase 16.3** liest externen Port aus `c.get("ports", [])` — (1) dicts haben kein `ip`-Attribut → `external_port = None`, (2) enthält angeforderten nicht zugewiesenen Port, Remap wird ignoriert | Port aus finaler `ports`-Liste: `next((p.external for p in ports if p.ip == "0.0.0.0"), None)` | deploy_compiler.py |
| REV31-G15 | **Reihenfolge**: Phase 4.3 nutzt `container.network_mode` im Renderer, aber Feld wird erst in Phase 16.2 eingeführt — Phase 4 (Woche 3) nicht standalone ausführbar | `network_mode: Optional[str] = None` in Phase 4.1 eingeführt; Phase 16.2 führt es nicht erneut ein | models.py |
| REV32-P1 | **Phase 7.8**: `vol.split(":")[0]` extrahiert bei Windows-Bind-Mount `C:\...\logs:/var/log` nur `"C"` — `safe in "C".split("/")` matcht nie, Container fehlt in Filebeat-Config; Phase-7.2-Fix (drive-letter-aware) wird an dieser Stelle nicht wiederverwendet | `_host_path_from_vol()` mit identischer drive-letter-Logik; Host-Pfad-Split auf `[/\\]` via `re.split` | orchestrator.py |
| REV32-P2 | **`from_dict()`**: Guard prüft nur `"type" in t` / `isinstance(p, dict)` — `t["path"]`, `t["template"]`, `p["name"]` können weiterhin `KeyError` werfen bei kaputtem Snapshot; Testlücke verspricht defensives Verhalten | Alle Pflichtfelder auf `.get()` umgestellt; Einträge ohne `path` (HoneytokenTemplate) bzw. `name` (VulnerabilityProfile) übersprungen | catalog/models.py |
| REV32-P3 | **Phase 9 env-Honeytokens**: Datei-Templates haben `seen_paths`-Deduplizierung, `env`-Templates werden blind an `env_extras` angehängt — zwei Templates mit gleichem Env-Key erzeugen doppelte `KEY=value`-Einträge | `seen_env_keys`-Set mit identischer "letztes gewinnt"-Policy; `env_extras.insert(0, ...)` statt `append` | deploy_compiler.py |
| REV33-P1 | **Phase 13**: `_repair_is_making_progress` verwendete `attempt <= MAX_STAGE_1 + MAX_STAGE_2` als Schutzgrenze — identisch mit der Schleifengrenze; Abort-Pfad konnte nie feuern | Schutz nur für Stage 1: `attempt <= MAX_REPAIR_ATTEMPTS_STAGE_1`; Stage 2 erfordert `curr < prev` | world_model_repair.py |
| REV33-P2 | **Phase 14.2**: Exposure-Warning unterdrückt mit `not vuln_profile_name` — Tippfehler im Profilnamen (`"defualt_creds"`) gilt als "Profil vorhanden", obwohl `active_profile=None`; Warning feuert nicht | Bedingung auf `not active_profile` geändert — nur aufgelöstes Profil zählt | deploy_compiler.py |
| REV34-P3 | **Phase 13 Kommentar**: Beispiel `attempt=3 → True (Stage 1)` war falsch wenn `MAX_STAGE_1=2`; konkrete Zahlen koppelten implizit an Konstantenwerte | Kommentar auf Konstanten-relative Formulierung umgestellt | world_model_repair.py |
| REV34-P2 | **Phase 12.6 + 14.1**: Validator prüft `system.deploy.vulnerability_profile` (roher String) — Tippfehler erzeugt `HONEYNET_VULNERABILITY_ACTIVE`-Warning (irreführend) oder `POLICY_ZONE_ISOLATION`-Error (fälschlicherweise) ohne aufgelöstes Profil | Phase 12.6: neues `VULN_PROFILE_UNKNOWN`-Error + `HONEYNET_VULNERABILITY_ACTIVE` nur bei aufgelöstem Profil; Phase 14.1: `POLICY_ZONE_ISOLATION` nur bei aufgelöstem Profil | world_model_validator.py |
| REV35-P2 | **Phase 12.6**: `catalog and archetype and active_vuln is None` feuerte auch wenn `get_catalog_entry_for_archetype` keinen Entry liefert → irreführender Profil-Fehler zusätzlich zum Archetyp-Fehler | Guard auf `entry_for_check is not None and active_vuln is None`; bei fehlendem Entry schweigt Phase 12.6 | world_model_validator.py |
| REV35-P3 | **Zeitplan Woche 10**: `network_mode Guard — G4` falsch beschriftet; G4 betrifft `_build_repair_prompt`-Verschiebung, der Guard gehört zu G5 | Korrigiert auf `G5` | UMSETZUNGSPLAN.md |
| REV36-P2 | **Phase 12.6**: `else`-Branch emittierte `HONEYNET_VULNERABILITY_ACTIVE` auch bei unbekanntem Archetyp (Entry=None) — Kommentar und Testlücke sagten "nur Archetyp-Validator spricht", Code tat es nicht | Vier explizite Fälle: kein Katalog→gutgläubig warnen; Entry=None→schweigen; Profil fehlt→`VULN_PROFILE_UNKNOWN`; Profil aufgelöst→`HONEYNET_VULNERABILITY_ACTIVE` | world_model_validator.py |
| REV36-P3 | **Kopf**: `Stand: REV34` veraltet obwohl REV35 bereits in Tabelle und Testlücken | Auf `REV35` aktualisiert | UMSETZUNGSPLAN.md |
| REV37-P3 | **Kopf + Testblock**: `Stand: REV35 … REV33–REV35` und Testüberschrift `REV34-P2, REV35-P2` veraltet obwohl REV36 bereits in Korrektur-Übersicht | Kopf auf `REV36`, Testüberschrift auf `REV34-P2–REV36-P2` aktualisiert | UMSETZUNGSPLAN.md |
| REV38-P2 | **Phase 7/10**: `_render_filebeat_config` verwendete `**/*.log` → verpasst Logs ohne `.log`-Suffix; `finalize_bundle` verwendete `rglob("*.log")` → gleicher Verlust + `honeynet.ndjson` fehlt im Archiv; Filebeat-Output-Dir als `:ro` gemountet → `output.file` konnte nicht schreiben | `_render_filebeat_config` nutzt katalogisierte Pfade + Fallback `**/*`; `finalize_bundle` nutzt `rglob("*")` mit `is_file()`; `_inject_log_aggregator` erhält separaten `:rw`-Mount für `filebeat_out/` | orchestrator.py |
| REV39-P1 | **Phase 7.8**: REV38 fügte `honeynet.ndjson*` als Filebeat-Input-Pfad hinzu — `output.file` schreibt in dasselbe Verzeichnis → Self-Ingestion-Schleife (duplizierte Events, unkontrolliertes Output-Wachstum, verfälschtes Evidence-Bundle) | Zeile entfernt; `honeynet.ndjson` wird ausschließlich von `finalize_bundle()` via `rglob("*")` archiviert; Fallback-Pattern schließt `filebeat_out/` explizit aus | orchestrator.py |
| REV40-P2 | **Phase 7.4/7.8**: `_container_safe_name()` kann `"filebeat_out"` als Slug erzeugen → Host-Pfad-Kollision mit `_inject_log_aggregator`-Output-Dir → Self-Ingestion über Fallback-Pattern; Kommentar behauptete Ausschluss, Code implementierte ihn nicht | Neuer `_log_safe_name()` Helper + `_FILEBEAT_OUT_SLUG`-Konstante; beide Funktionen nutzen denselben Helper → strukturelle Garantie statt Kommentar | orchestrator.py |
| REV40-P3 | **Testlücke Phase 7.8**: `YAML ohne filebeat_out-Zeile` war fachlich falsch — `output.file.path` enthält bewusst `filebeat_out`; gemeint war nur der Input-`paths:`-Abschnitt | Testfälle präzisiert: `filebeat_out` nicht in `paths:`-Inputs, aber weiterhin in `output.file.path` | UMSETZUNGSPLAN.md |
| REV41-P1 | **Phase 7.8 Fallback**: `log_paths` leer (z.B. `catalog=None`) → Fallback `/usr/share/filebeat/logs/**/*` → erfasst `filebeat_out/` das im selben Mount-Baum liegt; Testlücke forderte diesen Fallback explizit | Output nach `/usr/share/filebeat/output` (eigener Mount-Baum, außerhalb `/usr/share/filebeat/logs/`); Fallback ist jetzt safe — kann Output physisch nicht erreichen | orchestrator.py |
| REV41-P2 | **Phase 7.4**: Fixer `_hn`-Suffix in `_log_safe_name` erzeugt neuen Kollisionsfall ("filebeat-out" und "filebeat_out_hn" → beide `filebeat_out_hn`); allgemein nicht injektiv | `_log_safe_name` hängt 8-Zeichen SHA-256-Präfix des Originalnamens an; deterministisch, stabil, kollisionsfrei mit vernachlässigbarer Kollisionswahrscheinlichkeit | orchestrator.py |
| REV42-P1 | **Phase 7.6**: `filebeat_out` als Unterordner von `session_log_root` → unter `/usr/share/filebeat/logs/filebeat_out` sichtbar trotz separatem Mount → Fallback `/**/*` liest Output ein; Kommentare/Tests behaupteten false Isolation | Output-Host-Pfad auf `session_log_root + "_out"` (Geschwisterverzeichnis) → physisch kein Unterordner des Input-Mounts; `EvidenceSigner` bekommt `output_base`-Parameter; `finalize_bundle` archiviert beide Bäume | orchestrator.py, deployer.py |
| REV42-P3 | **Phase 7.8 Testlücke**: `catalog=None → Fallback sicher` behauptete Service-Log-Capture; `_attach_log_mounts` bricht ohne Katalog ab → keine Mounts → leerer Scan | Explizit als "kein Log-Capture"-Modus dokumentiert; Fallback harmlos (leeres Ergebnis, keine Self-Ingestion) | UMSETZUNGSPLAN.md |
| REV43-P1 | **Phase 10.3**: `export_evidence()` übergab `output_base=None` an `EvidenceSigner` → `honeynet.ndjson` nie archiviert; REV42-Fix nur halb umgesetzt | `export_evidence` leitet `output_base=bundle.log_root + "_out"` durch (REV42-Konvention) | orchestrator.py |
| REV43-P3 | **Testlücke Phase 10**: `finalize_bundle archiviert filebeat_out/honeynet.ndjson` referenzierte alten Pfad aus Unterordner-Modell; neuer Archiv-Pfad ist `output/honeynet.ndjson` (separater Baum) | Testfälle auf `output/`-Präfix und `export_evidence`-Verdrahtung umgestellt | UMSETZUNGSPLAN.md |

---

## Testlücken

*(Phase 2–16 wie bisher — plus neue REV29-Einträge)*

**REV29 — Neue Testlücken**

**Phase 2.1 — PEP-503-Normalisierer (jetzt implementiert)**
- `_pep503_normalize("apache-kafka")` → `"apache-kafka"`
- `_pep503_normalize("apache_kafka")` → `"apache-kafka"`
- `_pep503_normalize("Apache.Kafka")` → `"apache-kafka"`
- `build_archetype_lookup_index(catalog)` → enthält `"apache-kafka"` → `"apache/kafka"`
- `normalize_expanded_archetype("kafka", catalog)` → `"apache/kafka"` (Basename-Fallback)
- `normalize_expanded_archetype("postgresql", catalog)` → `"postgres"` (Technology-Alias)
- `normalize_expanded_archetype("db", catalog)` → `None` wenn mehrdeutig (Kollision)
- `normalize_expanded_archetype("unknown_xyz", catalog)` → `None`

**Phase 13 — Repair-Loop-Helfer in `world_model_repair.py`**
- `from world_model_repair import _build_repair_prompt` funktioniert
- `from repair.strategies import _build_repair_prompt` schlägt fehl (nicht mehr dort)
- `_build_repair_prompt(errors, attempt=1)` → komprimierter Summary
- `_build_repair_prompt(errors, attempt=3)` → vollständiger Fehler-Text
- `_repair_is_making_progress(10, 10, attempt=1)` → `True` (Stage 1, erster Versuch)
- `_repair_is_making_progress(10, 10, attempt=MAX_REPAIR_ATTEMPTS_STAGE_1)` → `True` (Stage 1, letzter freier Versuch — kein Fortschritt erzwungen)
- `_repair_is_making_progress(10, 10, attempt=MAX_REPAIR_ATTEMPTS_STAGE_1 + 1)` → `False` (Stage 2 beginnt, kein Fortschritt → Abort)
- `_repair_is_making_progress(10, 5,  attempt=MAX_REPAIR_ATTEMPTS_STAGE_1 + 1)` → `True` (Stage 2, Fehler gesunken → weitermachen)
- `_repair_is_making_progress(10, 10, attempt=MAX_REPAIR_ATTEMPTS_STAGE_1 + MAX_REPAIR_ATTEMPTS_STAGE_2)` → `False` (letzter Stage-2-Versuch, kein Fortschritt → Abort)

**Phase 10 — `EvidenceSigner` (REV38-P2, REV42-P1, REV43-P1)**
- `EvidenceSigner` hat keine `create_bundle`-Methode mehr (AttributeError wenn aufgerufen)
- `finalize_bundle(bundle)` → zurückgegebenes Bundle hat `log_root=bundle.log_root`
- `finalize_bundle` archiviert Datei ohne `.log`-Suffix (z.B. `access_log`) → im Archiv unter `logs/`
- `finalize_bundle` mit `output_base` gesetzt → `output/honeynet.ndjson` im Archiv vorhanden
- `finalize_bundle` mit `output_base=None` → kein `output/`-Eintrag im Archiv (Elasticsearch-Modus)
- `finalize_bundle` überspringt Verzeichnisse (`is_file()`-Guard) → kein `IsADirectoryError`
- `export_evidence(bundle)` leitet `output_base=bundle.log_root + "_out"` an `EvidenceSigner` weiter
- `export_evidence` mit `output_base`-Pfad der nicht existiert → `output/`-Baum leer, kein Fehler (`Path.exists()`-Guard)

**Phase 7.8 — `_render_filebeat_config` (REV38-P2, REV39-P1, REV40-P2, REV42)**
- Container mit `log_paths=["/var/log/nginx/access_log"]` im Katalog → Filebeat-Input-Pfad `/usr/share/filebeat/logs/{slug}/var_log_nginx/access_log`
- Container ohne Katalog-Entry → Fallback `/usr/share/filebeat/logs/{slug}/**/*`
- `catalog=None` → `log_paths` leer → Fallback `/usr/share/filebeat/logs/**/*`; kein Service-Log-Capture (dokumentierter "kein Log-Capture"-Modus); kein Self-Ingestion (Host-Output-Pfad ist Geschwister von `session_log_root`, nicht Unterordner)
- `output.file.path` im erzeugten YAML ist `/usr/share/filebeat/output`
- Host-Output-Pfad `session_log_root + "_out"` ist KEIN Unterordner von `session_log_root` → nicht unter `/usr/share/filebeat/logs/` gemountet → physisch unerreichbar für Input-Patterns
- `_log_safe_name("nginx")` → `"nginx_<sha256[:8]>"` (deterministisch, stabil)
- `_log_safe_name("nginx-1")` ≠ `_log_safe_name("nginx_1")` (kollisionsfrei durch Hash)
- `_log_safe_name("filebeat-out")` ≠ `_log_safe_name("filebeat_out_hn")` (kein fixer Suffix)
- `_attach_log_mounts` und `_render_filebeat_config` liefern für denselben Container-Namen denselben Slug (Konsistenz-Test)
- `_inject_log_aggregator` mountet: `session_log_root → /usr/share/filebeat/logs:ro`; `session_log_root + "_out" → /usr/share/filebeat/output:rw`

**Phase 10 — `EvidenceSigner` (REV42-P1)**
- `EvidenceSigner(log_base, evidence_dir, output_base)` nimmt `output_base` entgegen
- `finalize_bundle` archiviert `logs/`-Baum (Service-Logs) + `output/`-Baum (honeynet.ndjson) getrennt im Archiv
- `output_base=None` → nur `logs/`-Baum (Elasticsearch-Modus, kein lokaler Output)
- Archiv-Pfade: `logs/{relative_path}` und `output/{relative_path}` — keine Kollision

**Phase 16 — `network_mode` Guard im Renderer**
- Container mit `network_mode="host"`: kein `networks_advanced`-Block im Output
- Container ohne `network_mode`: `networks_advanced`-Block wie bisher
- Container mit `network_mode="host"` und `networks=[]`: valider Renderer-Output

**Phase 9 — `upload_files` Deduplizierung**
- Zwei HoneytokenTemplates mit identischem `path`: nur ein `upload_files`-Eintrag
- Zwei Templates mit unterschiedlichen Pfaden: beide in `upload_files`

**Phase 6b.3 — `DeployContainer.to_dict()` Port-Serialisierung**
- `Port(internal=5432, external=5432, protocol="tcp", ip="0.0.0.0").to_dict()`
  → `{"internal": 5432, "external": 5432, "protocol": "tcp", "ip": "0.0.0.0"}`
- `DeployContainer.to_dict()["ports"]` enthält `protocol` und `ip` — kein Abschneiden mehr
- Alter Inline-Dict `{"internal": p.internal, "external": p.external}` existiert nicht mehr

**Phase 6b.5 — Katalog-Serialisierung und Roundtrip**
- `ImageCatalogEntry` mit `traffic_profile`: `to_dict()` enthält `"traffic_profile"`
- `from_dict(entry.to_dict())` → identisches `traffic_profile`-Objekt (Roundtrip verlustfrei)
- `ImageCatalogEntry` mit `health_contract`: Roundtrip verlustfrei (alle 9 Felder)
- `ImageCatalogEntry` mit `vulnerability_profiles`: Roundtrip verlustfrei
- `ImageCatalogEntry` mit `network_aliases`: nach Roundtrip noch vorhanden
- `ImageCatalogEntry` mit `required_companion_archetypes`: nach Roundtrip noch vorhanden
- `from_dict()` mit unbekannten Schlüsseln: keine Exception (defensive `data.get()`)
- `PROVENANCE_LLM_ENRICHED` enthält alle neuen Felder aus Phasen 7–16

**Phase 12 — `expose_ports_externally` Gate-Override (G9)**
- Container in interner Zone mit `vulnerability_profile="default_creds"` und
  `expose_ports_externally=True` → Port erscheint in `DeployContainer.ports`
  (Gate wird bypassed)
- Container in interner Zone OHNE `vulnerability_profile` → Port weiterhin
  verworfen (Gate greift normal)
- Container in externer Zone mit `public_reverse_proxy_present=True` und
  `expose_ports_externally=True` → Port erscheint (Gate für Profil-Container bypassed)
- `Port.ip="0.0.0.0"` landet im Renderer-Output → `ip`-Feld in Terraform-Block

**Phase 12 — Port-Loop Port-Objekte (G12)**
- Phase 12.5 setzt `c["ports"]` als Liste von dicts mit `"ip"`-Schlüssel
- Keine Port-Objekte in `c["ports"]` nach Phase 12.5
- Port-Loop liest `ip` aus dict korrekt aus
- `Port`-Objekte die in anderen Phasen in `c["ports"]` landen werden ebenfalls
  korrekt durch den erweiterten Port-Loop verarbeitet (Phase 12.5b)

**Phase 12.6 / 14.1 — Profil-Auflösung im Validator (REV34-P2, REV35-P2, REV36-P2)**
- `vulnerability_profile="default_creds"` (gültig, im Katalog vorhanden) → `HONEYNET_VULNERABILITY_ACTIVE`-Warning
- `vulnerability_profile="default_creds"` + Katalog + Entry + Profil gefunden → `HONEYNET_VULNERABILITY_ACTIVE`-Warning (Fall 4)
- `vulnerability_profile="defualt_creds"` (Tippfehler) + Katalog + Entry vorhanden → `VULN_PROFILE_UNKNOWN`-Error; keine `HONEYNET_VULNERABILITY_ACTIVE`-Warning (Fall 3)
- `vulnerability_profile="defualt_creds"` + Katalog + Entry=None (Archetyp unbekannt) → keine Profil-Warning, kein Profil-Error; Archetyp-Validator spricht allein (Fall 2)
- `vulnerability_profile="defualt_creds"` + kein Katalog → gutgläubig `HONEYNET_VULNERABILITY_ACTIVE`-Warning (Fall 1)
- `vulnerability_profile="defualt_creds"` in interner Zone + Entry vorhanden → `VULN_PROFILE_UNKNOWN`-Error, kein `POLICY_ZONE_ISOLATION`
- `vulnerability_profile="defualt_creds"` in interner Zone + Archetyp unbekannt → weder `VULN_PROFILE_UNKNOWN` noch `POLICY_ZONE_ISOLATION`
- `vulnerability_profile="default_creds"` in interner Zone + Katalog + Entry + Profil gefunden → `POLICY_ZONE_ISOLATION`-Error (Fall 4-Auflösung, Zone falsch)

**Phase 14.2 — Port-ip-Prüfung auf IR-Ebene (REV31-G13, REV33-P2)**
- Container mit `p.ip == "0.0.0.0"` UND `active_profile` nicht None → keine Warning
- Container mit `p.ip == "0.0.0.0"` UND `active_profile=None` → Warning wird emittiert
- Container mit `vulnerability_profile="defualt_creds"` (Tippfehler) → `active_profile=None` → Warning wird trotzdem emittiert
- `vuln_profile_name` (rohes String-Feld) wird für die Warning-Bedingung NICHT verwendet
- Prüfung iteriert über finale `ports`-Liste (Port-Objekte), nicht `c["ports"]`
- `p.ip` direkt zugegriffen — kein `getattr`-Fallback erforderlich

**Phase 16 — Externer Port aus finaler IR (REV31-G14)**
- `external_port = next((p.external for p in ports if p.ip == "0.0.0.0"), None)`
  → liest aus der finalen `ports`-Liste (Port-Objekte, nach `_allocate_unique_external_port`)
- Bei Port-Remap: `external_port` enthält den tatsächlich publizierten Port, nicht den angeforderten
- `c["ports"]` wird hier NICHT verwendet — wäre dict-basiert und ohne Remap-Info

**Phase 7.8 — Windows-Bind-Mount-Erkennung (REV32-P1)**
- Linux-Volume `"/data/logs/nginx:/var/log/nginx"` → `safe="nginx"` gefunden
- Windows-Volume `"C:\\data\\logs\\nginx:/var/log/nginx"` → `safe="nginx"` gefunden
- Windows-Volume `"C:/data/logs/nginx:/var/log/nginx"` → `safe="nginx"` gefunden
- Volume ohne `safe` im Host-Pfad → Container nicht in Filebeat-Config

**Phase 9 — `from_dict()` defensive Deserialisierung (REV32-P2)**
- `from_dict({"honeytoken_templates": [{"type": "file"}]})` → leere Liste (kein `path` → übersprungen)
- `from_dict({"honeytoken_templates": [{"type": "file", "path": "/t", "template": "x"}]})` → 1 Eintrag
- `from_dict({"vulnerability_profiles": [{"description": "no name"}]})` → leere Liste
- `from_dict({"vulnerability_profiles": [{"name": "creds"}]})` → 1 Eintrag
- `from_dict({})` → keine Exception (alle `.get()` mit Defaults)

**Phase 9 — env-Honeytoken-Deduplizierung (REV32-P3)**
- Zwei `env`-Templates mit gleichem `path="SECRET_KEY"` → nur ein `SECRET_KEY=...`-Eintrag
- Letztes Template in Originalreihenfolge gewinnt (identische Policy wie Datei-Templates)
- Gemischte Duplikate (ein `file`, ein `env` mit `path="/etc/x"`) → beide unabhängig dedupliziert
- `env_extras`-Liste enthält keine doppelten Keys nach Verarbeitung
