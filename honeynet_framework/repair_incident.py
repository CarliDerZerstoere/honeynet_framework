"""
Structured repair / pipeline incidents for audit (repair_attempts.jsonl).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .utils import utc_now_iso as _utc_now

_LEDGER_LOCK = threading.Lock()
_FILE_LOCK_PATH: Path | None = None


@dataclass
class RepairIncident:
    """One auditable incident (validation repair, image loop, apply, runtime, QA)."""

    run_id: str
    phase: str
    rule_or_kind: str = ""
    attempt: int = 0
    outcome: str = ""  # ok | fail | skip
    detail_hash: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json_line(self) -> str:
        d = asdict(self)
        d["recorded_at_utc"] = _utc_now()
        return json.dumps(d, ensure_ascii=False)


def append_repair_incident(ledger_path: Path, incident: RepairIncident) -> None:
    """Append one JSON line to the ledger (create parent dirs).

    Uses file-based locking for cross-process safety on top of the
    in-process threading lock.
    """
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    line = incident.to_json_line() + "\n"
    lock_path = ledger_path.with_suffix(".lock")
    with _LEDGER_LOCK:
        try:
            lock_fh = lock_path.open("a")
            if sys.platform == "win32":
                import msvcrt
                # Lock a large region from position 0 to cover the entire file.
                # Seek to 0 first since append mode positions at EOF.
                lock_fh.seek(0)
                _LOCK_REGION = 1024 * 1024  # 1 MiB region — more than enough
                msvcrt.locking(lock_fh.fileno(), msvcrt.LK_LOCK, _LOCK_REGION)
            else:
                import fcntl
                fcntl.flock(lock_fh, fcntl.LOCK_EX)
            try:
                with ledger_path.open("a", encoding="utf-8") as f:
                    f.write(line)
            finally:
                if sys.platform == "win32":
                    import msvcrt
                    lock_fh.seek(0)
                    msvcrt.locking(lock_fh.fileno(), msvcrt.LK_UNLCK, _LOCK_REGION)
                else:
                    import fcntl
                    fcntl.flock(lock_fh, fcntl.LOCK_UN)
                lock_fh.close()
        except Exception:
            # Fall back to thread-lock-only write if file locking unavailable
            with ledger_path.open("a", encoding="utf-8") as f:
                f.write(line)


def excerpt_hash(text: str, max_len: int = 2000) -> str:
    """Stable short hash of stderr excerpt for dedup without storing full text."""
    s = (text or "")[:max_len]
    return hashlib.sha256(s.encode("utf-8", errors="replace")).hexdigest()[:16]
