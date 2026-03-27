# Call 19 — Phase 10: Beweissicherung — EvidenceSigner + export_evidence

**Schwierigkeit:** ★★★ hoch  
**Abhängigkeiten:** Call 13 (Phase 6.1 — `EvidenceBundle`, `DeploymentResult`), Call 16 (Phase 7 — `session_log_root`-Konvention)  
**Risiko:** hoch — Archivierung beider Host-Bäume; `output_base` muss korrekt verdrahtet sein

---

## Ziel

`EvidenceSigner` in `deployer.py` implementieren. `export_evidence()` in
`orchestrator.py` implementieren und `output_base` korrekt durchreichen,
damit `honeynet.ndjson` aus `session_log_root + "_out"` im Archiv landet.

**Kritisch:** `output_base` ist das Geschwisterverzeichnis von `session_log_root`
(REV42-Konvention). `export_evidence()` muss es explizit berechnen und
an `EvidenceSigner` übergeben. `_output_base=None` → `honeynet.ndjson` nie archiviert.

---

## Dateien

### Zu editieren
- `honeynet_framework/deployer.py` — `EvidenceSigner`-Klasse
- `honeynet_framework/orchestrator.py` — `EvidenceBundle`-Initialisierung + `export_evidence()`

### Als Kontext mitgeben
- `deployer.py` vollständig
- `orchestrator.py` — deterministischer Deploy-Pfad (ab session_id-Initialisierung)
- `models.py` — `EvidenceBundle`-Felder

---

## Änderungen

### 10.2 — `deployer.py`: `EvidenceSigner`

**[G2] `create_bundle` existiert nicht — einzige öffentliche Methode ist `finalize_bundle`.**

```python
class EvidenceSigner:
    def __init__(self, log_base: str, evidence_dir: str, output_base: Optional[str] = None):
        self._log_base = log_base
        # [REV42-P1] output_base = session_log_root + "_out"
        # Liegt außerhalb von log_base → muss explizit übergeben werden.
        # None = kein separater Output-Baum (z.B. Elasticsearch-Modus).
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
        # - "logs/"   → Service-Logs (session_log_root)
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

### 10.3 — `orchestrator.py`: Session-Start

Am Ende des deterministischen Deploy-Pfads (nach `result.success`):
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

### 10.3 — `orchestrator.py`: `export_evidence()`

```python
async def export_evidence(self, bundle: "EvidenceBundle") -> "EvidenceBundle":
    evidence_dir = self.config.evidence_dir or str(self.config.work_dir / "evidence")
    # [REV43-P1] output_base durchreichen — output_base=None → honeynet.ndjson nie archiviert.
    # REV42-Konvention: Geschwisterverzeichnis außerhalb des Input-Baums.
    output_base = bundle.log_root.rstrip("/").rstrip("\\") + "_out"
    signer = EvidenceSigner(
        log_base=bundle.log_root,
        evidence_dir=evidence_dir,
        output_base=output_base,
    )
    return signer.finalize_bundle(bundle)
```

---

## Kritischer Integrationstest nach dem Call

```python
import tempfile, os
from pathlib import Path
from honeynet_framework.deployer import EvidenceSigner
from honeynet_framework.models import EvidenceBundle

with tempfile.TemporaryDirectory() as tmp:
    log_root = os.path.join(tmp, "session_abc")
    output_base = log_root + "_out"
    evidence_dir = os.path.join(tmp, "evidence")

    # Service-Log anlegen
    os.makedirs(log_root, exist_ok=True)
    Path(os.path.join(log_root, "access_log")).write_text("line1")

    # Filebeat-Output anlegen
    os.makedirs(output_base, exist_ok=True)
    Path(os.path.join(output_base, "honeynet.ndjson")).write_text('{"event": 1}')

    bundle = EvidenceBundle(
        session_id="abc", log_root=log_root,
        started_at="2026-01-01", ended_at=None,
        log_archive_path="", sha256_manifest="", bundle_signature=""
    )

    signer = EvidenceSigner(log_root, evidence_dir, output_base=output_base)
    result = signer.finalize_bundle(bundle)

    import tarfile
    with tarfile.open(result.log_archive_path, "r:gz") as tar:
        names = tar.getnames()
    # Service-Log: "logs/access_log"
    assert any("logs/access_log" in n for n in names)
    # Filebeat-Output: "output/honeynet.ndjson"
    assert any("output/honeynet.ndjson" in n for n in names)

# output_base nicht existent → kein Fehler, kein output/-Eintrag
with tempfile.TemporaryDirectory() as tmp:
    log_root = os.path.join(tmp, "session_xyz")
    os.makedirs(log_root)
    evidence_dir = os.path.join(tmp, "evidence")
    signer = EvidenceSigner(log_root, evidence_dir, output_base=log_root + "_out")
    bundle2 = EvidenceBundle("xyz", log_root, "now", None, "", "", "")
    result2 = signer.finalize_bundle(bundle2)
    with tarfile.open(result2.log_archive_path, "r:gz") as tar:
        assert not any("output/" in n for n in tar.getnames())
```
