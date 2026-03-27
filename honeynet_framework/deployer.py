"""
Terraform Deployer - Deploy infrastructure using OpenTofu/Terraform.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import re
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional



logger = logging.getLogger(__name__)

IS_WINDOWS = platform.system() == "Windows"


def find_tofu_binary() -> str:
    """Find tofu or terraform binary.

    Override via environment (no code edit needed when PATH is stale in IDEs):
    ``HONEYNET_TOFU_BINARY`` or ``TOFU_BINARY`` — absolute path to ``tofu.exe`` / ``terraform.exe``.
    """
    for env_key in ("HONEYNET_TOFU_BINARY", "TOFU_BINARY"):
        raw = (os.environ.get(env_key) or "").strip().strip('"')
        if not raw:
            continue
        candidate = Path(raw)
        if candidate.is_file():
            return str(candidate)

    # Check for tofu first
    for name in ["tofu", "tofu.exe"]:
        if shutil.which(name):
            return name

    # Fall back to terraform
    for name in ["terraform", "terraform.exe"]:
        if shutil.which(name):
            return name

    # Default to tofu, will fail with clear error if not found
    return "tofu"


@dataclass
class CommandResult:
    """Result of a command execution."""
    success: bool
    stdout: str
    stderr: str
    exit_code: int


def _truncate_head_tail(text: str, max_chars: int) -> str:
    """Keep start and end of long strings so errors near the tail remain visible."""
    s = text or ""
    if len(s) <= max_chars:
        return s
    sep = "\n...[truncated]...\n"
    half = max(1, (max_chars - len(sep)) // 2)
    return s[:half] + sep + s[-half:]


def command_result_to_dict(cr: CommandResult | None, *, max_text: int = 8000) -> dict[str, Any] | None:
    """Serialize a stage result for deployer_stages.json (truncate long streams)."""
    if cr is None:
        return None
    return {
        "success": cr.success,
        "exit_code": cr.exit_code,
        "stdout": _truncate_head_tail(cr.stdout or "", max_text),
        "stderr": _truncate_head_tail(cr.stderr or "", max_text),
    }


def serialize_stage_results(stage_results: dict[str, CommandResult | None]) -> dict[str, Any]:
    """Map stage name → serialized command result or null."""
    return {k: command_result_to_dict(v) for k, v in stage_results.items()}


@dataclass
class DeployerConfig:
    """Configuration for the deployer."""
    work_dir: Path
    use_docker: bool = False  # Run tofu inside Docker container (default False for local)
    docker_container: str = "opentofu"  # Name of the tofu container
    tofu_binary: str = ""  # Path to tofu binary if not using Docker (auto-detected if empty)
    timeout: int = 900  # Command timeout in seconds
    auto_approve: bool = True
    enable_image_preflight: bool = True
    enable_image_prepull: bool = True
    upgrade_providers_on_init: bool = False
    fail_on_fmt_error: bool = False

    def __post_init__(self):
        """Auto-detect tofu binary if not specified."""
        if not self.tofu_binary:
            self.tofu_binary = find_tofu_binary()


class TerraformDeployer:
    """Deploy infrastructure using OpenTofu/Terraform."""

    def __init__(self, config: DeployerConfig):
        self.config = config
        self.work_dir = config.work_dir
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.last_stage_results: dict[str, CommandResult | None] = {
            "fmt": None,
            "init": None,
            "validate": None,
            "plan": None,
            "apply": None,
        }

    async def check_prerequisites(self) -> tuple[bool, str]:
        """Check if tofu/terraform is available."""
        if self.config.use_docker:
            # Check if Docker is available and container exists
            result = await self._run_docker_cmd(["version"])
            if not result.success:
                return False, "Docker is not available or not running"

            result = await self._run_docker_cmd(["container", "inspect", self.config.docker_container])
            if not result.success:
                return False, f"Docker container '{self.config.docker_container}' not found"

            return True, f"Using Docker container: {self.config.docker_container}"
        else:
            # Check if tofu/terraform is available
            binary = self.config.tofu_binary
            if not shutil.which(binary):
                return False, f"'{binary}' not found in PATH. Please install OpenTofu or Terraform."

            result = await self._run_process([binary, "version"], cwd=str(self.work_dir))
            if not result.success:
                return False, f"'{binary}' found but failed to run: {result.stderr}"

            # Extract version info
            version_line = result.stdout.strip().split('\n')[0] if result.stdout else "unknown"
            return True, f"Using local {binary}: {version_line}"

    async def tofu_version_line(self) -> str:
        """Short label for observability (first line of ``tofu version``)."""
        if self.config.use_docker:
            import shlex
            escaped = "cd /workspace && tofu version"
            result = await self._run_docker_exec(["sh", "-c", escaped])
        else:
            binary = self.config.tofu_binary or find_tofu_binary()
            result = await self._run_process([binary, "version"], cwd=str(self.work_dir))
        if result.stdout:
            return result.stdout.strip().split("\n")[0][:500]
        return "unknown"

    async def write_main_tf(self, code: str) -> Path:
        """Write main.tf to the work directory."""
        main_tf = self.work_dir / "main.tf"
        main_tf.write_text(code, encoding="utf-8")
        logger.info(f"Wrote main.tf to {main_tf}")
        return main_tf

    async def preflight_images(self, code: str) -> tuple[str, dict]:
        """
        Validate and warm Docker images before tofu plan/apply.

        Behavior:
        - Extract image references from docker_image resources
        - Deduplicate by image name
        - Prefer local cache, then manifest check
        - Optionally pre-pull each unique remote image once
        """
        if not self.config.enable_image_preflight:
            return code, {
                "checked": 0,
                "fallbacks_applied": 0,
                "pulled": 0,
                "unresolved": [],
                "changed": False,
            }

        preflight_code = code
        preflight_code, keep_locally_updates = self._ensure_keep_locally_true_for_docker_images(
            preflight_code
        )
        resources = self._extract_docker_image_resources(preflight_code)
        if not resources:
            return preflight_code, {
                "checked": 0,
                "fallbacks_applied": 0,
                "pulled": 0,
                "unresolved": [],
                "keep_locally_enforced": keep_locally_updates,
                "changed": False,
            }

        changed = False
        pulled = 0
        unresolved: list[str] = []
        if keep_locally_updates > 0:
            changed = True

        seen_images: set[str] = set()
        for resource_name, image_name in resources.items():
            target_image = image_name
            exists = await self._image_available_or_remote(target_image)
            image_resolvable = bool(exists)
            if not exists:
                unresolved.append(image_name)

            if (
                self.config.enable_image_prepull
                and image_resolvable
                and target_image not in seen_images
            ):
                if not await self._image_exists_locally(target_image):
                    pull_result = await self._run_docker_cmd(["pull", target_image])
                    if pull_result.success:
                        pulled += 1
                        seen_images.add(target_image)  # Only mark as seen on success
                    else:
                        # Do not fail preflight hard; repair loop may still recover.
                        # Don't add to seen_images - allow retry on subsequent resources
                        unresolved.append(target_image)
                else:
                    # Image already exists locally, mark as seen
                    seen_images.add(target_image)

        return preflight_code, {
            "checked": len(resources),
            "fallbacks_applied": 0,
            "pulled": pulled,
            "unresolved": sorted(set(unresolved)),
            "keep_locally_enforced": keep_locally_updates,
            "changed": changed,
        }

    async def _image_exists_locally(self, image_name: str) -> bool:
        """Check if image already exists locally."""
        result = await self._run_docker_cmd(["image", "inspect", image_name])
        return result.success

    async def _image_available_or_remote(self, image_name: str) -> bool:
        """
        Return True if image exists locally or remote manifest resolves.

        If docker manifest inspect is unavailable in this environment,
        fall back to local-only check to avoid hard failure.
        """
        if await self._image_exists_locally(image_name):
            return True

        manifest = await self._run_docker_cmd(["manifest", "inspect", image_name])
        if manifest.success:
            return True

        lower = f"{manifest.stderr}\n{manifest.stdout}".lower()
        if "is not a docker command" in lower or "unknown command" in lower:
            # Environments without `docker manifest` support: keep preflight best-effort.
            return False
        return False

    @staticmethod
    def _extract_docker_image_resources(code: str) -> dict[str, str]:
        """Extract docker_image resource -> image name mapping from HCL or JSON."""
        resources: dict[str, str] = {}
        # Try JSON format first (output of TofuRenderer)
        try:
            data = json.loads(code)
            images = data.get("resource", {}).get("docker_image", {})
            for resource_name, block in images.items():
                name = block.get("name", "")
                if name:
                    resources[resource_name] = name
            if resources:
                return resources
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass
        # Fall back to HCL regex parsing
        for match in re.finditer(
            r'resource\s+"docker_image"\s+"([^"]+)"\s*\{([\s\S]*?)\n\}',
            code,
        ):
            resource_name = match.group(1)
            block = match.group(2)
            name_match = re.search(r'^\s*name\s*=\s*"([^"]+)"', block, flags=re.MULTILINE)
            if name_match:
                resources[resource_name] = name_match.group(1).strip()
        return resources

    @staticmethod
    def _count_braces_in_line(line: str) -> int:
        """
        Count net brace depth change in a line, ignoring braces in strings and comments.
        
        This handles cases like:
        - name = "value with {braces}"
        - command = ["echo", "{json}"]
        - # Comment with { brace
        """
        # Remove string contents FIRST so that '#' inside strings
        # (e.g. image tags "myimage:v1#sha256:abc") is not mistaken
        # for a comment delimiter.
        line = re.sub(r'"(?:[^"\\]|\\.)*"', '""', line)

        # Now remove comments (any '#' remaining is outside strings)
        comment_idx = line.find('#')
        if comment_idx >= 0:
            line = line[:comment_idx]
        
        return line.count("{") - line.count("}")

    @staticmethod
    def _find_block_end(lines: list[str], start: int) -> int:
        """
        Find the end line of an HCL block starting at 'start'.

        Properly handles braces inside strings and comments.
        Returns the line index of the closing brace.
        Also handles the case where the opening '{' is on a subsequent line.
        """
        depth = TerraformDeployer._count_braces_in_line(lines[start])
        end = start
        # If the opening brace is not on the start line, scan forward to find it
        if depth == 0:
            while end + 1 < len(lines):
                end += 1
                depth += TerraformDeployer._count_braces_in_line(lines[end])
                if depth > 0:
                    break
        while depth > 0 and end + 1 < len(lines):
            end += 1
            depth += TerraformDeployer._count_braces_in_line(lines[end])
        return end

    def _ensure_keep_locally_true_for_docker_images(self, code: str) -> tuple[str, int]:
        """
        Ensure every docker_image resource has `keep_locally = true`.

        Returns updated code and number of resources changed.
        """
        resources = self._extract_docker_image_resources(code)
        updated = code
        changes = 0
        for resource_name in resources.keys():
            updated, changed = self._set_docker_image_keep_locally_true(updated, resource_name)
            if changed:
                changes += 1
        return updated, changes

    @staticmethod
    def _set_docker_image_keep_locally_true(code: str, resource_name: str) -> tuple[str, bool]:
        """Set or insert keep_locally=true in one docker_image resource block."""
        lines = code.split("\n")
        start = None
        resource_re = re.compile(
            rf'^\s*resource\s+"docker_image"\s+"{re.escape(resource_name)}"\s*\{{\s*$'
        )
        for i, line in enumerate(lines):
            if resource_re.match(line):
                start = i
                break
        if start is None:
            return code, False

        end = TerraformDeployer._find_block_end(lines, start)

        keep_line = "  keep_locally = true"
        keep_idx = None
        for i in range(start + 1, end):
            if re.match(r"^\s*keep_locally\s*=", lines[i]):
                keep_idx = i
                break

        changed = False
        if keep_idx is not None:
            if lines[keep_idx].strip().lower() != keep_line.strip().lower():
                lines[keep_idx] = keep_line
                changed = True
        else:
            # Insert after name if available, otherwise before closing brace.
            insert_idx = end
            for i in range(start + 1, end):
                if re.match(r"^\s*name\s*=", lines[i]):
                    insert_idx = i + 1
                    break
            lines.insert(insert_idx, keep_line)
            changed = True

        return "\n".join(lines), changed

    async def init(self, upgrade: bool = False) -> CommandResult:
        """Run tofu init."""
        cmd = ["init"]
        if upgrade:
            cmd.append("-upgrade")
        return await self._run_tofu(cmd)

    async def fmt(self) -> CommandResult:
        """Run tofu fmt."""
        return await self._run_tofu(["fmt"])

    async def validate(self) -> CommandResult:
        """Run tofu validate."""
        return await self._run_tofu(["validate"])

    async def plan(self, out_file: str = "tfplan") -> CommandResult:
        """Run tofu plan."""
        return await self._run_tofu(["plan", f"-out={out_file}"])

    async def apply(self, plan_file: str = "tfplan") -> CommandResult:
        """Run tofu apply."""
        cmd = ["apply"]
        if self.config.auto_approve:
            cmd.append("-auto-approve")
        cmd.append(plan_file)
        return await self._run_tofu(cmd)

    async def apply_targets(self, target_addresses: list[str]) -> CommandResult:
        """Run tofu apply with -target flags for specific resources.

        This enables partial deployment: only the targeted resources (and
        their dependencies) are applied.  Used as a last-resort fallback
        when the full apply fails due to a few bad containers.
        """
        cmd = ["apply", "-auto-approve", "-input=false"]
        for addr in target_addresses:
            cmd.extend(["-target", addr])
        return await self._run_tofu(cmd)

    async def destroy(self) -> CommandResult:
        """Run tofu destroy."""
        cmd = ["destroy"]
        if self.config.auto_approve:
            cmd.append("-auto-approve")
        result = await self._run_tofu(cmd)
        if result.success:
            return result

        recovery = await self.recover_container_destroy_cycle(result.stderr)
        if not recovery.success:
            return result

        logger.warning(
            "tofu destroy hit a docker_container destroy cycle; "
            "removed implicated containers from Docker/state and retrying"
        )
        retry = await self._run_tofu(cmd)
        if retry.success:
            return retry

        combined_stdout = "\n".join(
            chunk for chunk in (
                (result.stdout or "").strip(),
                (recovery.stdout or "").strip(),
                (retry.stdout or "").strip(),
            )
            if chunk
        )
        combined_stderr = "\n".join(
            chunk for chunk in (
                (result.stderr or "").strip(),
                (recovery.stderr or "").strip(),
                (retry.stderr or "").strip(),
            )
            if chunk
        )
        return CommandResult(
            success=False,
            stdout=combined_stdout,
            stderr=combined_stderr,
            exit_code=retry.exit_code,
        )

    @staticmethod
    def _extract_destroy_cycle_container_addresses(stderr: str) -> list[str]:
        """Return docker_container addresses mentioned in a destroy cycle."""
        if not stderr:
            return []

        matches = re.findall(r"(docker_container\.[0-9A-Za-z_]+)\s*\(destroy\)", stderr)
        if not matches and "cycle:" in stderr.lower():
            matches = re.findall(r"docker_container\.[0-9A-Za-z_]+", stderr)
        return list(dict.fromkeys(matches))

    def _resolve_container_names_from_state(self, addresses: list[str]) -> dict[str, str]:
        """Map terraform addresses to real Docker container names from state."""
        resolved: dict[str, str] = {}
        state_path = self.work_dir / "terraform.tfstate"
        if state_path.exists():
            try:
                payload = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError) as e:
                logger.warning("Could not parse terraform state for cycle recovery: %s", e)
            else:
                for resource in payload.get("resources", []):
                    if not isinstance(resource, dict) or resource.get("type") != "docker_container":
                        continue
                    resource_name = resource.get("name")
                    if not isinstance(resource_name, str):
                        continue
                    address = f"docker_container.{resource_name}"
                    if address not in addresses:
                        continue
                    instances = resource.get("instances") or []
                    if not isinstance(instances, list) or not instances:
                        continue
                    attrs = instances[0].get("attributes") or {}
                    actual_name = attrs.get("name") if isinstance(attrs, dict) else None
                    if isinstance(actual_name, str) and actual_name.strip():
                        resolved[address] = actual_name.strip()

        for address in addresses:
            parts = address.split(".", 1)
            resolved.setdefault(address, parts[1] if len(parts) > 1 else address)
        return resolved

    async def recover_container_destroy_cycle(self, stderr: str) -> CommandResult:
        """Force-remove cycle-involved containers and drop them from terraform state."""
        addresses = self._extract_destroy_cycle_container_addresses(stderr)
        if not addresses:
            return CommandResult(
                success=False,
                stdout="",
                stderr="No docker_container destroy cycle found",
                exit_code=1,
            )

        name_map = self._resolve_container_names_from_state(addresses)
        logger.warning(
            "Recovering docker_container destroy cycle for: %s",
            ", ".join(addresses),
        )

        stdout_chunks = [f"cycle_addresses={','.join(addresses)}"]
        stderr_chunks: list[str] = []

        # Remove from Terraform state FIRST — state rm is idempotent and
        # safe to retry if the subsequent docker rm fails.  The reverse
        # order (docker rm first) risks state/reality divergence when the
        # state rm step fails after containers are already gone.
        state_rm_result = await self._run_tofu(["state", "rm", *addresses])
        if state_rm_result.stdout:
            stdout_chunks.append(state_rm_result.stdout.strip())
        if state_rm_result.stderr:
            stderr_chunks.append(state_rm_result.stderr.strip())
        if not state_rm_result.success:
            return CommandResult(
                success=False,
                stdout="\n".join(chunk for chunk in stdout_chunks if chunk),
                stderr="\n".join(chunk for chunk in stderr_chunks if chunk),
                exit_code=state_rm_result.exit_code,
            )

        for address in addresses:
            container_name = name_map.get(address, address.split(".", 1)[1])
            docker_result = await self._run_docker_cmd(["rm", "-f", container_name])
            if docker_result.stdout:
                stdout_chunks.append(docker_result.stdout.strip())
            if docker_result.stderr:
                stderr_chunks.append(docker_result.stderr.strip())
            if not docker_result.success:
                stderr_lower = (docker_result.stderr or "").lower()
                if "no such container" not in stderr_lower:
                    logger.warning(
                        "docker rm failed for %s after state rm — container may be stale: %s",
                        container_name, docker_result.stderr,
                    )

        return CommandResult(
            success=state_rm_result.success,
            stdout="\n".join(chunk for chunk in stdout_chunks if chunk),
            stderr="\n".join(chunk for chunk in stderr_chunks if chunk),
            exit_code=state_rm_result.exit_code,
        )

    async def plan_and_apply(self) -> tuple[CommandResult, Optional[CommandResult]]:
        """Run full plan and apply cycle."""
        logger.debug("plan_and_apply: starting")
        # Reset stage results at the start of a new cycle.
        # We intentionally reset *before* any stage runs so that a caller
        # inspecting last_stage_results after an exception always sees the
        # results that belong to THIS cycle, not a previous one.  The
        # reset is therefore correct: if an early stage raises, the
        # previous stage slots remain None for this cycle which is accurate.
        self.last_stage_results = {
            "fmt": None,
            "init": None,
            "validate": None,
            "plan": None,
            "apply": None,
        }

        # Backward compatibility: normalize legacy labels map syntax in JSON configs.
        self._normalize_legacy_container_labels()

        # Format
        logger.debug("plan_and_apply: running fmt")
        fmt_result = await self.fmt()
        self.last_stage_results["fmt"] = fmt_result
        if not fmt_result.success:
            logger.warning(f"fmt failed: {fmt_result.stderr}")
            if self.config.fail_on_fmt_error:
                return fmt_result, None

        # Init
        logger.debug("plan_and_apply: running init (upgrade=%s)", self.config.upgrade_providers_on_init)
        init_result = await self.init(upgrade=self.config.upgrade_providers_on_init)
        self.last_stage_results["init"] = init_result
        logger.debug(f"plan_and_apply: init result: success={init_result.success}")
        if not init_result.success:
            return init_result, None

        # Validate
        logger.debug("plan_and_apply: running validate")
        validate_result = await self.validate()
        self.last_stage_results["validate"] = validate_result
        logger.debug(f"plan_and_apply: validate result: success={validate_result.success}")
        if not validate_result.success:
            return validate_result, None

        # Plan
        logger.debug("plan_and_apply: running plan")
        plan_result = await self.plan()
        self.last_stage_results["plan"] = plan_result
        logger.debug(f"plan_and_apply: plan result: success={plan_result.success}")
        if not plan_result.success:
            return plan_result, None

        # Apply
        logger.debug("plan_and_apply: running apply")
        apply_result = await self.apply()
        self.last_stage_results["apply"] = apply_result
        logger.debug(f"plan_and_apply: apply result: success={apply_result.success}")
        return plan_result, apply_result

    def _normalize_legacy_container_labels(self) -> int:
        """Normalize docker_container.labels map -> [{label,value}] in main.tofu.json.

        Older generated JSON used:
            "labels": {"k": "v"}
        but the docker provider JSON syntax expects:
            "labels": [{"label": "k", "value": "v"}]
        """
        tofu_path = self.work_dir / "main.tofu.json"
        if not tofu_path.exists():
            return 0

        try:
            payload = json.loads(tofu_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return 0

        resources = payload.get("resource")
        if not isinstance(resources, dict):
            return 0
        docker_container = resources.get("docker_container")
        if not isinstance(docker_container, dict):
            return 0

        updated = 0
        for _, block in docker_container.items():
            if not isinstance(block, dict):
                continue
            labels = block.get("labels")
            if not isinstance(labels, dict):
                continue
            block["labels"] = [
                {"label": str(k), "value": str(v)}
                for k, v in labels.items()
            ]
            updated += 1

        if updated:
            tofu_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            logger.info("Normalized legacy labels syntax in %d docker_container block(s)", updated)
        return updated

    @staticmethod
    def _extract_resource_addresses(code: str) -> set[str]:
        """Extract Terraform resource addresses from HCL code."""
        addresses = set()
        stripped = (code or "").lstrip()
        if stripped.startswith("{"):
            try:
                payload = json.loads(code)
            except (json.JSONDecodeError, ValueError):
                payload = None
            if isinstance(payload, dict):
                resources = payload.get("resource")
                if isinstance(resources, dict):
                    for resource_type, entries in resources.items():
                        if not isinstance(resource_type, str) or not isinstance(entries, dict):
                            continue
                        for resource_name in entries.keys():
                            if isinstance(resource_name, str):
                                addresses.add(f"{resource_type}.{resource_name}")
                if addresses:
                    return addresses
        for match in re.finditer(r'resource\s+"([^"]+)"\s+"([^"]+)"', code):
            addresses.add(f"{match.group(1)}.{match.group(2)}")
        return addresses

    async def prune_stale_state(self, code: str) -> CommandResult:
        """
        Remove stale docker_* resources from Terraform state.

        This prevents apply-time destroy errors for resources that are no longer
        present in generated configuration (for example old docker_image entries).
        """
        desired = self._extract_resource_addresses(code)
        state_list = await self._run_tofu(["state", "list"])

        if not state_list.success:
            msg = f"{state_list.stderr}\n{state_list.stdout}".lower()
            if "no state file was found" in msg or "state file not found" in msg:
                return CommandResult(success=True, stdout="", stderr="", exit_code=0)
            return state_list

        stale = []
        for line in state_list.stdout.splitlines():
            addr = line.strip()
            if not addr:
                continue
            if addr.startswith("docker_") and addr not in desired:
                stale.append(addr)

        if not stale:
            return CommandResult(success=True, stdout="", stderr="", exit_code=0)

        logger.info(f"Pruning {len(stale)} stale Terraform state resource(s): {', '.join(stale)}")
        stdout_chunks = []
        stderr_chunks = []
        ok = True

        for addr in stale:
            rm_result = await self._run_tofu(["state", "rm", addr])
            ok = ok and rm_result.success
            if rm_result.stdout:
                stdout_chunks.append(rm_result.stdout.strip())
            if rm_result.stderr:
                stderr_chunks.append(rm_result.stderr.strip())

        return CommandResult(
            success=ok,
            stdout="\n".join(stdout_chunks),
            stderr="\n".join(stderr_chunks),
            exit_code=0 if ok else 1,
        )

    async def reset_state(self) -> CommandResult:
        """Reset Terraform state by removing .terraform directory."""
        if self.config.use_docker:
            return await self._run_docker_exec(
                ["sh", "-c", "cd /workspace && rm -rf .terraform terraform.tfstate*"]
            )
        else:
            tf_dir = self.work_dir / ".terraform"
            if tf_dir.exists():
                shutil.rmtree(tf_dir)
            for state_file in self.work_dir.glob("terraform.tfstate*"):
                state_file.unlink()
            return CommandResult(success=True, stdout="", stderr="", exit_code=0)

    async def cleanup_docker_resources(
        self, networks: list[str], containers: list[str]
    ) -> CommandResult:
        """Remove only the named containers and networks produced by this project.

        All operations are best-effort: resources that do not yet exist (first
        deploy) produce non-fatal errors that are captured in stderr but do not
        mark the overall result as failed.

        Note: ``docker network prune`` was intentionally removed.  A global
        prune would delete *all* unused Docker networks on the host, including
        those belonging to unrelated projects.  Only the explicitly named
        networks from the current deploy projection are removed.
        """
        all_stdout: list[str] = []
        all_stderr: list[str] = []
        last_exit_code = 0

        # Remove specific containers first (best-effort)
        for container in containers:
            result = await self._run_docker_cmd(["rm", "-f", container])
            if result.stdout:
                all_stdout.append(result.stdout)
            if result.stderr:
                all_stderr.append(result.stderr)
            # Track container removal failures (not "no such container")
            if not result.success and "no such container" not in (result.stderr or "").lower():
                last_exit_code = result.exit_code or 1

        # Remove named networks (best-effort)
        for net in networks:
            result = await self._run_docker_cmd(["network", "rm", net])
            if result.stdout:
                all_stdout.append(result.stdout)
            if result.stderr:
                all_stderr.append(result.stderr)
            # Track exit code only for actual failures (not "not found")
            if not result.success and "no such network" not in (result.stderr or "").lower():
                last_exit_code = result.exit_code or 1

        return CommandResult(
            success=last_exit_code == 0,
            stdout="\n".join(all_stdout),
            stderr="\n".join(all_stderr),
            exit_code=last_exit_code,
        )

    async def _run_docker_cmd(self, args: list[str]) -> CommandResult:
        """Run a docker command directly."""
        cmd = ["docker"] + args
        return await self._run_process(cmd)

    async def get_container_logs(self, container_name: str, tail: int = 80) -> str:
        """Return the last N log lines from a container (works on running or exited).

        Docker writes most container output to stderr, so both stdout and stderr
        are concatenated and returned as a single string.
        """
        result = await self._run_docker_cmd(["logs", "--tail", str(tail), container_name])
        return ((result.stdout or "") + (result.stderr or "")).strip()

    async def get_used_ports(self) -> set[int]:
        """Get list of ports currently in use by Docker."""
        result = await self._run_docker_cmd(["ps", "--format", "{{.Ports}}"])
        if not result.success:
            return set()

        used = set()
        # Parse port mappings like "0.0.0.0:8080->80/tcp"
        for match in re.finditer(r":(\d+)->", result.stdout):
            used.add(int(match.group(1)))

        return used

    async def get_host_port_owners(
        self, ports: Optional[list[int]] = None
    ) -> dict[int, list[str]]:
        """
        Return Docker container owners for published host ports.

        Args:
            ports: Optional list of host ports to filter for.

        Returns:
            Mapping host_port -> list of running container names publishing that port.
        """
        result = await self._run_docker_cmd(["ps", "--format", "{{.Names}}|{{.Ports}}"])
        if not result.success:
            return {}

        filter_ports = set(int(p) for p in ports) if ports else None
        owners: dict[int, set[str]] = {}

        for line in result.stdout.splitlines():
            if "|" not in line:
                continue
            name, ports_blob = line.split("|", 1)
            container_name = name.strip()
            if not container_name:
                continue
            for match in re.finditer(r":(\d+)->", ports_blob):
                host_port = int(match.group(1))
                if filter_ports is not None and host_port not in filter_ports:
                    continue
                owners.setdefault(host_port, set()).add(container_name)

        return {port: sorted(names) for port, names in owners.items()}

    async def verify_deployment(self, expected_containers: list[str]) -> dict:
        """Verify that expected containers are running and not unhealthy.

        A container is considered successfully deployed only when:
        - Its status is "running" (not exited, restarting, or created), AND
        - Its Docker health check (if configured) is not "unhealthy" or "starting".

        Containers whose health check is still "starting" are retried up to
        60 seconds — a container stuck in "starting" is treated as unhealthy
        (potential boot-loop).
        """

        async def _snapshot_runtime_state() -> tuple[dict[str, dict], list[str], list[str]]:
            result = await self._run_docker_cmd([
                "ps", "--filter", "status=running",
                "--format", "{{.Names}}|{{.Image}}|{{.Status}}",
            ])

            all_running: dict[str, dict] = {}
            if result.success:
                for line in result.stdout.strip().split("\n"):
                    if "|" not in line:
                        continue
                    parts = line.split("|")
                    if len(parts) < 3:
                        continue
                    all_running[parts[0]] = {
                        "image": parts[1],
                        "status": parts[2],
                    }

            deployed = {
                name: info for name, info in all_running.items()
                if name in expected_containers
            }
            missing = [c for c in expected_containers if c not in all_running]

            unhealthy: list[str] = []
            for name in list(deployed.keys()):
                inspect_result = await self._run_docker_cmd([
                    "inspect", "--format",
                    "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}",
                    name,
                ])
                if not inspect_result.success:
                    # Cannot determine health — treat as degraded, not silent pass
                    deployed[name]["health"] = "inspect_failed"
                    unhealthy.append(name)
                    continue

                health_status = inspect_result.stdout.strip().lower()
                if health_status == "unhealthy":
                    unhealthy.append(name)
                    deployed[name]["health"] = "unhealthy"
                elif health_status == "starting":
                    deployed[name]["health"] = "starting"
                elif health_status == "healthy":
                    deployed[name]["health"] = "healthy"
                else:
                    # "none" means no health check is configured — the
                    # container's running status (already verified above)
                    # is the only signal available.
                    deployed[name]["health"] = health_status or "none"

            return deployed, missing, unhealthy

        deployed, missing, unhealthy = await _snapshot_runtime_state()

        if missing and expected_containers:
            # Wait for containers to stabilize (simple timeout)
            stabilization_timeout = min(30.0 + len(expected_containers) * 5.0, 120.0)
            stabilization_deadline = time.monotonic() + stabilization_timeout
            previous_missing_count = len(missing)

            logger.info(
                "verify_deployment: waiting up to %.1fs for %d missing containers to become visible",
                stabilization_timeout,
                previous_missing_count,
            )

            while missing and time.monotonic() < stabilization_deadline:
                await asyncio.sleep(3.0)
                deployed, missing, unhealthy = await _snapshot_runtime_state()
                if len(missing) < previous_missing_count:
                    logger.info(
                        "verify_deployment: missing containers reduced from %d to %d during startup stabilization",
                        previous_missing_count,
                        len(missing),
                    )
                previous_missing_count = len(missing)

        # Re-check containers stuck in 'starting' health — give them up to
        # 60s to transition to 'healthy' or 'unhealthy'.  A container that
        # remains in 'starting' is treated as unhealthy (potential boot-loop).
        starting_containers = [
            name for name, info in deployed.items()
            if info.get("health") == "starting"
        ]
        if starting_containers:
            starting_deadline = time.monotonic() + 60.0
            logger.info(
                "verify_deployment: %d container(s) still in 'starting' health — "
                "waiting up to 60s: %s",
                len(starting_containers), starting_containers,
            )
            while starting_containers and time.monotonic() < starting_deadline:
                await asyncio.sleep(5.0)
                still_starting = []
                for name in starting_containers:
                    inspect_result = await self._run_docker_cmd([
                        "inspect", "--format",
                        "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}",
                        name,
                    ])
                    if not inspect_result.success:
                        deployed[name]["health"] = "inspect_failed"
                        if name not in unhealthy:
                            unhealthy.append(name)
                        continue
                    health = inspect_result.stdout.strip().lower()
                    deployed[name]["health"] = health
                    if health == "healthy":
                        pass  # resolved
                    elif health == "unhealthy":
                        if name not in unhealthy:
                            unhealthy.append(name)
                    elif health == "starting":
                        still_starting.append(name)
                    else:
                        # "none" or unknown — ignore
                        pass
                starting_containers = still_starting

            # Any container still in 'starting' after timeout → treat as unhealthy
            for name in starting_containers:
                logger.warning(
                    "Container %s stuck in 'starting' after 60s — treating as unhealthy", name
                )
                deployed[name]["health"] = "starting_timeout"
                if name not in unhealthy:
                    unhealthy.append(name)

        # Final snapshot: containers may have exited during the health-wait phase.
        # Re-check which containers are actually still running so the caller gets
        # an accurate picture (not stale data from the first snapshot).
        final_deployed, final_missing, final_unhealthy = await _snapshot_runtime_state()
        # Containers that were in 'deployed' but are now missing have crashed
        for name in list(deployed.keys()):
            if name not in final_deployed and name in expected_containers:
                if name not in missing:
                    missing.append(name)
                deployed.pop(name, None)
                logger.info("verify_deployment: %s exited during health-wait phase", name)

        # Merge unhealthy containers from final snapshot
        for name in final_unhealthy:
            if name not in unhealthy:
                unhealthy.append(name)

        # Update health fields in deployed dict from the final snapshot so
        # callers see current state, not stale data from the health-wait phase.
        for name, info in final_deployed.items():
            if name in deployed and isinstance(info, dict):
                deployed[name]["health"] = info.get("health", deployed[name].get("health"))

        return {
            "running": deployed,
            "expected": expected_containers,
            "missing": missing,
            "unhealthy": unhealthy,
            "success": len(missing) == 0 and len(unhealthy) == 0,
            "running_count": len(deployed),
            "expected_count": len(expected_containers),
        }

    async def _run_tofu(self, args: list[str]) -> CommandResult:
        """Run a tofu command."""
        logger.debug(f"_run_tofu: running tofu {' '.join(args)}")
        try:
            if self.config.use_docker:
                # Run inside Docker container
                # Use shlex.quote to prevent shell injection
                import shlex
                escaped_args = ' '.join(shlex.quote(arg) for arg in args)
                docker_args = [
                    "sh", "-c",
                    f"cd /workspace && OPENTOFU_ENFORCE_GPG_VALIDATION=false tofu {escaped_args}"
                ]
                result = await self._run_docker_exec(docker_args)
            else:
                # Run directly
                cmd = [self.config.tofu_binary] + args
                result = await self._run_process(cmd, cwd=str(self.work_dir))

            logger.debug(f"_run_tofu: completed with success={result.success}")
            return result
        except asyncio.TimeoutError as e:
            logger.error(f"_run_tofu: timeout: {e}")
            return CommandResult(
                success=False,
                stdout="",
                stderr=f"Tofu command timed out: {e}",
                exit_code=-1,
            )
        except (OSError, subprocess.SubprocessError) as e:
            logger.error(f"_run_tofu: process error: {e}", exc_info=True)
            return CommandResult(
                success=False,
                stdout="",
                stderr=f"Failed to run tofu: {e}",
                exit_code=-1,
            )

    async def _run_docker_exec(self, args: list[str]) -> CommandResult:
        """Run a command inside the Docker container."""
        cmd = [
            "docker", "exec",
            "-e", "OPENTOFU_ENFORCE_GPG_VALIDATION=false",
            self.config.docker_container,
        ] + args
        return await self._run_process(cmd)

    async def _run_process(
        self, cmd: list[str], cwd: Optional[str] = None
    ) -> CommandResult:
        """Run a process asynchronously."""
        cmd_str = " ".join(cmd[:3]) + ("..." if len(cmd) > 3 else "")
        logger.debug(f"_run_process: starting '{cmd_str}' in {cwd or 'current dir'}")

        try:
            # On Windows, use sync subprocess in executor to avoid asyncio issues
            if IS_WINDOWS:
                return await self._run_process_sync(cmd, cwd)

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            logger.debug(f"_run_process: process created with pid={process.pid}")

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=self.config.timeout,
                )
                logger.debug(f"_run_process: process completed with returncode={process.returncode}")
            except asyncio.TimeoutError:
                logger.warning(f"_run_process: command timed out after {self.config.timeout}s")
                process.kill()
                try:
                    await process.communicate()
                except Exception:
                    pass
                return CommandResult(
                    success=False,
                    stdout="",
                    stderr="Command timed out",
                    exit_code=-1,
                )

            return CommandResult(
                success=process.returncode == 0,
                stdout=stdout.decode("utf-8", errors="replace"),
                stderr=stderr.decode("utf-8", errors="replace"),
                exit_code=process.returncode if process.returncode is not None else -1,
            )

        except FileNotFoundError as e:
            logger.error(f"_run_process: command not found: {e}")
            return CommandResult(
                success=False,
                stdout="",
                stderr=f"Command not found: {e}",
                exit_code=-1,
            )
        except Exception as e:
            logger.error(f"_run_process: unexpected error: {e}")
            return CommandResult(
                success=False,
                stdout="",
                stderr=str(e),
                exit_code=-1,
            )

    async def _run_process_sync(
        self, cmd: list[str], cwd: Optional[str] = None
    ) -> CommandResult:
        """Run a process synchronously in an executor (Windows fallback)."""
        def run_sync():
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=cwd,
                )
            except FileNotFoundError as e:
                return CommandResult(
                    success=False,
                    stdout="",
                    stderr=f"Command not found: {e}",
                    exit_code=-1,
                )
            except Exception as e:
                return CommandResult(
                    success=False,
                    stdout="",
                    stderr=str(e),
                    exit_code=-1,
                )
            try:
                stdout_bytes, stderr_bytes = proc.communicate(
                    timeout=self.config.timeout,
                )
                return CommandResult(
                    success=proc.returncode == 0,
                    stdout=stdout_bytes.decode("utf-8", errors="replace"),
                    stderr=stderr_bytes.decode("utf-8", errors="replace"),
                    exit_code=proc.returncode,
                )
            except subprocess.TimeoutExpired:
                # Kill the child process to avoid zombie processes holding
                # state file locks on Windows.
                proc.kill()
                proc.communicate()
                return CommandResult(
                    success=False,
                    stdout="",
                    stderr="Command timed out",
                    exit_code=-1,
                )

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, run_sync)
