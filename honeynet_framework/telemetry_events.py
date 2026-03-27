"""
Append-only JSONL telemetry for deploy pipeline stages.

First line of each file (when empty) contains schema_version for consumers.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

TELEMETRY_SCHEMA_VERSION = 1
_DEFAULT_LOCK = threading.Lock()

# Redact likely secrets / tokens from telemetry snippets (best-effort).
_TOKENISH = re.compile(
    r"(?i)(api[_-]?key|token|password|secret|bearer|authorization)\s*[:=]\s*[^\s,;\"']+"
)
# Match IPv4 addresses but exclude RFC-1918 private ranges (10.x, 172.16-31.x, 192.168.x)
# which are typically Docker subnets needed for debugging.
_IPV4 = re.compile(r"\b(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\b")
_PRIVATE_IPV4 = re.compile(
    r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2[0-9]|3[01])\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3})\b"
)
# Redact credentials embedded in URLs (e.g. postgres://user:pass@host/db)
_URL_CREDS = re.compile(r"://[^:]+:[^@]+@")


def _redact_telemetry_text(text: str, max_len: int = 2000) -> str:
    s = text or ""
    # Redact BEFORE truncating to avoid leaking partial credentials
    # at the truncation boundary.
    s = _TOKENISH.sub(r"\1=<redacted>", s)
    s = _URL_CREDS.sub("://<user>:<redacted>@", s)
    # Redact public IPs but preserve private/Docker IPs for debugging
    def _redact_ip(match: re.Match) -> str:
        ip = match.group(0)
        if _PRIVATE_IPV4.match(ip):
            return ip
        return "<ip>"
    s = _IPV4.sub(_redact_ip, s)
    # Truncate AFTER redaction so partial secrets can't leak at the boundary.
    return s[:max_len]


def _redact_value(value: Any) -> Any:
    """Recursively redact sensitive strings in dicts/lists."""
    if isinstance(value, str):
        return _redact_telemetry_text(value)
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(v) for v in value]
    return value


def telemetry_path(work_dir: Path) -> Path:
    return work_dir / "telemetry" / "events.jsonl"


def append_event(
    work_dir: Path,
    *,
    run_id: str,
    stage: str,
    outcome: str,
    duration_ms: float,
    error_summary: Optional[str] = None,
    extra: Optional[dict[str, Any]] = None,
    flush: bool = True,
    _lock: threading.Lock = _DEFAULT_LOCK,
) -> None:
    """Append one JSON line. Writes schema header when file is new."""
    path = telemetry_path(work_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "schema_version": TELEMETRY_SCHEMA_VERSION,
        "timestamp_ms": int(time.time() * 1000),
        "run_id": run_id,
        "stage": stage,
        "outcome": outcome,
        "duration_ms": round(duration_ms, 2),
    }
    if error_summary:
        payload["error_summary"] = _redact_telemetry_text(error_summary)
    if extra:
        payload["extra"] = _redact_value(extra)

    line = json.dumps(payload, ensure_ascii=False) + "\n"

    with _lock:
        try:
            # Try exclusive creation first (O_CREAT|O_EXCL) to avoid cross-process
            # races where two processes both see an empty/missing file and both
            # write a header.  If the file already exists, fall back to normal append.
            import os as _os
            wrote_header = False
            try:
                fd = _os.open(str(path), _os.O_CREAT | _os.O_EXCL | _os.O_WRONLY)
                try:
                    header = json.dumps(
                        {"schema_version": TELEMETRY_SCHEMA_VERSION, "kind": "honeynet_telemetry_header"},
                        ensure_ascii=False,
                    ) + "\n"
                    _os.write(fd, (header + line).encode("utf-8"))
                    if flush:
                        _os.fsync(fd)
                    wrote_header = True
                finally:
                    _os.close(fd)
            except FileExistsError:
                pass  # File already exists — fall through to normal append

            if not wrote_header:
                needs_header = path.stat().st_size == 0
                with path.open("a", encoding="utf-8") as f:
                    if needs_header:
                        header = json.dumps(
                            {"schema_version": TELEMETRY_SCHEMA_VERSION, "kind": "honeynet_telemetry_header"},
                            ensure_ascii=False,
                        ) + "\n"
                        f.write(header)
                    f.write(line)
                    if flush:
                        f.flush()
        except FileNotFoundError:
            logger.warning("Telemetry directory disappeared before write")


def write_schema_header_if_empty(work_dir: Path) -> None:
    """Ensure telemetry file exists with header only (for tests)."""
    path = telemetry_path(work_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > 0:
        return
    with _DEFAULT_LOCK:
        if path.exists() and path.stat().st_size > 0:
            return
        with path.open("w", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {"schema_version": TELEMETRY_SCHEMA_VERSION, "kind": "honeynet_telemetry_header"},
                    ensure_ascii=False,
                )
                + "\n"
            )
