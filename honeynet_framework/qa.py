"""
Post-deployment QA checks for honeynet containers.

Generates and runs checks based on the DeployProjection:
- Container running checks
- Dependency ordering checks
- TCP port connectivity checks
- HTTP status checks for web-facing ports
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)

_WEB_PORTS = {80, 443, 8080, 8443, 3000, 8000, 8888, 9090, 9200}
_QA_TCP_RETRY_SLEEP_S = 0.4
_DB_IMAGES = {"postgres", "mysql", "mariadb", "mongo", "redis", "elasticsearch", "opensearch", "clickhouse", "cassandra"}


class CheckType(str, Enum):
    CONTAINER_RUNNING = "container_running"
    DEPENDS_ON_RUNNING = "depends_on_running"
    TCP_CONNECT = "tcp_connect"
    HTTP_STATUS = "http_status"


class Severity(str, Enum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


@dataclass
class QACheck:
    id: str
    check_type: CheckType
    target: str
    params: dict = field(default_factory=dict)
    timeout_s: float = 10.0
    severity: Severity = Severity.CRITICAL


def filter_canary_checks(checks: list[QACheck], canary_ids: list[str]) -> list[QACheck]:
    """Keep only checks whose id matches a canary prefix or exact id (frozen subset for regression)."""
    if not canary_ids:
        return checks
    patterns = [p for p in canary_ids if str(p).strip()]
    if not patterns:
        return checks
    kept: list[QACheck] = []
    for c in checks:
        cid = c.id
        if any(cid == p or cid.startswith(p) for p in patterns):
            kept.append(c)
    return kept


@dataclass
class QACheckResult:
    check_id: str
    passed: bool
    actual: str = ""
    expected: str = ""
    error: str = ""
    duration_ms: float = 0.0
    check_type: str = ""


@dataclass
class QAReport:
    checks_run: int = 0
    checks_passed: int = 0
    checks_failed: int = 0
    results: list[QACheckResult] = field(default_factory=list)

    @property
    def pass_rate(self) -> float:
        return self.checks_passed / self.checks_run if self.checks_run > 0 else 0.0

    def to_dict(self) -> dict:
        return {
            "checks_run": self.checks_run,
            "checks_passed": self.checks_passed,
            "checks_failed": self.checks_failed,
            "pass_rate": round(self.pass_rate, 4),
            "results": [
                {
                    "check_id": r.check_id,
                    "passed": r.passed,
                    "actual": r.actual,
                    "expected": r.expected,
                    "error": r.error,
                    "duration_ms": round(r.duration_ms, 1),
                    "check_type": r.check_type,
                }
                for r in self.results
            ],
        }

    def summary(self) -> str:
        lines = [f"QA: {self.checks_passed}/{self.checks_run} checks passed ({self.pass_rate:.0%})"]
        for r in self.results:
            if not r.passed:
                lines.append(f"  FAIL: {r.check_id} — {r.error or r.actual}")
        return "\n".join(lines)


class QARunner:
    """Build and run QA checks against a deployed honeynet."""

    def __init__(self, *, tcp_connect_extra_attempts: int = 1) -> None:
        self._tcp_extra = max(0, int(tcp_connect_extra_attempts))

    def build_checks(self, projection) -> list[QACheck]:
        """Generate QA checks from a DeployProjection."""
        checks: list[QACheck] = []

        for container in projection.containers:
            # Container running check
            checks.append(QACheck(
                id=f"running:{container.name}",
                check_type=CheckType.CONTAINER_RUNNING,
                target=container.name,
            ))

            # Port checks (published ports only)
            for port in container.ports:
                external = port.external
                checks.append(QACheck(
                    id=f"tcp:{container.name}:{external}",
                    check_type=CheckType.TCP_CONNECT,
                    target="localhost",
                    params={"port": external, "container": container.name},
                    timeout_s=5.0,
                ))

                # HTTP check for web-facing ports
                if port.internal in _WEB_PORTS:
                    checks.append(QACheck(
                        id=f"http:{container.name}:{external}",
                        check_type=CheckType.HTTP_STATUS,
                        target=f"http://localhost:{external}/",
                        params={"container": container.name},
                        timeout_s=10.0,
                        severity=Severity.WARNING,
                    ))

            # Dependency checks — verify upstream containers are running
            for dep in container.depends_on:
                checks.append(QACheck(
                    id=f"dep:{container.name}:{dep}",
                    check_type=CheckType.DEPENDS_ON_RUNNING,
                    target=dep,
                    params={"dependent": container.name},
                ))

        return checks

    async def run_checks(self, checks: list[QACheck]) -> QAReport:
        """Execute all QA checks concurrently and return a report."""

        async def _timed_check(check: QACheck) -> QACheckResult:
            start = time.monotonic()
            try:
                result = await self._run_single(check)
            except Exception as e:
                result = QACheckResult(
                    check_id=check.id,
                    passed=False,
                    error=str(e),
                )
            result.duration_ms = (time.monotonic() - start) * 1000
            result.check_type = check.check_type.value
            return result

        results = await asyncio.gather(*[_timed_check(c) for c in checks])
        results = list(results)

        passed = sum(1 for r in results if r.passed)
        return QAReport(
            checks_run=len(results),
            checks_passed=passed,
            checks_failed=len(results) - passed,
            results=results,
        )

    async def _run_single(self, check: QACheck) -> QACheckResult:
        """Run a single QA check."""
        if check.check_type == CheckType.CONTAINER_RUNNING:
            return await self._check_container_running(check)
        elif check.check_type == CheckType.DEPENDS_ON_RUNNING:
            return await self._check_container_running(check)
        elif check.check_type == CheckType.TCP_CONNECT:
            return await self._check_tcp_connect(check)
        elif check.check_type == CheckType.HTTP_STATUS:
            return await self._check_http_status(check)
        else:
            return QACheckResult(check_id=check.id, passed=False, error=f"Unknown check type: {check.check_type}")

    async def _check_container_running(self, check: QACheck) -> QACheckResult:
        """Check if a Docker container is running."""
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "inspect", "-f", "{{.State.Status}}", check.target,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=check.timeout_s)
            status = stdout.decode().strip()
            passed = status == "running"
            return QACheckResult(
                check_id=check.id,
                passed=passed,
                actual=status,
                expected="running",
                error="" if passed else f"Container {check.target} is '{status}'",
            )
        except asyncio.TimeoutError:
            if proc is not None:
                try:
                    proc.kill()
                    await proc.wait()
                except ProcessLookupError:
                    pass
            return QACheckResult(check_id=check.id, passed=False, error="Timeout checking container status")
        except FileNotFoundError:
            return QACheckResult(check_id=check.id, passed=False, error="docker CLI not found")

    async def _verify_port_owner(self, port: int, expected_container: str) -> tuple[bool, str]:
        """Verify that a published port is actually owned by the expected container.

        Returns (owner_match, message).  If verification cannot be performed
        (docker not available, port not mapped), returns (True, ...) to avoid
        false negatives.
        """
        if not expected_container:
            return True, "no container specified"
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "port", expected_container,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            port_map = stdout.decode()
            # docker port output lines: "80/tcp -> 0.0.0.0:8080"
            # Parse the external (host) port from the right side to avoid
            # substring false positives (e.g. "80" matching inside "8080").
            found = False
            for line in port_map.strip().splitlines():
                # Format: "<container_port>/<proto> -> <host_ip>:<host_port>"
                if "->" not in line:
                    continue
                right = line.split("->")[-1].strip()
                # right is like "0.0.0.0:8080" or "[::]:8080"
                host_port_str = right.rsplit(":", 1)[-1].strip()
                try:
                    if int(host_port_str) == int(port):
                        found = True
                        break
                except (ValueError, IndexError):
                    continue
            if found:
                return True, f"port {port} confirmed on {expected_container}"
            return False, f"port {port} not mapped to {expected_container}"
        except Exception as e:
            # Cannot verify — be lenient to avoid false negatives
            # Kill the subprocess if it's still running (e.g. on timeout)
            if proc is not None:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
            logger.debug("Port owner verification error: %s", e)
            return True, f"verification_error — lenient"

    async def _check_tcp_connect(self, check: QACheck) -> QACheckResult:
        """Check TCP connectivity to a port, with optional port-owner verification."""
        port = check.params.get("port", 0)
        expected_container = check.params.get("container", "")

        # Verify port ownership first to prevent false-healthy from foreign processes
        owner_ok, owner_msg = await self._verify_port_owner(port, expected_container)
        if not owner_ok:
            return QACheckResult(
                check_id=check.id,
                passed=False,
                actual=owner_msg,
                expected=f"port {port} owned by {expected_container}",
                error=f"Port owner mismatch: {owner_msg}",
            )

        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(check.target, port),
                timeout=check.timeout_s,
            )
            writer.close()
            await writer.wait_closed()
            return QACheckResult(
                check_id=check.id,
                passed=True,
                actual=f"TCP connect to {check.target}:{port} OK",
                expected="connection",
            )
        except (ConnectionRefusedError, OSError, asyncio.TimeoutError) as e:
            last_err = e
            for attempt in range(self._tcp_extra):
                await asyncio.sleep(_QA_TCP_RETRY_SLEEP_S * (attempt + 1))
                try:
                    _, writer = await asyncio.wait_for(
                        asyncio.open_connection(check.target, port),
                        timeout=check.timeout_s,
                    )
                    writer.close()
                    await writer.wait_closed()
                    return QACheckResult(
                        check_id=check.id,
                        passed=True,
                        actual=f"TCP connect to {check.target}:{port} OK (after retry)",
                        expected="connection",
                    )
                except (ConnectionRefusedError, OSError, asyncio.TimeoutError) as e2:
                    last_err = e2
            return QACheckResult(
                check_id=check.id,
                passed=False,
                actual=str(last_err),
                expected="connection",
                error=f"TCP connect to {check.target}:{port} failed: {last_err}",
            )

    async def _check_http_status(self, check: QACheck) -> QACheckResult:
        """Check HTTP connectivity (accepts any 1xx-4xx response as alive)."""
        try:
            import httpx
            async with httpx.AsyncClient(timeout=check.timeout_s, verify=False) as client:
                resp = await client.get(check.target, follow_redirects=True)
                # For a honeynet, any response means the service is alive
                passed = resp.status_code < 500
                return QACheckResult(
                    check_id=check.id,
                    passed=passed,
                    actual=f"HTTP {resp.status_code}",
                    expected="HTTP < 500",
                    error="" if passed else f"HTTP {resp.status_code} from {check.target}",
                )
        except ImportError:
            return QACheckResult(check_id=check.id, passed=False, error="httpx not installed")
        except Exception as e:
            return QACheckResult(
                check_id=check.id,
                passed=False,
                actual=str(e),
                expected="HTTP response",
                error=f"HTTP request to {check.target} failed: {e}",
            )
