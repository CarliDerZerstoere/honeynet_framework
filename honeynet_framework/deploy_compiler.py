"""
Deploy Projection Compiler - Transforms WorldModel to deployment-ready DeployProjection.

Two phases:
1. EXTRACT - Pull deploy data from WorldModel zones and systems
2. BUILD IR - Produce DeployProjection with networks, containers, port mappings

Key Invariant: This is a PURE function - same WorldModel always produces same DeployProjection.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from .models import (
    WorldModel,
    DeployProjection,
    DeployNetwork,
    DeployContainer,
    Port,
    TerraformConstraints,
)

logger = logging.getLogger(__name__)

_BACKBONE_NETWORK = "honeynet_backbone"


@dataclass
class CompilerConfig:
    """Configuration for the deploy compiler."""
    container_prefix: str = "hn_"
    backbone_network_name: str = _BACKBONE_NETWORK
    project_name_override: str = ""


class DeployCompiler:
    """Compile a WorldModel into a DeployProjection."""

    def __init__(self, config: Optional[CompilerConfig] = None):
        self.config = config or CompilerConfig()
        self._seeded_host_ports: set[int] = set()
        self._used_host_ports: set[int] = set()  # reset at start of each compile()

    def seed_used_ports(self, ports: set[int]) -> None:
        """Pre-seed host ports known to be in use (e.g. from ``docker ps``).

        Called before ``compile()`` so that allocated external ports don't
        collide with already-running containers on the host.  Seeded ports
        are merged into each ``compile()`` call but compile-internal
        allocations are **not** retained between calls — ensuring that the
        same WorldModel always produces the same DeployProjection (idempotent).
        """
        self._seeded_host_ports.update(ports)
        # Also add to _used_host_ports so _allocate_host_port works
        # correctly even if called outside compile() (e.g. in tests).
        self._used_host_ports.update(ports)

    def reset_seed(self) -> None:
        """Clear the persisted seed set.

        Call this before ``seed_used_ports()`` when the host port state has
        changed (e.g. after ``tofu destroy`` freed containers) so that stale
        ports do not permanently pollute subsequent ``compile()`` calls.
        """
        self._seeded_host_ports.clear()
        self._used_host_ports.clear()

    def compile(self, world_model: WorldModel) -> DeployProjection:
        """Transform WorldModel into a deployment-ready DeployProjection.

        Port allocation state is strictly local to each compile() call:
        seeded ports are included but prior compile-internal allocations
        are discarded.  This guarantees idempotent output for the same input.
        """
        # Start fresh from seeded-only ports — discard any leftover state
        self._used_host_ports: set[int] = set(self._seeded_host_ports)

        project_name = self.config.project_name_override or world_model.project_name
        prefix = self.config.container_prefix

        # Build networks from zones (dedupe by final Docker network name)
        networks_by_name: dict[str, DeployNetwork] = {}
        zone_to_network: dict[str, str] = {}

        for zone_name, zone in world_model.zones.items():
            net_name = zone.deploy.network_name or zone_name
            # Prefix to avoid Docker collisions
            full_net_name = f"{project_name}_{net_name}"
            if full_net_name not in networks_by_name:
                networks_by_name[full_net_name] = DeployNetwork(
                    name=full_net_name,
                    driver=zone.deploy.driver,
                    internal=zone.deploy.internal,
                    zone_name=zone_name,
                )
            zone_to_network[zone_name] = full_net_name

        networks: list[DeployNetwork] = list(networks_by_name.values())

        # Add backbone network (connects all containers)
        backbone = f"{project_name}_{self.config.backbone_network_name}"
        if backbone in networks_by_name:
            backbone = f"{backbone}_backbone"
        networks.append(DeployNetwork(
            name=backbone,
            driver="bridge",
            internal=False,
            zone_name="",
        ))

        # Build containers from systems
        containers: list[DeployContainer] = []
        volumes: list[str] = []
        # Track system_name → initial container_name for depends_on remapping
        sys_to_container: dict[str, str] = {}

        for sys_name, system in world_model.systems.items():
            if not system.deploy:
                continue

            container_name = f"{prefix}{_safe_name(sys_name)}"
            sys_to_container[sys_name] = container_name
            zone_network = zone_to_network.get(system.deploy.zone)

            # Container networks: own zone only.
            # Backbone is the fallback for systems without an assigned zone.
            # Cross-zone communication is established via explicit depends_on:
            # a container that depends on a service in another zone is joined to
            # that zone's network so the two can reach each other by name.
            if zone_network is None:
                if system.deploy.zone:
                    # Zone name is set but not found in world_model.zones — likely
                    # an extraction typo.  Log so it's visible in run output.
                    logger.warning(
                        "System '%s' references undefined zone '%s' — "
                        "falling back to backbone network",
                        sys_name,
                        system.deploy.zone,
                    )
                container_networks = [backbone]
            else:
                container_networks = [zone_network]
                for dep_name in system.deploy.depends_on:
                    dep_sys = world_model.systems.get(dep_name)
                    if dep_sys and dep_sys.deploy:
                        dep_net = zone_to_network.get(dep_sys.deploy.zone)
                        if dep_net and dep_net not in container_networks:
                            container_networks.append(dep_net)

            # Network aliases for service discovery
            aliases: dict[str, list[str]] = {}
            alias_name = _safe_name(sys_name)
            for net in container_networks:
                aliases[net] = [alias_name]

            # Hostname from simulate section or system name
            hostname = None
            if system.simulate and system.simulate.hostname:
                hostname = system.simulate.hostname

            # Port mappings
            ports: list[Port] = []
            zone = world_model.zones.get(system.deploy.zone)
            should_publish = zone is not None and not zone.deploy.internal

            for internal_port in system.deploy.ports:
                if should_publish:
                    external = self._allocate_host_port(internal_port)
                    ports.append(Port(internal=internal_port, external=external))
                else:
                    # Internal-only zones don't publish ports to the host
                    pass

            # Volumes
            container_volumes: list[str] = []
            for vol in system.deploy.volumes:
                vol_name = f"{project_name}_{vol.name}"
                if vol_name not in volumes:
                    volumes.append(vol_name)
                spec = f"{vol_name}:{vol.path}"
                if vol.readonly:
                    spec += ":ro"
                container_volumes.append(spec)

            # depends_on → prefixed container names
            depends_on = []
            for dep in system.deploy.depends_on:
                if dep in world_model.systems:
                    depends_on.append(f"{prefix}{_safe_name(dep)}")
                else:
                    logger.warning(
                        "System '%s' depends_on '%s' which does not exist — dropping dependency",
                        sys_name, dep,
                    )

            containers.append(DeployContainer(
                name=container_name,
                image=system.deploy.image,
                networks=container_networks,
                env=list(system.deploy.env),
                ports=ports,
                volumes=container_volumes,
                depends_on=depends_on,
                command=system.deploy.command,
                network_aliases=aliases,
                hostname=hostname,
                healthcheck=system.deploy.healthcheck,
            ))

        # Detect container name collisions (can happen when _safe_name maps
        # different system names to the same sanitized form)
        # Build reverse map BEFORE iteration to preserve all original mappings.
        # Use explicit loop (not dict comprehension) so that the first system to
        # produce a given container name is preserved — a comprehension would
        # silently overwrite with the last entry when two names collide.
        sys_by_container: dict[str, str] = {}
        for _sname, _cname in sys_to_container.items():
            if _cname not in sys_by_container:
                sys_by_container[_cname] = _sname
        seen_names: dict[str, str] = {}  # container_name → system_name that claimed it first
        old_to_new: dict[str, str] = {}  # tracks renames for depends_on fixup
        for container in containers:
            original_name = container.name
            current_sys = sys_by_container.get(original_name, original_name)
            if container.name in seen_names:
                logger.warning(
                    "Container name collision: '%s' produced by both system '%s' and '%s'. "
                    "Appending suffix to disambiguate.",
                    container.name,
                    seen_names[container.name],
                    current_sys,
                )
                suffix = 2
                while f"{container.name}_{suffix}" in seen_names:
                    suffix += 1
                container.name = f"{container.name}_{suffix}"
            if container.name != original_name:
                old_to_new[original_name] = container.name
            seen_names[container.name] = current_sys

        # Remap depends_on references and fix network aliases for collision-renamed containers
        if old_to_new:
            for container in containers:
                container.depends_on = [
                    old_to_new.get(dep, dep) for dep in container.depends_on
                ]
                # Fix network aliases: if the container was renamed, update its
                # alias to the new unique name so Docker doesn't get duplicates.
                if container.name in [v for v in old_to_new.values()]:
                    new_alias = container.name.removeprefix(prefix)
                    container.network_aliases = {
                        net: [new_alias] for net in container.network_aliases
                    }

        # Build bidirectional container↔system mapping (accounts for renames)
        container_to_system: dict[str, str] = {}
        for sys_name, cname in sys_to_container.items():
            final_name = old_to_new.get(cname, cname)
            container_to_system[final_name] = sys_name

        return DeployProjection(
            project_name=project_name,
            networks=networks,
            containers=containers,
            volumes=volumes,
            constraints=TerraformConstraints(),
            backbone_network_name=backbone,
            container_to_system=container_to_system,
        )

    def _allocate_host_port(self, preferred: int) -> int:
        """Allocate a unique host port, starting from preferred.

        Searches the full usable port space (1024–65535) starting from
        *preferred*, wrapping around once.  Raises RuntimeError only when
        the entire usable port space is exhausted.
        """
        _MIN_PORT = 1024
        _MAX_PORT = 65535
        port = max(_MIN_PORT, min(preferred, _MAX_PORT))
        start = port
        # Scan upward; after reaching the max, wrap to _MIN_PORT and continue
        # up to but not including the starting point.
        while port in self._used_host_ports:
            port += 1
            if port > _MAX_PORT:
                port = _MIN_PORT
            if port == start:
                raise RuntimeError(
                    f"Cannot allocate host port: all usable ports "
                    f"({_MIN_PORT}–{_MAX_PORT}) are exhausted "
                    f"({len(self._used_host_ports)} ports in use)"
                )
        self._used_host_ports.add(port)
        return port


def _safe_name(name: str) -> str:
    """Sanitize a name for use in Docker container/network names."""
    sanitized = re.sub(r"[^a-z0-9_]", "_", str(name).lower().strip())
    sanitized = re.sub(r"_+", "_", sanitized).strip("_")
    return sanitized or "system"
