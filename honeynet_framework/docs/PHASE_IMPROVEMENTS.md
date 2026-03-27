# Pipeline Phase Improvements

## Phase 2: Katalog-Bereitstellung

### Problem 1: Synchrones Git-Blocking
**Datei:** `catalog/sync_service.py:38-52`
```python
# Aktuell: Blockiert 120s pro Repo
r = subprocess.run(["git"] + list(args), timeout=120, ...)
```

**Verbesserung:** Async Git-Operationen mit konfigurierbarem Timeout
```python
async def _run_git_async(cwd: Path, *args: str, timeout: float = None) -> tuple[bool, str, str]:
    config = get_pipeline_config().catalog
    timeout = timeout or config.git_timeout
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode == 0, stdout.decode(), stderr.decode()
    except asyncio.TimeoutError:
        proc.kill()
        return False, "", f"git timed out after {timeout}s"
```

### Problem 2: Sequentieller Sync
**Datei:** `catalog/sync_service.py:82-110`
```python
# Aktuell: Nacheinander
oi_ok, oi_commit = self._ensure_repo(self.official_images_dir, ...)
docs_ok, docs_commit = self._ensure_repo(self.docs_dir, ...)
```

**Verbesserung:** Paralleler Sync mit asyncio.gather
```python
async def sync_on_startup_async(self) -> SyncResult:
    oi_task = self._ensure_repo_async(self.official_images_dir, OFFICIAL_IMAGES_REPO)
    docs_task = self._ensure_repo_async(self.docs_dir, DOCS_REPO)

    (oi_ok, oi_commit), (docs_ok, docs_commit) = await asyncio.gather(
        oi_task, docs_task, return_exceptions=True
    )
```

### Problem 3: Fehlende Sync-Metriken
**Verbesserung:** Zeitmessung und Statistiken
```python
@dataclass
class SyncResult:
    success: bool = False
    sync_duration_seconds: float = 0.0
    official_images_sync_ms: float = 0.0
    docs_sync_ms: float = 0.0
    bytes_transferred: int = 0
```

---

## Phase 3: Scope Intent Extraktion

### Problem 1: Hardcodierte Scale-Keywords
**Datei:** `extraction/scope_intent.py:148-150`
```python
# Aktuell: Hardcodiert
if any(k in text for k in ("global", "carrier", "tier-one", ...)):
    scale = "massive"
```

**Verbesserung:** Konfigurierbare Keywords in PipelineConfig
```python
@dataclass
class ScaleDetectionConfig:
    massive_keywords: tuple[str, ...] = (
        "global", "carrier", "tier-one", "backbone",
        "continents", "critical national infrastructure", "thousands"
    )
    large_keywords: tuple[str, ...] = (
        "enterprise", "multi-region", "multinational", "telecom"
    )
    small_keywords: tuple[str, ...] = ("poc", "demo", "test", "lab")
```

### Problem 2: Confidence-Berechnung ohne Szenario-Kontext
**Datei:** `extraction/scope_intent.py:25-47`

**Verbesserung:** Szenario-spezifische Confidence-Anpassung
```python
def scope_intent_confidence_heuristic(
    scope: "ScopeIntent",
    user_request: str,
    scenario_contract: Optional["ScenarioContract"] = None,  # NEU
    config: Optional["ConfidenceHeuristicConfig"] = None,
) -> float:
    # Bonus wenn Szenario explizite Technologien definiert
    if scenario_contract and scenario_contract.required_technologies:
        tech_bonus = min(0.15, 0.03 * len(scenario_contract.required_technologies))
        h += tech_bonus
```

### Problem 3: Kein Caching von Scope-Extraktionen
**Verbesserung:** Hash-basiertes Caching für identische Prompts
```python
class ScopeIntentCache:
    _cache: dict[str, tuple[ScopeIntent, float]] = {}

    @classmethod
    def get_or_extract(cls, user_request: str, extractor: Callable) -> ScopeIntent:
        key = hashlib.sha256(user_request.encode()).hexdigest()[:16]
        if key in cls._cache:
            scope, timestamp = cls._cache[key]
            if time.time() - timestamp < 300:  # 5 min TTL
                return scope
        scope = extractor(user_request)
        cls._cache[key] = (scope, time.time())
        return scope
```

---

## Phase 4: World Model Extraktion

### Problem 1: Kein Streaming für große Antworten
**Verbesserung:** Streaming-Parser für YAML
```python
async def extract_with_streaming(
    self, user_request: str, scope: ScopeIntent, ...
) -> WorldModel:
    chunks = []
    async for chunk in self.llm.generate_stream(prompt):
        chunks.append(chunk)
        # Early validation: prüfe ob YAML-Struktur valide beginnt
        partial = "".join(chunks)
        if len(partial) > 500 and not self._looks_like_valid_yaml_start(partial):
            logger.warning("LLM output does not look like valid YAML, aborting early")
            break
    return self._parse_world_model("".join(chunks))
```

### Problem 2: Keine Fortschrittsanzeige
**Verbesserung:** Progress-Callback für UI
```python
@dataclass
class ExtractionProgress:
    phase: str  # "topology", "zone_expansion", "system_details"
    current: int
    total: int
    message: str

async def extract(
    self,
    user_request: str,
    progress_callback: Optional[Callable[[ExtractionProgress], None]] = None,
) -> WorldModel:
    if progress_callback:
        progress_callback(ExtractionProgress("topology", 0, 3, "Extracting topology..."))
```

### Problem 3: Ineffiziente Zone-Expansion
**Datei:** `extraction/extractor.py`

**Verbesserung:** Batch-Zone-Expansion statt einzeln
```python
async def _expand_zones_batch(
    self,
    zone_plans: dict[str, dict],
    batch_size: int = 3,
) -> dict[str, list[System]]:
    """Expand multiple zones in parallel batches."""
    zones = list(zone_plans.items())
    results = {}

    for i in range(0, len(zones), batch_size):
        batch = zones[i:i + batch_size]
        tasks = [self._expand_single_zone(name, plan) for name, plan in batch]
        batch_results = await asyncio.gather(*tasks, return_exceptions=True)
        for (name, _), result in zip(batch, batch_results):
            if isinstance(result, Exception):
                logger.warning("Zone expansion failed for %s: %s", name, result)
                results[name] = []
            else:
                results[name] = result
    return results
```

---

## Phase 5: Resolver

### Problem 1: Keine Erklärung bei ambiguous
**Datei:** `catalog_resolver.py`

**Verbesserung:** Detaillierte Resolver-Begründung
```python
@dataclass
class ResolverResult:
    status: str  # "unique" | "ambiguous" | "none"
    archetype: Optional[str] = None
    candidates: list[str] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)  # NEU
    reason: str = ""
    exclusion_reasons: dict[str, str] = field(default_factory=dict)  # NEU
```

### Problem 2: Statische Kind→Category Mapping
**Datei:** `catalog_resolver.py:51-60`

**Verbesserung:** Erweiterbare Mappings via Config
```python
@dataclass
class ResolverMappingConfig:
    kind_to_categories: dict[str, set[str]] = field(default_factory=lambda: {
        "database": {"database", "graph database", "cache", ...},
        ...
    })
    # Erlaubt Erweiterung ohne Code-Änderung
    custom_mappings: dict[str, set[str]] = field(default_factory=dict)

    def get_categories(self, kind: str) -> set[str]:
        base = self.kind_to_categories.get(kind.lower(), set())
        custom = self.custom_mappings.get(kind.lower(), set())
        return base | custom
```

### Problem 3: Kein Fallback bei "none"
**Verbesserung:** Fallback-Strategie mit degradierter Suche
```python
def resolve_with_fallback(
    req: SystemRequirement,
    catalog: CatalogSnapshot,
) -> ResolverResult:
    result = resolve_requirement(req, catalog)

    if result.status == "none" and req.role_description:
        # Fallback: Suche nur nach role_description tokens
        fallback_req = SystemRequirement(
            kind=req.kind,
            capabilities_needed=[],  # Leer
            role_description=req.role_description,
        )
        fallback_result = resolve_requirement(fallback_req, catalog)
        if fallback_result.status != "none":
            fallback_result.reason = f"Fallback from role_description: {req.role_description}"
            return fallback_result

    return result
```

---

## Phase 6: Katalog-Defaults

### Problem 1: Keine Validierung vor Merge
**Verbesserung:** Pre-Merge Validierung
```python
def apply_catalog_defaults_to_system(
    system: System,
    catalog: CatalogSnapshot,
    validate_before_merge: bool = True,  # NEU
) -> System:
    entry = get_catalog_entry_for_archetype(system.deploy.archetype, catalog)
    if not entry:
        return system

    if validate_before_merge:
        conflicts = _detect_merge_conflicts(system.deploy, entry)
        if conflicts:
            logger.warning(
                "Merge conflicts for %s: %s",
                system.name, conflicts
            )
```

### Problem 2: Kein Merge-Audit
**Verbesserung:** Detailliertes Merge-Log
```python
@dataclass
class MergeAudit:
    system_name: str
    archetype: str
    fields_from_catalog: list[str]
    fields_preserved: list[str]
    fields_overwritten: list[str]
    warnings: list[str]

def apply_catalog_defaults_with_audit(system: System, catalog: CatalogSnapshot) -> tuple[System, MergeAudit]:
    ...
```

---

## Phase 8: Validierung & Repair

### Problem 1: Repair-Statistiken nicht granular genug
**Verbesserung:** Bereits implementiert in `RepairStats`, aber erweitern:
```python
@dataclass
class RepairStats:
    attempts: list[RepairAttempt]
    errors_before: int
    errors_after: int
    # NEU:
    time_spent_ms: float = 0.0
    rules_attempted: set[str] = field(default_factory=set)
    rules_succeeded: set[str] = field(default_factory=set)
    systems_modified: set[str] = field(default_factory=set)
```

### Problem 2: Keine Priorisierung von Reparaturen
**Verbesserung:** Prioritäts-basierte Repair-Reihenfolge
```python
REPAIR_PRIORITY: dict[str, int] = {
    "REF_ZONE": 1,  # Erst Zonen fixen
    "SCHEMA_SYSTEM_ZONE": 2,
    "REF_DEPENDS_ON": 3,
    "CONTRACT_REQUIRED_ENV": 4,
    "POLICY_ARCHETYPE": 5,
}

def _sort_errors_by_priority(errors: list[ValidationError]) -> list[ValidationError]:
    return sorted(errors, key=lambda e: REPAIR_PRIORITY.get(e.rule, 99))
```

### Problem 3: Keine Rollback-Möglichkeit
**Verbesserung:** Snapshot vor Repair
```python
class WorldModelRepairLoop:
    def repair(self, world_model: WorldModel, ...) -> WorldModel:
        # Snapshot für Rollback
        self._last_snapshot = copy.deepcopy(world_model)

        try:
            return self._do_repair(world_model, ...)
        except RepairError as e:
            logger.error("Repair failed, rolling back: %s", e)
            return self._last_snapshot

    def rollback(self) -> Optional[WorldModel]:
        """Rollback to pre-repair state."""
        return self._last_snapshot
```

---

## Zusammenfassung der Top-Prioritäten

| Phase | Verbesserung | Impact | Aufwand |
|-------|-------------|--------|---------|
| 2 | Paralleler Async Git-Sync | Hoch | Mittel |
| 3 | Konfigurierbare Scale-Keywords | Mittel | Niedrig |
| 4 | Streaming + Progress-Callback | Hoch | Mittel |
| 5 | Resolver Fallback-Strategie | Hoch | Niedrig |
| 6 | Merge-Audit für Debugging | Mittel | Niedrig |
| 8 | Prioritäts-basierte Repairs | Mittel | Niedrig |

