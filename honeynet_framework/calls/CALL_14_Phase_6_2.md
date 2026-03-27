# Call 14 — Phase 6.2: deployer.py — RuntimeVerifier

**Schwierigkeit:** ★★★ hoch  
**Abhängigkeiten:** Call 13 (Phase 6.1 — `RuntimeSummary`, `ContainerDiagnosis`)  
**Risiko:** hoch — neue asynchrone Klasse mit Docker-Kommunikation und LLM-Batch

---

## Ziel

`RuntimeVerifier`-Klasse in `deployer.py` implementieren. Klassifiziert
gestoppte Container via Docker-Inspect (OOM-Kill) und optionalem LLM-Batch.
`_inspect_batch` verwendet einen einzigen `docker inspect`-Aufruf mit
Einzelfall-Fallback.

---

## Dateien

### Zu editieren
- `honeynet_framework/deployer.py` (~600 Zeilen)

### Als Kontext mitgeben
- `deployer.py` vollständig
- `models.py` — `RuntimeSummary`, `ContainerDiagnosis`
- Signatur von `CommandResult` (falls in deployer.py vorhanden)

---

## Änderungen

### `RuntimeVerifier`-Klasse implementieren

```python
class RuntimeVerifier:
    _CATEGORIES = [
        "MISSING_ENV", "DEPENDENCY_MISSING", "INTERACTIVE_IMAGE",
        "LICENSE_REQUIRED", "CONFIG_ERROR", "UNKNOWN",
    ]
    _MAX_LLM_BATCH = 10

    def __init__(self, llm: Optional["LLMProvider"] = None):
        self._llm = llm

    async def verify(self, verification: dict) -> "RuntimeSummary":
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
                name=name, category="UNHEALTHY",
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
                        name=name, exit_code=insp.get("exit_code", 0),
                        oom_killed=True, category="OOM",
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

        # Einzelfall-Fallback für fehlende Container
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

    async def _classify_batch(self, names, inspections, logs):
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
            # LLM hat möglicherweise nicht alle Namen zurückgegeben
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
        import asyncio
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

---

## Smoke-Test nach dem Call

```python
import asyncio
from honeynet_framework.deployer import RuntimeVerifier
from honeynet_framework.models import RuntimeSummary

# Instanz ohne LLM
rv = RuntimeVerifier(llm=None)

# Leere Verification → alles healthy
result = asyncio.run(rv.verify({"expected_count": 0, "running_count": 0,
                                 "unhealthy": [], "missing": []}))
assert isinstance(result, RuntimeSummary)
assert result.is_healthy
```
