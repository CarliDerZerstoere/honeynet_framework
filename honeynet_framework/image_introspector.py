"""
Dynamic image introspection via OCI registry.

Replaces hardcoded image classification lists (BASE_IMAGE_PREFIXES,
MUST_USE_DEFAULT_ENTRYPOINT, CONFIG_REQUIRED_IMAGES) with live data
from the OCI Distribution API.

Usage::

    profile = await get_image_profile("postgres:16-alpine")
    profile.has_daemon_entrypoint   # True — postgres has a real daemon
    profile.required_env            # ["POSTGRES_PASSWORD"] — empty default
    profile.exposed_ports           # ["5432/tcp"]
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .image_resolver import fetch_image_config

logger = logging.getLogger(__name__)

# Interpreter binaries — universal, not image-specific.
# If an image's entrypoint is one of these, it's a base runtime that
# needs either application code or an explicit keep-alive command.
_INTERPRETERS: frozenset[str] = frozenset({
    "python", "python3", "python3.10", "python3.11", "python3.12", "python3.13",
    "node", "ruby", "irb", "php", "perl",
    "sh", "bash", "/bin/sh", "/bin/bash",
    "/usr/bin/python", "/usr/bin/python3",
    "/usr/local/bin/python", "/usr/local/bin/python3",
    "/usr/local/bin/node",
})


@dataclass(frozen=True)
class ImageProfile:
    """Dynamically derived profile of a container image.

    Built from OCI registry data (Entrypoint, Cmd, Env, ExposedPorts).
    All classification properties are derived — no hardcoded image lists.
    """

    image_ref: str
    entrypoint: tuple[str, ...]
    cmd: tuple[str, ...]
    env: tuple[str, ...]
    exposed_ports: tuple[str, ...]
    working_dir: str
    error: str  # empty string when OK

    @property
    def available(self) -> bool:
        """True when the OCI config was fetched successfully."""
        return not self.error

    @property
    def has_daemon_entrypoint(self) -> bool:
        """True when the image has a non-interpreter entrypoint.

        Images like nginx, postgres, redis have entrypoints that start
        a long-running daemon.  No command override is needed.
        """
        if not self.available or not self.entrypoint:
            return False
        first = self.entrypoint[0].rsplit("/", 1)[-1].lower()
        return first not in _INTERPRETERS

    @property
    def is_base_runtime(self) -> bool:
        """True when the entrypoint is an interpreter (python, node, sh, ...).

        These images ship a language runtime but no application code.
        Running them without a command starts an interactive REPL that
        exits immediately without a TTY.
        """
        if not self.available or not self.entrypoint:
            return False
        first = self.entrypoint[0].rsplit("/", 1)[-1].lower()
        return first in _INTERPRETERS

    @property
    def needs_command(self) -> bool:
        """True when the image has neither Entrypoint nor Cmd.

        Rare — utility/scratch images.  A command must be provided
        or the container won't start.
        """
        return self.available and not self.entrypoint and not self.cmd

    @property
    def required_env(self) -> list[str]:
        """Environment variables that have an empty default value.

        These are typically REQUIRED by the image to start (e.g.
        POSTGRES_PASSWORD, MYSQL_ROOT_PASSWORD).
        """
        required = []
        for entry in self.env:
            if "=" not in entry:
                continue
            key, _, value = entry.partition("=")
            if not value.strip():
                required.append(key)
        return required

    @property
    def likely_backend_service(self) -> bool:
        """True if exposed ports suggest a non-public backend service.

        Derived from OCI ExposedPorts — images that expose only database,
        queue, or internal-protocol ports are unlikely to be web-facing.
        """
        if not self.available or not self.exposed_ports:
            return False
        web_ports = {80, 443, 8080, 8443, 3000, 9090}
        exposed: set[int] = set()
        for p in self.exposed_ports:
            try:
                exposed.add(int(str(p).split("/")[0]))
            except (ValueError, IndexError):
                pass
        return bool(exposed) and not exposed.intersection(web_ports)


def _make_error_profile(image_ref: str, error: str) -> ImageProfile:
    """Create a profile representing a failed introspection."""
    return ImageProfile(
        image_ref=image_ref,
        entrypoint=(),
        cmd=(),
        env=(),
        exposed_ports=(),
        working_dir="",
        error=error,
    )


async def get_image_profile(image_ref: str, timeout: int = 15) -> ImageProfile:
    """Fetch and classify an image from the OCI registry.

    Returns an ImageProfile with ``available=True`` on success, or
    ``available=False`` with the error message on failure.
    Uses the cached ``fetch_image_config()`` under the hood.
    """
    if not image_ref or not image_ref.strip():
        return _make_error_profile(image_ref or "", "empty image reference")

    config = await fetch_image_config(image_ref, timeout=timeout)

    if "error" in config:
        logger.debug("Image introspection failed for %s: %s", image_ref, config["error"])
        return _make_error_profile(image_ref, config["error"])

    return ImageProfile(
        image_ref=image_ref,
        entrypoint=tuple(config.get("entrypoint") or []),
        cmd=tuple(config.get("cmd") or []),
        env=tuple(config.get("env") or []),
        exposed_ports=tuple(config.get("exposed_ports") or []),
        working_dir=config.get("working_dir") or "",
        error="",
    )


async def get_image_profiles(image_refs: list[str]) -> dict[str, ImageProfile]:
    """Fetch profiles for multiple images.  Returns {image_ref: profile}."""
    import asyncio

    async def _fetch(ref: str) -> tuple[str, ImageProfile | Exception]:
        try:
            return ref, await get_image_profile(ref)
        except Exception as exc:
            return ref, exc

    raw_results = await asyncio.gather(*[_fetch(ref) for ref in set(image_refs)])
    profiles: dict[str, ImageProfile] = {}
    for ref, result in raw_results:
        if isinstance(result, Exception):
            logger.warning("OCI profile fetch failed for %s: %s", ref, result)
        else:
            profiles[ref] = result
    return profiles
