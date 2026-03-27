"""
TofuRenderer - Deterministic OpenTofu JSON (.tofu.json) from DeployProjection.

Produces programmatic IaC only from validated World Model + resolved image refs.
No LLM; same projection + digest refs always yield same JSON.
"""

import json
import logging
import os
import platform
import re
from pathlib import Path
from typing import Optional

from .models import DeployProjection

logger = logging.getLogger(__name__)

IS_WINDOWS = platform.system() == "Windows"
DOCKER_HOST = "npipe:////./pipe/docker_engine" if IS_WINDOWS else "unix:///var/run/docker.sock"


class TofuRenderer:
    """
    Render DeployProjection to OpenTofu JSON configuration.

    Uses resolved_digest_ref when set (image@sha256:...), else image.
    Output: .tofu.json with terraform, provider, docker_network, docker_volume,
    docker_image, docker_container.
    """

    def __init__(self, work_dir: Optional[Path] = None):
        self.work_dir = Path(work_dir) if work_dir else None

    def render(
        self,
        projection: DeployProjection,
        output_path: Optional[Path] = None,
    ) -> str:
        """
        Render projection to OpenTofu JSON. Returns JSON string.
        If output_path (and work_dir) set, also writes file.
        """
        config = self._build_config(projection)
        out = json.dumps(config, indent=2, sort_keys=False)
        if output_path is not None:
            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(out, encoding="utf-8")
            logger.info("Wrote OpenTofu JSON to %s", output_path)
        elif self.work_dir is not None:
            path = self.work_dir / "main.tofu.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(out, encoding="utf-8")
            logger.info("Wrote OpenTofu JSON to %s", path)
        return out

    def _build_config(self, projection: DeployProjection) -> dict:
        docker_host = self._resolve_docker_host(getattr(projection.constraints, "docker_host", None))
        network_resource_names = self._allocate_resource_names(net.name for net in projection.networks)
        volume_resource_names = self._allocate_resource_names(projection.volumes)
        container_resource_names = self._allocate_resource_names(
            container.name for container in projection.containers
        )
        actual_container_names = set(container_resource_names)
        for container in projection.containers:
            if container.name.startswith("hn_"):
                alias = container.name[3:]
                if alias and alias not in actual_container_names:
                    if alias in container_resource_names:
                        logger.warning(
                            "Alias collision: '%s' already maps to '%s', skipping alias for '%s'",
                            alias, container_resource_names[alias], container.name,
                        )
                    else:
                        container_resource_names[alias] = container_resource_names[container.name]

        root: dict = {
            "terraform": {
                "required_providers": {
                    "docker": {
                        "source": "registry.terraform.io/kreuzwerker/docker",
                        "version": projection.constraints.provider_version,
                    }
                }
            },
            "provider": [
                {
                    "docker": [
                        {"host": docker_host}
                    ]
                }
            ],
            "resource": {
                "docker_network": {},
                "docker_volume": {},
                "docker_image": {},
                "docker_container": {},
            },
        }

        for net in projection.networks:
            resource_name = network_resource_names[net.name]
            root["resource"]["docker_network"][resource_name] = {
                "name": net.name,
                "driver": net.driver,
                "internal": net.internal,
            }

        for volume_name in projection.volumes:
            resource_name = volume_resource_names[volume_name]
            root["resource"]["docker_volume"][resource_name] = {
                "name": volume_name,
            }

        for container in projection.containers:
            image_ref = container.resolved_digest_ref or container.image
            safe_name = container_resource_names[container.name]
            root["resource"]["docker_image"][safe_name] = {
                "name": image_ref,
                "keep_locally": True,
            }

            container_block: dict = {
                "name": container.name,
                "image": f"${{docker_image.{safe_name}.image_id}}",
            }

            # Aliases pro Netzwerk zusammenbauen
            networks_advanced = []
            for network_name in container.networks:
                if network_name not in network_resource_names:
                    logger.warning(
                        "Container '%s' references network '%s' not in projection — skipping",
                        container.name, network_name,
                    )
                    continue
                net_entry: dict = {
                    "name": f"${{docker_network.{network_resource_names[network_name]}.name}}"
                }
                aliases = (container.network_aliases or {}).get(network_name, [])
                if aliases:
                    net_entry["aliases"] = list(aliases)
                networks_advanced.append(net_entry)

            # [G5-FIX] networks_advanced + network_mode combined is invalid Docker config
            if container.network_mode:
                container_block["network_mode"] = container.network_mode
                # Do NOT set networks_advanced — combining it with network_mode is invalid
            else:
                if networks_advanced:
                    container_block["networks_advanced"] = networks_advanced
            if container.hostname:
                container_block["hostname"] = container.hostname

            if container.ports:
                rendered_ports = []
                for port in container.ports:
                    port_block = {
                        "internal": port.internal,
                        "external": port.external,
                        "protocol": port.protocol,
                    }
                    if port.ip is not None:
                        port_block["ip"] = port.ip
                    rendered_ports.append(port_block)
                container_block["ports"] = rendered_ports
            if container.env:
                container_block["env"] = list(container.env)
            if container.volumes:
                rendered_volumes = []
                for volume_spec in container.volumes:
                    rendered = self._render_volume_spec(volume_spec, volume_resource_names)
                    if rendered:
                        rendered_volumes.append(rendered)
                if rendered_volumes:
                    container_block["volumes"] = rendered_volumes
            if container.depends_on:
                resolved: list[str] = []
                for dep_name in container.depends_on:
                    if dep_name in container_resource_names:
                        resolved.append(f"docker_container.{container_resource_names[dep_name]}")
                    else:
                        logger.warning(
                            "depends_on target %r not found in rendered containers for %r — skipping edge",
                            dep_name,
                            container.name,
                        )
                if resolved:
                    container_block["depends_on"] = resolved
            if container.command:
                container_block["command"] = list(container.command)

            # Docker provider JSON syntax expects repeated labels blocks.
            container_block["labels"] = [
                {
                    "label": "honeynet.project",
                    "value": projection.project_name,
                },
                {
                    "label": "honeynet.managed",
                    "value": "true",
                },
            ]

            if getattr(container, "healthcheck", None):
                hc = container.healthcheck
                # Normalize test field: bare strings must be wrapped in CMD-SHELL list
                hc_test = hc.get("test", ["NONE"])
                if isinstance(hc_test, str):
                    hc_test = ["CMD-SHELL", hc_test]
                container_block["healthcheck"] = [{
                    "test": hc_test,
                    "interval": hc.get("interval", "30s"),
                    "timeout": hc.get("timeout", "10s"),
                    "retries": hc.get("retries", 3),
                    "start_period": hc.get("start_period", "0s"),
                }]
            root["resource"]["docker_container"][safe_name] = container_block

        if not root["resource"]["docker_volume"]:
            del root["resource"]["docker_volume"]

        return root

    def _resolve_docker_host(self, configured_host: Optional[str]) -> str:
        """Normalize docker host for the current platform."""
        host = (configured_host or "").strip()
        if not host:
            return DOCKER_HOST
        if IS_WINDOWS and host.startswith("unix://"):
            return DOCKER_HOST
        if not IS_WINDOWS and host.startswith("npipe://"):
            return DOCKER_HOST
        return host

    def _render_volume_spec(self, volume_spec: str, volume_resource_names: dict[str, str]) -> Optional[dict]:
        """Render compiler volume spec `name:/path[:ro]` into docker_container.volumes.

        Windows drive-letter-aware: C:\\path\\...:/container/path[:ro] is parsed
        correctly — parts[0]=='C' followed by parts[1]=='\\path\\...' are rejoined.
        """
        if not volume_spec or ":" not in volume_spec:
            return None

        parts = volume_spec.split(":")
        read_only = parts[-1].strip().lower() == "ro" if len(parts) >= 3 else False
        if read_only:
            parts = parts[:-1]
        if len(parts) < 2:
            return None

        # Windows drive-letter-aware path detection (e.g. C:\path\...)
        if len(parts) >= 3 and len(parts[0]) == 1 and parts[0].isalpha():
            source = parts[0] + ":" + parts[1]
            container_path = ":".join(parts[2:])
        else:
            source = parts[0]
            container_path = ":".join(parts[1:])

        source = source.strip()
        container_path = container_path.strip()
        if not source or not container_path:
            return None

        block: dict[str, object] = {"container_path": container_path}
        if read_only:
            block["read_only"] = True

        if os.path.isabs(source):
            block["host_path"] = source
        elif source in volume_resource_names:
            block["volume_name"] = f"${{docker_volume.{volume_resource_names[source]}.name}}"
        else:
            block["volume_name"] = source
        return block

    @classmethod
    def _allocate_resource_names(cls, names) -> dict[str, str]:
        allocated: dict[str, str] = {}
        used: set[str] = set()
        for raw_name in names:
            base_name = cls._safe_resource_name(raw_name)
            candidate = base_name
            suffix = 2
            while candidate in used:
                candidate = f"{base_name}__{suffix}"
                suffix += 1
            allocated[raw_name] = candidate
            used.add(candidate)
        return allocated

    @staticmethod
    def _safe_resource_name(name: str) -> str:
        """Sanitize name for Terraform resource identifier."""
        sanitized = re.sub(r"[^0-9A-Za-z_]+", "_", str(name or "").strip())
        sanitized = sanitized.strip("_")
        if not sanitized:
            sanitized = "resource"
        if sanitized[0].isdigit():
            sanitized = f"r_{sanitized}"
        return sanitized
