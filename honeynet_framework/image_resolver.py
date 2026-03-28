"""
Image Resolver - Resolve image refs to digests before deploy.

For each image in DeployProjection: check manifest via OCI Distribution Spec API.
Classify: RESOLVED, NOT_FOUND, AUTH_REQUIRED, RATE_LIMITED, etc.
Set resolved_digest_ref on containers when RESOLVED (image@sha256:...).

Uses direct HTTP HEAD /v2/{repo}/manifests/{ref} instead of docker manifest inspect:
- No Docker daemon required for remote checks
- HEAD requests do not count toward Docker Hub pull rate limits
- Docker-Content-Digest response header gives the reliable manifest digest
- Works uniformly across Docker Hub, GHCR, Quay, ECR, GCR and any OCI-compliant registry
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import re
import subprocess
import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

import httpx

from .models import DeployProjection, DeployContainer

logger = logging.getLogger(__name__)

# In-process cache for image config results (cleared between runs via
# clear_image_config_cache).  Only successful results are cached.
_image_config_cache: dict[str, dict] = {}


def clear_image_config_cache() -> None:
    """Clear the image config cache.  Call between test runs or pipeline runs."""
    _image_config_cache.clear()


async def fetch_image_config(image_ref: str, timeout: int = 15) -> dict:
    """Fetch the OCI image config (Env, Cmd, Entrypoint, ExposedPorts) for an image.

    Makes two unauthenticated-then-Bearer-authed GET requests:
      1. GET /v2/{repo}/manifests/{ref}  → config blob digest
      2. GET /v2/{repo}/blobs/{digest}   → image config JSON

    Results are cached in-process (successful results only).

    Returns a dict with keys ``env``, ``cmd``, ``entrypoint``, ``exposed_ports``,
    ``working_dir``, or ``{"error": "..."}`` on failure.
    """
    cached = _image_config_cache.get(image_ref)
    if cached is not None:
        return cached
    resolver = ImageResolver(timeout=timeout, max_retries=1)
    result = await resolver._fetch_image_config(image_ref)
    if "error" not in result:
        _image_config_cache[image_ref] = result
    return result


async def check_image_exists(image_ref: str, timeout: int = 10) -> dict:
    """Check whether a Docker image reference is pullable from a registry.

    Intended as the backend for the LLM ``validate_docker_image`` tool so the
    model can verify images during generation rather than after.

    Returns one of::

        {"exists": True,  "ref": "image@sha256:..."}
        {"exists": False, "status": "NOT_FOUND", "message": "..."}
    """
    resolver = ImageResolver(timeout=timeout, max_retries=1)
    entry = await resolver._resolve_one(image_ref)
    if entry.status == ResolutionStatus.RESOLVED:
        return {"exists": True, "ref": entry.digest_ref or image_ref}
    return {"exists": False, "status": entry.status.value, "message": entry.message}


_known_good_registry_cache: dict | None = None
_known_good_registry_lock = threading.Lock()


def _load_known_good_registry() -> dict:
    """Load the curated known-good images registry (cached after first successful load).

    Used by repair_scorer and retrieval modules for reference data.
    Image resolution uses live OCI API calls instead of this static list.

    Unlike ``@lru_cache``, this does NOT cache failures — a transient read
    error will be retried on the next call.
    """
    global _known_good_registry_cache
    with _known_good_registry_lock:
        if _known_good_registry_cache is not None:
            return _known_good_registry_cache
        registry_path = Path(__file__).parent / "data" / "known_good_images.json"
        if not registry_path.exists():
            return {}
        try:
            result = json.loads(registry_path.read_text(encoding="utf-8"))
            _known_good_registry_cache = result
            return result
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load known-good images registry: %s — will retry", e)
            return {}


# Registries tried in order when an image is not found on Docker Hub.
# Only applied to namespaced images (e.g. bitnami/kafka) — official single-name
# images (nginx, postgres) are not tried on other registries to avoid false matches.
_FALLBACK_REGISTRIES = ["ghcr.io", "quay.io"]

# OCI manifest media types accepted — covers both OCI and legacy Docker schema.
_MANIFEST_ACCEPT = (
    "application/vnd.oci.image.index.v1+json,"
    "application/vnd.oci.image.manifest.v1+json,"
    "application/vnd.docker.distribution.manifest.list.v2+json,"
    "application/vnd.docker.distribution.manifest.v2+json"
)


def _parse_image_ref(image_ref: str) -> tuple[str, str, str]:
    """Parse an image reference into (registry_host, repository, reference).

    Examples::

        "nginx"                     → ("registry-1.docker.io", "library/nginx", "latest")
        "nginx:1.25-alpine"         → ("registry-1.docker.io", "library/nginx", "1.25-alpine")
        "bitnami/kafka:3.6"         → ("registry-1.docker.io", "bitnami/kafka", "3.6")
        "ghcr.io/bitnami/kafka:3.7" → ("ghcr.io", "bitnami/kafka", "3.7")
        "nginx@sha256:abc..."       → ("registry-1.docker.io", "library/nginx", "sha256:abc...")
    """
    ref = image_ref

    # Separate digest component when already pinned (image@sha256:...)
    digest_ref = ""
    if "@" in ref:
        ref, digest_ref = ref.split("@", 1)

    # Separate tag — avoid confusing registry:port/path with image:tag.
    # Rule: if the substring after the *last* ":" contains no "/" it is a tag.
    tag = "latest"
    if ":" in ref:
        last_colon = ref.rfind(":")
        after_colon = ref[last_colon + 1 :]
        if "/" not in after_colon:
            tag = after_colon
            ref = ref[:last_colon]

    reference = digest_ref if digest_ref else tag

    # Determine registry host vs repository path.
    # A registry host contains a dot, a colon (port), or is "localhost".
    parts = ref.split("/")
    first = parts[0]
    is_registry_host = "." in first or ":" in first or first == "localhost"

    if is_registry_host:
        registry = first
        repo = "/".join(parts[1:])
    else:
        registry = "registry-1.docker.io"
        # Docker Hub official (library) images have no org prefix — add it.
        repo = ref if "/" in ref else f"library/{ref}"

    return registry, repo, reference


def _parse_www_auth_param(header: str, param: str) -> str:
    """Extract a named parameter value from a WWW-Authenticate Bearer header."""
    m = re.search(rf'{param}="([^"]*)"', header)
    return m.group(1) if m else ""


class ResolutionStatus(str, Enum):
    RESOLVED = "RESOLVED"
    NOT_FOUND = "NOT_FOUND"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    ACCESS_DENIED = "ACCESS_DENIED"
    RATE_LIMITED = "RATE_LIMITED"
    NETWORK_ERROR = "NETWORK_ERROR"
    REGISTRY_UNSUPPORTED = "REGISTRY_UNSUPPORTED"
    INVALID_REFERENCE = "INVALID_REFERENCE"
    UNEXPECTED_STATUS = "UNEXPECTED_STATUS"


@dataclass
class ImageResolutionEntry:
    """Per-image resolution result."""

    image_ref: str
    status: ResolutionStatus
    digest_ref: Optional[str] = None
    message: str = ""


@dataclass
class ResolutionReport:
    """Result of resolve_all."""

    entries: list[ImageResolutionEntry] = field(default_factory=list)
    by_image: dict[str, ImageResolutionEntry] = field(default_factory=dict)

    @property
    def all_resolved(self) -> bool:
        return all(e.status == ResolutionStatus.RESOLVED for e in self.entries)

    def unresolved(self) -> list[ImageResolutionEntry]:
        return [e for e in self.entries if e.status != ResolutionStatus.RESOLVED]

    def blocking_unresolved(
        self, *, promote_soft: bool = False
    ) -> list[ImageResolutionEntry]:
        """Return entries that should block deployment.

        Args:
            promote_soft: If True, soft failures (RATE_LIMITED, NETWORK_ERROR,
                REGISTRY_UNSUPPORTED) are also treated as blocking.
        """
        blocking = {
            ResolutionStatus.NOT_FOUND,
            ResolutionStatus.AUTH_REQUIRED,
            ResolutionStatus.ACCESS_DENIED,
            ResolutionStatus.INVALID_REFERENCE,
            ResolutionStatus.UNEXPECTED_STATUS,
        }
        if promote_soft:
            blocking |= {
                ResolutionStatus.RATE_LIMITED,
                ResolutionStatus.NETWORK_ERROR,
                ResolutionStatus.REGISTRY_UNSUPPORTED,
            }
        return [e for e in self.entries if e.status in blocking]

    def soft_unresolved(self) -> list[ImageResolutionEntry]:
        soft = {
            ResolutionStatus.RATE_LIMITED,
            ResolutionStatus.NETWORK_ERROR,
            ResolutionStatus.REGISTRY_UNSUPPORTED,
        }
        return [e for e in self.entries if e.status in soft]


class ImageResolver:
    """
    Resolve image references to digest-pinned refs (image@sha256:...).

    Uses OCI Distribution Spec HEAD requests — no Docker daemon required,
    no pull rate limits. Bearer token auth is handled automatically via the
    standard 401 → WWW-Authenticate challenge flow.
    """

    # Transient statuses that warrant automatic retry
    _TRANSIENT_STATUSES = frozenset({
        ResolutionStatus.NETWORK_ERROR,
        ResolutionStatus.RATE_LIMITED,
    })

    def __init__(self, timeout: int = 30, max_retries: int = 2):
        self.timeout = timeout
        self.max_retries = max_retries

    async def resolve_images(self, image_refs: list[str]) -> ResolutionReport:
        """Validate a list of image references exist (without DeployProjection)."""
        report = ResolutionReport()
        unique = list(set(image_refs))
        sem = asyncio.Semaphore(5)

        async def _limited(ref: str):
            async with sem:
                return await self._resolve_one(ref)

        entries = await asyncio.gather(*[_limited(ref) for ref in unique])
        for image_ref, entry in zip(unique, entries):
            report.entries.append(entry)
            report.by_image[image_ref] = entry
        return report

    async def resolve_all(
        self,
        projection: DeployProjection,
        set_digest_on_containers: bool = True,
    ) -> ResolutionReport:
        """
        Resolve each unique image in projection. Optionally set resolved_digest_ref on containers.

        Returns ResolutionReport. If any image is not RESOLVED, caller should fail-fast when
        abort_on_unresolved_images is True.
        """
        unique_images: dict[str, list[DeployContainer]] = {}
        for c in projection.containers:
            ref = c.image
            if ref:
                unique_images.setdefault(ref, []).append(c)

        report = ResolutionReport()
        # Collect all digest assignments before mutating any container so that
        # a mid-batch exception leaves the projection in a consistent state.
        digest_assignments: dict[str, str] = {}

        # Resolve all images concurrently with rate-limit protection.
        image_ref_list = list(unique_images.keys())
        sem = asyncio.Semaphore(5)

        async def _limited(ref: str):
            async with sem:
                return await self._resolve_one(ref)

        entries = await asyncio.gather(*[_limited(ref) for ref in image_ref_list])
        for image_ref, entry in zip(image_ref_list, entries):
            report.entries.append(entry)
            report.by_image[image_ref] = entry
            # Only record a digest_ref that is an actual pinned digest (@sha256:).
            if (
                set_digest_on_containers
                and entry.status == ResolutionStatus.RESOLVED
                and entry.digest_ref
                and "@sha256:" in entry.digest_ref
            ):
                digest_assignments[image_ref] = entry.digest_ref

        # Apply all digest assignments atomically after all resolutions succeed
        for image_ref, digest_ref in digest_assignments.items():
            for c in unique_images[image_ref]:
                c.resolved_digest_ref = digest_ref

        return report

    async def _resolve_one(self, image_ref: str) -> ImageResolutionEntry:
        """Resolve a single image ref with fallback chain.

        Resolution order:
        1. Local docker image cache
        2. Remote manifest via OCI API (with retry for transient errors)
        3. Multi-registry fallback: ghcr.io, quay.io (for namespaced images only)
        4. :latest tag as last resort
        """
        # 1. Local cache — uses docker image inspect, no network required.
        # Skip short-circuiting if docker itself is not available so that step 2
        # can still validate the image remotely.
        local_entry = await self._resolve_from_local_image(image_ref)
        if local_entry is not None and local_entry.status != ResolutionStatus.REGISTRY_UNSUPPORTED:
            return local_entry

        # 2. Remote manifest via OCI API (with retry)
        entry = await self._resolve_with_retry(image_ref)
        if entry.status == ResolutionStatus.RESOLVED:
            return entry

        # Fallbacks only for hard "not found" errors — not transient network issues.
        _FALLBACK_ELIGIBLE = frozenset({
            ResolutionStatus.NOT_FOUND,
            ResolutionStatus.AUTH_REQUIRED,
            ResolutionStatus.ACCESS_DENIED,
        })
        if entry.status not in _FALLBACK_ELIGIBLE:
            return entry

        original_entry = entry
        registry, repo, reference = _parse_image_ref(image_ref)

        # 3. Multi-registry fallback for namespaced Docker Hub images.
        # Example: bitnami/kafka:3.6 (not on docker.io) → ghcr.io/bitnami/kafka:3.6
        # Skip official single-name images (library/nginx) — they don't migrate.
        if registry == "registry-1.docker.io" and not repo.startswith("library/"):
            # Use the tag from the original ref, not a digest
            tag = reference if not reference.startswith("sha256:") else "latest"
            for fallback_registry in _FALLBACK_REGISTRIES:
                candidate = f"{fallback_registry}/{repo}:{tag}"
                logger.info("Trying registry fallback: %s → %s", image_ref, candidate)
                fb_entry = await self._resolve_with_retry(candidate)
                if fb_entry.status == ResolutionStatus.RESOLVED:
                    fb_entry.message = f"resolved via registry fallback: {candidate}"
                    # Use the fallback candidate as image_ref so downstream
                    # code (Tofu renderer, logs) references the correct registry.
                    fb_entry.image_ref = candidate
                    if not (fb_entry.digest_ref and "@sha256:" in fb_entry.digest_ref):
                        # No digest available — set to None rather than a tag string
                        # to avoid violating the @sha256: contract downstream.
                        fb_entry.digest_ref = None
                    return fb_entry

        # 3b. Namespace alias fallback — when a namespaced image (e.g. bitnami/kafka)
        # is not found on any registry, try the bare image name as an official
        # Docker Hub image (kafka → library/kafka) or well-known alternative
        # namespaces.  This handles cases where the LLM suggests a bitnami image
        # that requires authentication or doesn't exist publicly.
        if registry in ("registry-1.docker.io", "ghcr.io") and "/" in repo:
            bare_name = repo.rsplit("/", 1)[-1]  # "bitnami/kafka" → "kafka"
            tag = reference if not reference.startswith("sha256:") else "latest"
            # Try official Docker Hub image first (e.g. library/kafka:latest)
            alias_candidates = [f"{bare_name}:{tag}", f"{bare_name}:latest"]
            for alias in alias_candidates:
                alias_entry = await self._resolve_with_retry(alias)
                if alias_entry.status == ResolutionStatus.RESOLVED:
                    logger.info(
                        "Namespace alias fallback: %s → %s", image_ref, alias,
                    )
                    alias_entry.image_ref = alias
                    alias_entry.message = f"resolved via namespace alias fallback: {alias}"
                    if not (alias_entry.digest_ref and "@sha256:" in alias_entry.digest_ref):
                        alias_entry.digest_ref = None
                    return alias_entry

        # 4. :latest as last resort — handles cases where only the tag is wrong
        lr_registry, lr_repo, lr_reference = _parse_image_ref(image_ref)
        # Only try :latest if the original ref used an explicit tag (not already "latest" or a digest)
        if lr_reference.startswith("sha256:"):
            latest_ref = image_ref  # digest ref — skip :latest fallback
        elif lr_registry == "registry-1.docker.io":
            # Strip the "library/" prefix for official images to match the input style
            display_repo = lr_repo.removeprefix("library/")
            latest_ref = f"{display_repo}:latest"
        else:
            latest_ref = f"{lr_registry}/{lr_repo}:latest"
        if latest_ref != image_ref:
            logger.info("Trying :latest fallback: %s → %s", image_ref, latest_ref)
            latest_entry = await self._resolve_with_retry(latest_ref)
            if latest_entry.status == ResolutionStatus.RESOLVED:
                latest_entry.message = (
                    f"WARNING: resolved via :latest fallback — original tag "
                    f"'{lr_reference}' not found. The deployed image may differ "
                    f"from the requested version."
                )
                latest_entry.image_ref = latest_ref  # Keep :latest ref for transparency
                logger.warning(
                    "Image %s resolved via :latest fallback — deployed version "
                    "may not match requested tag '%s'",
                    image_ref, lr_reference,
                )
                return latest_entry

        # All fallbacks exhausted — return the original failure entry
        return original_entry

    async def _resolve_with_retry(self, image_ref: str) -> ImageResolutionEntry:
        """OCI manifest check with exponential backoff retry for transient errors."""
        import random

        last_entry: ImageResolutionEntry | None = None
        for attempt in range(1 + self.max_retries):
            entry = await self._manifest_inspect(image_ref)
            if entry.status not in self._TRANSIENT_STATUSES or attempt >= self.max_retries:
                return entry
            last_entry = entry
            base_delay = (2 ** attempt) * 1.0
            jitter = base_delay * 0.25 * (random.random() * 2 - 1)
            delay = max(0.5, base_delay + jitter)
            logger.info(
                "Transient error resolving %s (%s) — retry %d/%d in %.1fs",
                image_ref,
                entry.status.value,
                attempt + 1,
                self.max_retries,
                delay,
            )
            await asyncio.sleep(delay)

        return last_entry or ImageResolutionEntry(
            image_ref=image_ref,
            status=ResolutionStatus.UNEXPECTED_STATUS,
            message="retry loop exhausted",
        )

    async def _manifest_inspect(self, image_ref: str) -> ImageResolutionEntry:
        """Check image existence via OCI Distribution Spec HEAD request.

        Replaces ``docker manifest inspect``:
        - HEAD /v2/{repo}/manifests/{ref} does not count toward pull rate limits
        - ``Docker-Content-Digest`` response header gives the registry-authoritative digest
        - Works across all OCI-compliant registries without a Docker daemon
        """
        registry, repo, reference = _parse_image_ref(image_ref)

        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, follow_redirects=True
            ) as client:
                status_code, digest = await self._head_manifest_oci(
                    client, registry, repo, reference
                )
        except httpx.TimeoutException:
            return ImageResolutionEntry(
                image_ref=image_ref,
                status=ResolutionStatus.NETWORK_ERROR,
                message="timeout",
            )
        except httpx.NetworkError as e:
            return ImageResolutionEntry(
                image_ref=image_ref,
                status=ResolutionStatus.NETWORK_ERROR,
                message=str(e)[:200],
            )
        except httpx.InvalidURL:
            return ImageResolutionEntry(
                image_ref=image_ref,
                status=ResolutionStatus.INVALID_REFERENCE,
                message=f"invalid registry URL derived from ref: {image_ref!r}",
            )
        except Exception as e:
            return ImageResolutionEntry(
                image_ref=image_ref,
                status=ResolutionStatus.UNEXPECTED_STATUS,
                message=str(e)[:200],
            )

        base = image_ref.split("@")[0]

        if status_code == 200:
            if digest:
                return ImageResolutionEntry(
                    image_ref=image_ref,
                    status=ResolutionStatus.RESOLVED,
                    digest_ref=f"{base}@{digest}",
                )
            return ImageResolutionEntry(
                image_ref=image_ref,
                status=ResolutionStatus.RESOLVED,
                digest_ref=image_ref,
                message="resolved but registry returned no content-digest header",
            )
        if status_code == 404:
            return ImageResolutionEntry(
                image_ref=image_ref,
                status=ResolutionStatus.NOT_FOUND,
                message=f"404 — {registry}/{repo}:{reference}",
            )
        if status_code == 401:
            return ImageResolutionEntry(
                image_ref=image_ref,
                status=ResolutionStatus.AUTH_REQUIRED,
                message="authentication required (private image or auth failure)",
            )
        if status_code == 403:
            return ImageResolutionEntry(
                image_ref=image_ref,
                status=ResolutionStatus.ACCESS_DENIED,
                message="forbidden",
            )
        if status_code == 429:
            return ImageResolutionEntry(
                image_ref=image_ref,
                status=ResolutionStatus.RATE_LIMITED,
                message="rate limited by registry",
            )
        return ImageResolutionEntry(
            image_ref=image_ref,
            status=ResolutionStatus.UNEXPECTED_STATUS,
            message=f"HTTP {status_code}",
        )

    async def _head_manifest_oci(
        self,
        client: httpx.AsyncClient,
        registry: str,
        repo: str,
        reference: str,
    ) -> tuple[int, str | None]:
        """Perform HEAD /v2/{repo}/manifests/{reference} with Bearer auth.

        Implements the standard OCI auth challenge:
          1. Unauthenticated HEAD → 401 with WWW-Authenticate header
          2. Fetch Bearer token from the advertised realm
          3. Retry HEAD with Authorization header

        Returns (http_status_code, digest_or_None).
        """
        url = f"https://{registry}/v2/{repo}/manifests/{reference}"
        headers = {"Accept": _MANIFEST_ACCEPT}

        resp = await client.head(url, headers=headers)

        if resp.status_code != 401:
            digest = resp.headers.get("Docker-Content-Digest") or resp.headers.get(
                "OCI-Content-Digest"
            )
            return resp.status_code, digest

        # 401 → fetch token and retry once
        www_auth = resp.headers.get("WWW-Authenticate", "")
        token = await self._fetch_bearer_token(client, www_auth, registry, repo)
        if not token:
            return 401, None

        auth_headers = {**headers, "Authorization": f"Bearer {token}"}
        resp = await client.head(url, headers=auth_headers)
        digest = resp.headers.get("Docker-Content-Digest") or resp.headers.get(
            "OCI-Content-Digest"
        )
        return resp.status_code, digest

    async def _fetch_bearer_token(
        self,
        client: httpx.AsyncClient,
        www_auth: str,
        registry: str,
        repo: str,
    ) -> str | None:
        """Fetch a Bearer token using the registry's WWW-Authenticate challenge."""
        realm = _parse_www_auth_param(www_auth, "realm")
        service = _parse_www_auth_param(www_auth, "service")
        scope = _parse_www_auth_param(www_auth, "scope")

        # Fallback to well-known endpoints when no realm is advertised
        if not realm:
            if "docker.io" in registry:
                realm = "https://auth.docker.io/token"
                service = "registry.docker.io"
            else:
                realm = f"https://{registry}/v2/auth"

        if not scope:
            scope = f"repository:{repo}:pull"

        params: dict[str, str] = {"scope": scope}
        if service:
            params["service"] = service

        try:
            resp = await client.get(realm, params=params)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("token") or data.get("access_token")
        except Exception as e:
            logger.debug("Bearer token fetch failed for realm %s: %s", realm, e)
        return None

    async def _get_with_auth(
        self,
        client: "httpx.AsyncClient",
        url: str,
        registry: str,
        repo: str,
        headers: dict | None = None,
    ) -> "httpx.Response":
        """GET request with automatic Bearer auth retry on 401."""
        req_headers = dict(headers or {})
        resp = await client.get(url, headers=req_headers)
        if resp.status_code != 401:
            return resp
        www_auth = resp.headers.get("WWW-Authenticate", "")
        token = await self._fetch_bearer_token(client, www_auth, registry, repo)
        if not token:
            return resp
        auth_headers = {**req_headers, "Authorization": f"Bearer {token}"}
        return await client.get(url, headers=auth_headers)

    @staticmethod
    def _select_platform_digest(
        manifests: list[dict],
        os: str = "linux",
        arch: str = "amd64",
    ) -> str | None:
        """Select the digest for a specific platform from a manifest list.

        Falls back to the first entry if no exact match is found.
        """
        for entry in manifests:
            platform = entry.get("platform") or {}
            if platform.get("os") == os and platform.get("architecture") == arch:
                return entry.get("digest")
        # Fallback: first entry (better than nothing)
        if manifests and manifests[0].get("digest"):
            return manifests[0]["digest"]
        return None

    async def _fetch_image_config(self, image_ref: str) -> dict:
        """Fetch image config blob from OCI registry.

        Resolution:
          1. GET manifest  → ``config.digest``
          2. GET blob      → parse ``config`` section for Env/Cmd/Entrypoint/ExposedPorts

        Returns a plain dict suitable for JSON serialisation as a tool result.
        """
        registry, repo, reference = _parse_image_ref(image_ref)
        manifest_accept = (
            "application/vnd.oci.image.index.v1+json,"
            "application/vnd.docker.distribution.manifest.list.v2+json,"
            "application/vnd.oci.image.manifest.v1+json,"
            "application/vnd.docker.distribution.manifest.v2+json"
        )
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, follow_redirects=True
            ) as client:
                # Step 1: fetch manifest to locate config blob digest
                manifest_url = f"https://{registry}/v2/{repo}/manifests/{reference}"
                resp = await self._get_with_auth(
                    client, manifest_url, registry, repo,
                    headers={"Accept": manifest_accept},
                )
                if resp.status_code != 200:
                    return {"error": f"manifest fetch failed: HTTP {resp.status_code}"}
                try:
                    manifest = resp.json()
                except Exception:
                    return {"error": "manifest body is not valid JSON"}

                config_digest = (manifest.get("config") or {}).get("digest")

                # Handle manifest lists (multi-arch images like nginx, postgres, etc.)
                if not config_digest and isinstance(manifest.get("manifests"), list):
                    platform_digest = self._select_platform_digest(manifest["manifests"])
                    if not platform_digest:
                        return {"error": "manifest list has no linux/amd64 entry"}
                    # Fetch the platform-specific manifest
                    plat_url = f"https://{registry}/v2/{repo}/manifests/{platform_digest}"
                    plat_accept = (
                        "application/vnd.oci.image.manifest.v1+json,"
                        "application/vnd.docker.distribution.manifest.v2+json"
                    )
                    plat_resp = await self._get_with_auth(
                        client, plat_url, registry, repo,
                        headers={"Accept": plat_accept},
                    )
                    if plat_resp.status_code != 200:
                        return {"error": f"platform manifest fetch failed: HTTP {plat_resp.status_code}"}
                    try:
                        plat_manifest = plat_resp.json()
                    except Exception:
                        return {"error": "platform manifest is not valid JSON"}
                    config_digest = (plat_manifest.get("config") or {}).get("digest")

                if not config_digest:
                    return {"error": "manifest has no config digest"}

                # Step 2: fetch config blob
                blob_url = f"https://{registry}/v2/{repo}/blobs/{config_digest}"
                resp = await self._get_with_auth(client, blob_url, registry, repo)
                if resp.status_code != 200:
                    return {"error": f"config blob fetch failed: HTTP {resp.status_code}"}
                try:
                    config_blob = resp.json()
                except Exception:
                    return {"error": "config blob is not valid JSON"}

        except httpx.TimeoutException:
            return {"error": "timeout fetching image config"}
        except httpx.NetworkError as exc:
            return {"error": f"network error: {exc!s:.200}"}
        except Exception as exc:
            return {"error": str(exc)[:200]}

        container_cfg = config_blob.get("config") or config_blob.get("Config") or {}
        exposed = list(container_cfg.get("ExposedPorts") or {})
        return {
            "env": container_cfg.get("Env") or [],
            "cmd": container_cfg.get("Cmd") or [],
            "entrypoint": container_cfg.get("Entrypoint") or [],
            "exposed_ports": exposed,
            "working_dir": container_cfg.get("WorkingDir") or "",
        }

    async def _resolve_from_local_image(
        self, image_ref: str
    ) -> Optional[ImageResolutionEntry]:
        """Resolve from a locally present image before contacting the registry."""
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    [
                        "docker",
                        "image",
                        "inspect",
                        image_ref,
                        "--format",
                        "{{json .RepoDigests}}",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                ),
            )
        except FileNotFoundError:
            # Docker not installed — signal caller to skip to OCI API path
            return ImageResolutionEntry(
                image_ref=image_ref,
                status=ResolutionStatus.REGISTRY_UNSUPPORTED,
                message="docker not found",
            )
        except subprocess.TimeoutExpired:
            return None
        except Exception:
            return None

        if result.returncode != 0:
            return None

        stdout = (result.stdout or "").strip()
        if stdout and stdout != "null":
            try:
                repo_digests = json.loads(stdout)
            except Exception:
                repo_digests = []
            if isinstance(repo_digests, list) and repo_digests:
                first = str(repo_digests[0])
                if "@sha256:" in first:
                    return ImageResolutionEntry(
                        image_ref=image_ref,
                        status=ResolutionStatus.RESOLVED,
                        digest_ref=first,
                        message="resolved from local image cache",
                    )

        return ImageResolutionEntry(
            image_ref=image_ref,
            status=ResolutionStatus.RESOLVED,
            digest_ref=image_ref,
            message="local image present but no repo digest available",
        )
