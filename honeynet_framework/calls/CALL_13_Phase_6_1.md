# Call 13 — Phase 6.1: models.py — Deployment-Status + Ergebnis-Typen

**Schwierigkeit:** ★★☆ mittel  
**Abhängigkeiten:** Call 11 (Phase 6b — alle `to_dict()`-Ergänzungen abgeschlossen)  
**Risiko:** mittel — neue Enum + Dataclasses, `DeploymentResult` erweitert

---

## Ziel

`DeploymentStatus`-Enum, `ContainerDiagnosis`, `RuntimeSummary` und
`EvidenceBundle` in `models.py` einführen. `DeploymentResult` um
`runtime_summary` und `evidence_bundle` erweitern. Properties
`success` und `fully_healthy` ergänzen.

---

## Dateien

### Zu editieren
- `honeynet_framework/models.py` (~900 Zeilen)

### Als Kontext mitgeben
- `models.py` vollständig

---

## Änderungen

### Neue Typen einführen

```python
from enum import Enum

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
class EvidenceBundle:
    session_id: str
    log_root: str
    started_at: str
    ended_at: Optional[str]
    log_archive_path: str
    sha256_manifest: str
    bundle_signature: str
```

### `DeploymentResult` erweitern

```python
@dataclass
class DeploymentResult:
    # --- bestehende Felder ---
    runtime_summary: Optional[RuntimeSummary] = None
    evidence_bundle: Optional["EvidenceBundle"] = None

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

---

## Smoke-Test nach dem Call

```python
from honeynet_framework.models import (
    DeploymentStatus, RuntimeSummary, ContainerDiagnosis, EvidenceBundle
)

# Enum-Werte vorhanden
assert DeploymentStatus.DEPLOYED.value == "deployed"
assert DeploymentStatus.RUNTIME_DEGRADED.value == "runtime_degraded"

# RuntimeSummary Properties
rs = RuntimeSummary(total_expected=3, total_running=2)
assert rs.is_total_failure is False
rs2 = RuntimeSummary(total_expected=3, total_running=0)
assert rs2.is_total_failure is True

# EvidenceBundle importierbar
eb = EvidenceBundle(
    session_id="abc", log_root="/tmp/logs", started_at="now",
    ended_at=None, log_archive_path="", sha256_manifest="", bundle_signature=""
)
assert eb.session_id == "abc"
```
