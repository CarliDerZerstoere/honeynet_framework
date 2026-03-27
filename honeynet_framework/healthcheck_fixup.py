"""
Deterministic healthcheck fixup for WorldModel systems.

Rewrites LLM-generated healthchecks that use wget/curl to robust
fallback chains, based on the image's OCI profile.  Many minimal
Docker images (traefik, minio, elasticsearch, scratch-based) ship
neither wget nor curl — the original healthcheck fails permanently
and Docker marks the container as unhealthy even though the service
is running correctly.

Runs as part of ``_post_extraction_oci_fixup`` — no LLM call, no
network requests.  Profiles are already fetched at that point.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .image_introspector import ImageProfile
    from .models import WorldModel

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Service-native healthcheck commands keyed by image base name.
# These are binaries that ship WITH the image — no wget/curl needed.
# ---------------------------------------------------------------------------
_NATIVE_HEALTHCHECKS: dict[str, str] = {
    "postgres": "pg_isready -U postgres",
    "timescaledb": "pg_isready -U postgres",
    "postgis": "pg_isready -U postgres",
    "mysql": "mysqladmin ping -h localhost",
    "mariadb": "mysqladmin ping -h localhost",
    "percona": "mysqladmin ping -h localhost",
    "redis": "redis-cli ping",
    "valkey": "redis-cli ping",
    "keydb": "redis-cli ping",
    "mongo": "mongosh --quiet --eval \"db.runCommand('ping').ok\" || exit 1",
    "mongosh": "mongosh --quiet --eval \"db.runCommand('ping').ok\" || exit 1",
    "rabbitmq": "rabbitmq-diagnostics check_port_connectivity",
    "memcached": "echo stats | nc localhost 11211 | grep -q pid",
    "clickhouse-server": "clickhouse-client --query 'SELECT 1'",
    "clickhouse": "clickhouse-client --query 'SELECT 1'",
    "cassandra": "cqlsh -e 'SELECT now() FROM system.local'",
    "couchdb": "curl -sf http://localhost:5984/_up || exit 1",
    "consul": "consul members | grep -q alive",
    "vault": "vault status -format=json | grep -q initialized",
    "etcd": "etcdctl endpoint health",
    "cockroachdb": "cockroach node status --insecure",
    "cockroach": "cockroach node status --insecure",
}

# Image base names known to have NO wget/curl/nc (scratch, distroless,
# Go binaries compiled statically, etc.).  For these we fall back to
# a TCP probe via /dev/tcp (bash built-in) or a no-op.
_TOOLLESS_IMAGES: frozenset[str] = frozenset({
    "traefik",
    "caddy",
    "envoy",
    "coredns",
    "minio",
    "mc",               # MinIO client
    "quay",
    "registry",         # Docker registry
    "loki",
    "promtail",
    "tempo",
    "mimir",
    "thanos",
    "alertmanager",
    "pushgateway",
    "blackbox-exporter",
    "node-exporter",
    "kube-state-metrics",
    "busybox",
    "scratch",
    "distroless",
    "static",
})

# Binaries that indicate the healthcheck is already service-native
# (not a generic wget/curl probe) and should not be rewritten.
_NATIVE_BINARIES: frozenset[str] = frozenset({
    "pg_isready", "mysqladmin", "redis-cli", "mongosh", "mongo",
    "rabbitmq-diagnostics", "rabbitmqctl", "cqlsh", "consul",
    "vault", "etcdctl", "clickhouse-client", "cockroach",
    "influx", "mc",
})


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

_HTTP_URL_RE = re.compile(
    r"https?://(?P<host>localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1?\]):?(?P<port>\d+)?(?P<path>/\S*)?",
)


def _image_base(image_ref: str) -> str:
    """Extract base image name: 'bitnami/redis:7' → 'redis', 'ghcr.io/minio/minio:latest' → 'minio'."""
    name = image_ref.split("@")[0].split(":")[0]   # strip tag/digest
    name = name.rsplit("/", 1)[-1]                  # strip registry/org prefix
    return name.lower()


def _extract_cmd_shell_body(test: list) -> str | None:
    """Return the shell command string from a CMD-SHELL healthcheck, or None."""
    if (
        isinstance(test, list)
        and len(test) >= 2
        and test[0] == "CMD-SHELL"
    ):
        return test[1]
    return None


def _is_fragile_probe(cmd: str) -> bool:
    """True if the command uses generic probe tools without a safe final fallback.

    A command is fragile when ALL of its tools might be missing and there is
    no unconditional success at the end.  ``curl ... || wget ... || true``
    is safe (the ``true`` catches the case where neither exists).
    ``curl ... || wget ... || exit 1`` is fragile (both missing → exit 1).
    """
    has_probe_tool = bool(re.search(r"\b(wget|curl|nc)\b", cmd))
    if not has_probe_tool:
        return False
    # Safe if the chain ends with an unconditional success
    has_safe_fallback = bool(re.search(r"\|\|\s*true\s*$", cmd.strip()))
    return not has_safe_fallback


def _is_native_check(cmd: str) -> bool:
    """True if the command uses a known service-native binary."""
    first_word = cmd.strip().split()[0].rsplit("/", 1)[-1] if cmd.strip() else ""
    return first_word in _NATIVE_BINARIES


def _build_http_fallback(url: str) -> str:
    """Build a curl-first fallback chain for an HTTP healthcheck."""
    return f"curl -sf {url} || wget -qO- {url} || true"


def _build_tcp_check(port: int) -> str:
    """Build a TCP-only healthcheck with graceful fallback.

    Chain: nc → /dev/tcp (bash builtin) → true (no-op).
    The final ``true`` ensures the container stays healthy even when
    the image has no networking tools at all (scratch, distroless).
    A running container without a working healthcheck is better than
    a running container permanently marked unhealthy.
    """
    return (
        f"nc -z localhost {port} "
        f"|| (echo > /dev/tcp/localhost/{port}) 2>/dev/null "
        f"|| true"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fix_healthcheck_commands(
    world_model: "WorldModel",
    profiles: dict[str, "ImageProfile"],
) -> int:
    """Rewrite fragile healthchecks to robust alternatives.

    Decision tree per system:
    1. Already service-native (pg_isready, redis-cli, …)?  → keep
    2. Image has a known native healthcheck?                → replace
    3. Toolless image (traefik, minio, …)?                  → TCP check (always)
    4. Fragile single-tool probe (curl/wget/nc alone)?      → curl||wget||true chain
    5. Otherwise                                            → keep

    Returns the number of healthchecks rewritten.
    """
    fixes = 0

    for sys_name, system in world_model.systems.items():
        if not system.deploy or not system.deploy.healthcheck:
            continue

        hc = system.deploy.healthcheck
        test = hc.get("test")
        body = _extract_cmd_shell_body(test)
        if body is None:
            continue

        image = system.deploy.image or ""
        base = _image_base(image)
        profile = profiles.get(image)

        # 1. Already service-native → don't touch
        if _is_native_check(body):
            continue

        # 2. Known native healthcheck for this image type
        native = _NATIVE_HEALTHCHECKS.get(base)
        if native:
            hc["test"] = ["CMD-SHELL", native]
            logger.info(
                "Healthcheck fixup: %s — replaced with native: %s",
                sys_name, native,
            )
            fixes += 1
            continue

        # Extract URL from the healthcheck (used by both paths below)
        url_match = _HTTP_URL_RE.search(body)

        # 3. Toolless image: ALWAYS rewrite to TCP check (regardless of tool used)
        if base in _TOOLLESS_IMAGES:
            # Toolless image: prefer TCP check
            if url_match and url_match.group("port"):
                port = int(url_match.group("port"))
            elif profile and profile.exposed_ports:
                # Use first exposed port
                port_str = profile.exposed_ports[0].split("/")[0]
                port = int(port_str) if port_str.isdigit() else 80
            elif system.deploy.ports:
                port = system.deploy.ports[0]
            else:
                port = 80

            new_cmd = _build_tcp_check(port)
            hc["test"] = ["CMD-SHELL", new_cmd]
            logger.info(
                "Healthcheck fixup: %s — toolless image '%s', using TCP check on port %d",
                sys_name, base, port,
            )
            fixes += 1
            continue

        # 4. Normal image with fragile single-tool probe → add fallback chain
        if not _is_fragile_probe(body):
            continue

        if url_match:
            # HTTP probe: add curl||wget||true chain
            url = url_match.group(0)
            new_cmd = _build_http_fallback(url)
            hc["test"] = ["CMD-SHELL", new_cmd]
            logger.info(
                "Healthcheck fixup: %s — added curl||wget fallback for %s",
                sys_name, url,
            )
            fixes += 1
        elif re.search(r"\bnc\b", body):
            # TCP probe (nc -z): add /dev/tcp + true fallback
            # First try to extract port from host:port pattern, fall back to bare number
            port_match = (
                re.search(r"(?:localhost|127\.0\.0\.1)[:\s]+(\d{2,5})\b", body)
                or re.search(r"-[zw]\s+\S+\s+(\d{2,5})\b", body)  # nc -z host PORT
                or re.search(r"\s(\d{2,5})\s*(?:\|\||;|&&|$)", body)  # last port-like arg
            )
            if port_match:
                port = int(port_match.group(1))
                new_cmd = _build_tcp_check(port)
                hc["test"] = ["CMD-SHELL", new_cmd]
                logger.info(
                    "Healthcheck fixup: %s — added TCP fallback on port %d",
                    sys_name, port,
                )
                fixes += 1

    return fixes
