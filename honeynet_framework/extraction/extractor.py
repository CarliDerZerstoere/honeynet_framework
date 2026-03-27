"""
World Model Extractor - single LLM call to generate a complete WorldModel.
"""

import json
import logging
import re
from typing import Optional

from ..llm import LLMConfig, LLMProvider
from ..image_resolver import check_image_exists, fetch_image_config


# Consistent healthcheck defaults used across all code paths.
_HC_DEFAULT_INTERVAL = "30s"
_HC_DEFAULT_TIMEOUT = "10s"
_HC_DEFAULT_RETRIES = 3
_HC_DEFAULT_START_PERIOD = "30s"

# Provider max output token limits — used to cap the dynamic token budget.
# Must be large enough for hard scenarios (20+ systems × 500 tokens = 10k+).
# Actual limits depend on model variant; these are conservative safe defaults.
_PROVIDER_MAX_OUTPUT: dict[str, int] = {
    "ollama": 32768,     # most models support 32k+ output; qwen2.5-coder:32b supports up to 128k
    "openai": 16384,     # gpt-4o / gpt-4-turbo (gpt-4o supports up to 16k output)
    "anthropic": 32768,  # claude-3.5/4 supports up to 8192 default, 32k with extended output
}
_DEFAULT_PROVIDER_CAP = 32768


def _provider_max_output_tokens(config: LLMConfig) -> int:
    """Return the max output token limit for the configured provider."""
    return _PROVIDER_MAX_OUTPUT.get(config.provider, _DEFAULT_PROVIDER_CAP)
from ..models import (
    Organization,
    BusinessUnit,
    System,
    SystemDeploy,
    SystemSimulate,
    SystemKind,
    VolumeMount,
    WorldModel,
    WorldZone,
    ZoneDeploy,
    ZoneSimulate,
    Secret,
    SecretSimulate,
)
from .prompts import (
    build_extraction_prompt,
    build_image_repair_prompt,
    build_container_repair_prompt,
    build_startup_config_prompt,
)
from ..retrieval import select_images_for_scenario
from .yaml_parsing import extract_yaml_from_response
from .coercion import coerce_bool, coerce_string_list, coerce_int_list, coerce_volume_mounts

logger = logging.getLogger(__name__)

# Tool definition (Anthropic input_schema format — translated per-provider inside generate_with_tools).
_VALIDATE_IMAGE_TOOL = {
    "name": "validate_docker_image",
    "description": (
        "Check whether a Docker image:tag exists and is pullable from a registry. "
        "Call this for EVERY image you plan to include in the honeynet before writing your YAML. "
        "If the image does not exist, try alternative tags or the ghcr.io/quay.io registry prefix "
        "until you find a pullable reference, then use that in your YAML output."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "image_ref": {
                "type": "string",
                "description": (
                    "Full image reference to validate, "
                    "e.g. 'nginx:1.25-alpine' or 'ghcr.io/bitnami/kafka:3.6'"
                ),
            }
        },
        "required": ["image_ref"],
    },
}


_FETCH_IMAGE_CONFIG_TOOL = {
    "name": "fetch_image_config",
    "description": (
        "Fetch the default Env, Cmd, Entrypoint and ExposedPorts for a container image "
        "directly from the OCI registry — no Docker pull required. "
        "Use this to discover which environment variables an image requires before "
        "generating a service config or diagnosing a startup failure."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "image_ref": {
                "type": "string",
                "description": "Full image reference, e.g. 'mysql:8.0' or 'ghcr.io/bitnami/kafka:3.6'",
            }
        },
        "required": ["image_ref"],
    },
}


async def _image_tool_executor(tool_name: str, tool_input: dict) -> str:
    """Execute LLM tool calls for image validation and config fetching."""
    if tool_name == "validate_docker_image":
        image_ref = (tool_input.get("image_ref") or "").strip()
        if not image_ref:
            return json.dumps({"exists": False, "error": "image_ref is required"})
        result = await check_image_exists(image_ref)
        logger.debug("Image validation: %s → %s", image_ref, result)
        return json.dumps(result)
    if tool_name == "fetch_image_config":
        image_ref = (tool_input.get("image_ref") or "").strip()
        if not image_ref:
            return json.dumps({"error": "image_ref is required"})
        result = await fetch_image_config(image_ref)
        logger.debug("Image config fetch: %s → %s", image_ref, result)
        return json.dumps(result)
    return json.dumps({"error": f"unknown tool: {tool_name}"})


def _honeypot_env_value(var_name: str) -> str:
    """Generate a plausible weak value for a required env var (honeynet bait).

    Values are randomized per call to avoid creating a fingerprint that
    attackers could use to detect the framework.
    """
    import random
    import string

    name = var_name.lower()
    if "password" in name or "passwd" in name:
        # Pool of plausible weak passwords (honeypot bait)
        weak = ["admin", "password", "changeme", "welcome1", "letmein", "qwerty",
                "test123", "P@ssw0rd", "12345678", "master"]
        return random.choice(weak) + str(random.randint(1, 99))
    if "secret" in name or "token" in name:
        # Short weak secrets for honeypot purposes (intentionally weak)
        weak_secrets = ["changeme", "s3cret", "default", "test"]
        return random.choice(weak_secrets) + str(random.randint(100, 999))
    if "key" in name and "api" in name:
        # API keys — short weak values
        return "test-api-key-" + str(random.randint(1000, 9999))
    if "key" in name:
        return "default-key-" + str(random.randint(100, 999))
    if "user" in name or "login" in name:
        # Exclude "root" to avoid conflicts with built-in root users
        users = ["admin", "sysadmin", "operator", "service", "deploy"]
        return random.choice(users)
    if "admin" in name and "password" not in name:
        return "admin"
    if "advertised" in name and "host" in name:
        # Kafka etc.: advertised host must be a resolvable name, not 0.0.0.0
        return "localhost"
    if "host" in name and "name" in name:
        # HOSTNAME-like vars should be a resolvable name
        return "localhost"
    if "host" in name:
        return "0.0.0.0"
    if "port" in name:
        # Try to return a sensible default port based on context
        _PORT_HINTS = {
            "redis": "6379", "mongo": "27017", "mysql": "3306",
            "postgres": "5432", "kafka": "9092", "zookeeper": "2181",
            "elastic": "9200", "grafana": "3000", "prometheus": "9090",
        }
        for hint, port in _PORT_HINTS.items():
            if hint in name:
                return port
        return "8080"
    if "db" in name or "database" in name:
        dbs = ["production", "maindb", "appdata", "healthcare", "records"]
        return random.choice(dbs)
    return "default"


class WorldModelExtractor:
    """Extract a WorldModel from a user prompt via LLM call(s).

    When ``multi_phase=True``, splits extraction into three focused phases
    (Architecture → Config → Enrich) for better quality on complex scenarios.
    """

    def __init__(self, llm_provider: LLMProvider, multi_phase: bool = False):
        self.llm = llm_provider
        self.multi_phase = multi_phase

    async def extract(self, user_request: str, *, scenario_context: str | None = None) -> WorldModel:
        """Generate a WorldModel from a natural language prompt.

        Pre-fetches OCI image configs for scenario-relevant images and injects
        them into the prompt so the LLM has real Entrypoint/Cmd/Env/ExposedPorts
        data without needing tool calls.  Tools are still available for images
        not in the pre-fetched set.

        When ``multi_phase=True``, delegates to MultiPhaseExtractor which
        splits the work into Architecture → Config → Enrich phases.
        """
        if self.multi_phase:
            return await self._extract_multi_phase(user_request, scenario_context=scenario_context)

        relevant_images = select_images_for_scenario(user_request)

        # Pre-fetch OCI configs for all relevant images — eliminates most
        # tool-use rounds and gives the LLM real data to work with.
        # Concurrency is limited to avoid Docker Hub rate-limiting (429).
        image_configs: dict[str, dict] = {}
        if relevant_images:
            from ..image_resolver import fetch_image_config
            import asyncio

            _semaphore = asyncio.Semaphore(5)  # max 5 concurrent registry calls

            async def _fetch(img: str) -> tuple[str, dict]:
                async with _semaphore:
                    try:
                        return img, await fetch_image_config(img)
                    except Exception:
                        return img, {"error": "fetch failed"}

            results = await asyncio.gather(*[_fetch(img) for img in relevant_images])
            image_configs = {img: cfg for img, cfg in results if "error" not in cfg}
            logger.info(
                "Pre-fetched OCI configs for %d/%d relevant images",
                len(image_configs), len(relevant_images),
            )

        system_msg, user_msg, estimated_max_systems = build_extraction_prompt(
            user_request,
            relevant_images=relevant_images or None,
            image_configs=image_configs or None,
            scenario_context=scenario_context,
        )

        # Dynamic token budget: ~500 tokens per system + 3000 overhead (zones,
        # secrets, org, YAML structure, simulate blocks).  The original 350/sys
        # was too tight — real YAML output averages 400-550 tokens per system
        # including env, volumes, healthchecks, and simulate blocks.  Insufficient
        # budget causes Ollama's num_predict to hard-cut the output mid-YAML,
        # producing truncated results (e.g. 13 systems instead of 20+).
        # Floor at config default, capped at provider max output tokens.
        tokens_per_system = 500
        overhead_tokens = 3000
        provider_cap = _provider_max_output_tokens(self.llm.config)
        dynamic_max_tokens = min(
            max(
                self.llm.config.max_tokens,
                estimated_max_systems * tokens_per_system + overhead_tokens,
            ),
            provider_cap,
        )
        logger.info(
            "Dynamic token budget: %d tokens (estimated %d systems × %d + %d overhead, "
            "config default: %d, provider cap: %d)",
            dynamic_max_tokens, estimated_max_systems, tokens_per_system,
            overhead_tokens, self.llm.config.max_tokens, provider_cap,
        )

        response, _usage = await self.llm.generate_with_tools(
            user_msg,
            tools=[_VALIDATE_IMAGE_TOOL, _FETCH_IMAGE_CONFIG_TOOL],
            tool_executor=_image_tool_executor,
            system_message=system_msg,
            max_tokens=dynamic_max_tokens,
        )
        raw = extract_yaml_from_response(response)
        world_model = self._build_world_model(raw)
        await self._post_extraction_oci_fixup(world_model)
        self.last_dep_additions = self._infer_dependencies_from_env(world_model)
        self.last_placement_fixes = await self._fix_placements(world_model)
        await self.generate_startup_configs(world_model)
        return world_model

    async def _extract_multi_phase(
        self, user_request: str, *, scenario_context: str | None = None,
    ) -> WorldModel:
        """Multi-phase extraction: Architecture → Config → Enrich.

        Delegates the 3-phase LLM work to MultiPhaseExtractor, then applies
        the same post-fixups as the single-call path.
        """
        from .multi_phase import MultiPhaseExtractor

        mp = MultiPhaseExtractor(self.llm)
        raw = await mp.extract_raw(user_request, scenario_context=scenario_context)
        world_model = self._build_world_model(raw)
        await self._post_extraction_oci_fixup(world_model)
        self.last_dep_additions = self._infer_dependencies_from_env(world_model)
        self.last_placement_fixes = await self._fix_placements(world_model)
        await self.generate_startup_configs(world_model)
        return world_model

    async def _post_extraction_oci_fixup(self, world_model: WorldModel) -> None:
        """Fix obviously wrong commands/env after extraction using real OCI data.

        The LLM sometimes guesses commands from image names (e.g. sets
        command: ["all-in-one"] for jaegertracing/all-in-one, or ["server"]
        for clickhouse-server) even though the image has a daemon entrypoint
        that starts correctly with command: null.

        This step fetches OCI configs for all actually-chosen images and:
        - Strips commands on images with daemon entrypoints
        - Adds required env vars that are missing
        """
        from ..image_introspector import get_image_profiles, ImageProfile

        deployable = [s for s in world_model.systems.values() if s.deploy and s.deploy.image]
        if not deployable:
            return

        image_refs = list({s.deploy.image for s in deployable})
        try:
            profiles = await get_image_profiles(image_refs)
        except Exception as exc:
            logger.warning("Post-extraction OCI fixup failed to fetch profiles: %s", exc)
            return

        fixes_applied = 0
        for sys_name, system in world_model.systems.items():
            if not system.deploy or not system.deploy.image:
                continue
            profile = profiles.get(system.deploy.image)
            if not profile or not profile.available:
                continue

            # Fix 1: Strip unnecessary commands on daemon images
            if system.deploy.command and profile.has_daemon_entrypoint:
                old_cmd = system.deploy.command
                system.deploy.command = None
                logger.info(
                    "OCI fixup: %s — stripped command %r (image '%s' has daemon entrypoint %s)",
                    sys_name, old_cmd, system.deploy.image, list(profile.entrypoint),
                )
                fixes_applied += 1

            # Fix 2: Add required env vars with honeypot defaults
            if profile.required_env:
                existing_keys = {e.split("=", 1)[0] for e in system.deploy.env if "=" in e}
                for var in profile.required_env:
                    if var not in existing_keys:
                        # Generate a weak honeypot value
                        val = _honeypot_env_value(var)
                        system.deploy.env.append(f"{var}={val}")
                        logger.info("OCI fixup: %s — added required env %s=%s", sys_name, var, val)
                        fixes_applied += 1

        if fixes_applied:
            logger.info("OCI fixup: applied %d correction(s) to WorldModel", fixes_applied)

        # Fix 3: Rewrite fragile wget-based healthchecks to robust alternatives
        from ..healthcheck_fixup import fix_healthcheck_commands
        hc_fixes = fix_healthcheck_commands(world_model, profiles)
        if hc_fixes:
            logger.info("OCI fixup: rewrote %d healthcheck(s)", hc_fixes)

    def _infer_dependencies_from_env(self, world_model: WorldModel) -> list[dict]:
        """Infer missing depends_on entries from environment variable values.

        Scans each system's env for references to other system names and
        adds any missing dependency edges.  Returns a list of inferred
        additions for metrics tracking.
        """
        system_names = set(world_model.systems.keys())
        additions: list[dict] = []
        for sys_name, system in world_model.systems.items():
            if not system.deploy:
                continue
            existing_deps_lower = {d.lower() for d in (system.deploy.depends_on or [])}
            for env_entry in (system.deploy.env or []):
                key, _, value = env_entry.partition("=")
                if not value:
                    continue
                value_normalized = value.lower().replace("-", "_")
                for candidate in system_names:
                    if candidate == sys_name:
                        continue
                    candidate_normalized = candidate.lower().replace("-", "_")
                    if (
                        candidate.lower() not in existing_deps_lower
                        and re.search(
                            r'(?:^|[_.\-/:@])' + re.escape(candidate_normalized) + r'(?:$|[_.\-/:@])',
                            value_normalized,
                        )
                    ):
                        if system.deploy.depends_on is None:
                            system.deploy.depends_on = []
                        system.deploy.depends_on.append(candidate)
                        existing_deps_lower.add(candidate.lower())
                        additions.append({
                            "system": sys_name,
                            "added_dep": candidate,
                            "source": f"env:{key}",
                        })
                        logger.info(
                            "Dependency inference: %s → %s (from env %s=%s)",
                            sys_name, candidate, key, value[:60],
                        )
        if additions:
            logger.info(
                "Dependency inference: added %d missing depends_on edge(s)",
                len(additions),
            )
        return additions

    async def _fix_placements(self, world_model: WorldModel) -> list[dict]:
        """Move backend services out of public zones using OCI port data.

        Returns a list of placement corrections for metrics tracking.
        """
        from ..image_introspector import get_image_profile

        # Find the first internal zone as the relocation target
        internal_zone = None
        for zone_name, zone in world_model.zones.items():
            if getattr(zone, "deploy", None) and getattr(zone.deploy, "internal", False):
                internal_zone = zone_name
                break
        if not internal_zone:
            return []

        fixes: list[dict] = []
        for sys_name, system in world_model.systems.items():
            if not system.deploy or not system.deploy.image:
                continue
            current_zone = system.deploy.zone
            zone_obj = world_model.zones.get(current_zone)
            if not zone_obj or not getattr(zone_obj, "deploy", None):
                continue
            # Only fix systems in non-internal zones
            if getattr(zone_obj.deploy, "internal", True):
                continue
            try:
                profile = await get_image_profile(system.deploy.image)
            except Exception:
                continue
            if profile.likely_backend_service:
                old_zone = current_zone
                system.deploy.zone = internal_zone
                fixes.append({
                    "system": sys_name,
                    "from_zone": old_zone,
                    "to_zone": internal_zone,
                    "reason": "backend_ports_only",
                })
                logger.info(
                    "Placement fix: moved %s from zone '%s' to '%s' (backend-only ports)",
                    sys_name, old_zone, internal_zone,
                )
        if fixes:
            logger.info("Placement fix: corrected %d misplaced system(s)", len(fixes))
        return fixes

    async def generate_startup_configs(self, world_model: WorldModel) -> None:
        """Enrich WorldModel with required env vars and healthchecks for each service.

        Calls the LLM with ``fetch_image_config`` available so it can query the OCI
        registry for images it does not recognise.  Only adds fields that are absent —
        never overwrites env vars or healthchecks already set by the extraction prompt.
        Mutates *world_model* in place; returns None.
        """
        deployable = [s for s in world_model.systems.values() if s.deploy and s.deploy.image]
        if not deployable:
            return

        # Skip the LLM call only when every deployed system already has BOTH a
        # healthcheck AND at least one env var configured.  Checking only the
        # healthcheck is insufficient: a database image (e.g. mysql:8.0) with a
        # healthcheck but no MYSQL_ROOT_PASSWORD will still fail at startup.
        # Check which systems still need startup configuration.  A system needs
        # work if it is missing a healthcheck OR if it has no env vars at all OR
        # if it uses a well-known image that requires specific env vars we can
        # detect are missing.
        _REQUIRED_ENV_BY_IMAGE_PREFIX: dict[str, set[str]] = {
            "mysql": {"MYSQL_ROOT_PASSWORD"},
            "mariadb": {"MYSQL_ROOT_PASSWORD", "MARIADB_ROOT_PASSWORD"},
            "postgres": {"POSTGRES_PASSWORD"},
            "mongo": {"MONGO_INITDB_ROOT_PASSWORD"},
            "redis": set(),  # optional but common
        }

        def _needs_startup_work(s: System) -> bool:
            if not s.deploy.healthcheck or not s.deploy.env:
                return True
            # Check known-required env vars against image prefix
            img = (s.deploy.image or "").split(":")[0].split("/")[-1].lower()
            existing_keys = {e.split("=", 1)[0] for e in s.deploy.env if "=" in e}
            for prefix, required in _REQUIRED_ENV_BY_IMAGE_PREFIX.items():
                if img.startswith(prefix) and required:
                    if not required & existing_keys:
                        return True
            return False

        needs_work = [s for s in deployable if _needs_startup_work(s)]
        if not needs_work:
            logger.debug("generate_startup_configs: all systems have healthchecks and required env vars — skipping")
            return

        system_msg, user_msg = build_startup_config_prompt(world_model)
        try:
            response, _usage = await self.llm.generate_with_tools(
                user_msg,
                tools=[_FETCH_IMAGE_CONFIG_TOOL, _VALIDATE_IMAGE_TOOL],
                tool_executor=_image_tool_executor,
                system_message=system_msg,
            )
        except Exception as exc:
            logger.warning("generate_startup_configs LLM call failed: %s", exc)
            return

        raw = extract_yaml_from_response(response)
        configs = raw.get("startup_configs", {})
        if not isinstance(configs, dict):
            logger.warning("generate_startup_configs: unexpected LLM output format")
            return

        for sys_name, cfg in configs.items():
            if not isinstance(cfg, dict):
                continue
            system = world_model.systems.get(sys_name)
            if not system or not system.deploy:
                logger.debug(
                    "generate_startup_configs: LLM returned config for unknown system %r — skipping",
                    sys_name,
                )
                continue

            # Merge env vars — add missing keys and replace empty values
            new_env = coerce_string_list(cfg.get("env")) or []
            if new_env:
                existing_by_key: dict[str, int] = {}
                for idx, e in enumerate(system.deploy.env):
                    if "=" in e:
                        existing_by_key[e.split("=", 1)[0]] = idx
                for entry in new_env:
                    key = (entry.split("=", 1)[0] if "=" in entry else entry).strip()
                    if not key:
                        continue  # skip blank/whitespace-only entries
                    if key not in existing_by_key:
                        system.deploy.env.append(entry)
                        logger.debug("startup_configs: %s added env %s", sys_name, key)
                    else:
                        # Overwrite if existing value is empty
                        old_idx = existing_by_key[key]
                        old_val = system.deploy.env[old_idx].split("=", 1)[1] if "=" in system.deploy.env[old_idx] else ""
                        if not old_val.strip():
                            system.deploy.env[old_idx] = entry
                            logger.debug("startup_configs: %s replaced empty env %s", sys_name, key)

            # Set healthcheck only if not already defined
            if not system.deploy.healthcheck and isinstance(cfg.get("healthcheck"), dict):
                hc_raw = cfg["healthcheck"]
                test_val = hc_raw.get("test")
                if test_val:
                    try:
                        retries = int(hc_raw.get("retries", 3))
                    except (ValueError, TypeError):
                        retries = 3
                    system.deploy.healthcheck = {
                        "test": list(test_val) if isinstance(test_val, list) else ["CMD-SHELL", str(test_val)],
                        "interval": str(hc_raw.get("interval", _HC_DEFAULT_INTERVAL)),
                        "timeout": str(hc_raw.get("timeout", _HC_DEFAULT_TIMEOUT)),
                        "retries": retries,
                        "start_period": str(hc_raw.get("start_period", _HC_DEFAULT_START_PERIOD)),
                    }
                    logger.debug("startup_configs: %s healthcheck set", sys_name)

    async def repair_images(
        self,
        world_model: WorldModel,
        failed_images: list[dict],
    ) -> WorldModel:
        """Ask the LLM to suggest replacement images for failed ones."""
        system_msg, user_msg = build_image_repair_prompt(failed_images)
        response, _usage = await self.llm.generate_with_tools(
            user_msg,
            tools=[_VALIDATE_IMAGE_TOOL, _FETCH_IMAGE_CONFIG_TOOL],
            tool_executor=_image_tool_executor,
            system_message=system_msg,
        )
        raw = extract_yaml_from_response(response)

        replacements = raw.get("replacements", {})
        if not isinstance(replacements, dict):
            logger.warning("LLM returned invalid replacements format")
            return world_model

        # Only replace images on systems that actually failed — not all systems
        # sharing the same image.  Match by system name from the failed list,
        # falling back to image match only for systems that are in the failed set.
        failed_system_names = {
            f["system_name"]
            for f in failed_images
            if isinstance(f, dict) and f.get("system_name")
        }
        failed_image_set = {f["image"] for f in failed_images if isinstance(f, dict) and f.get("image")}
        for sys_name, system in world_model.systems.items():
            if not system.deploy or system.deploy.image not in replacements:
                continue
            if sys_name not in failed_system_names and system.deploy.image not in failed_image_set:
                continue
            if sys_name not in failed_system_names:
                # This system shares the image but was not itself reported as failed — skip
                logger.debug(
                    "Skipping image replacement for %s (shares image %s with a failing system but was not itself failing)",
                    sys_name, system.deploy.image,
                )
                continue
            old = system.deploy.image
            new = str(replacements[old])
            logger.info("Replacing image %s -> %s (system %s)", old, new, sys_name)
            system.deploy.image = new

        return world_model

    async def repair_containers(
        self,
        world_model: WorldModel,
        failing_containers: list[dict],
    ) -> WorldModel:
        """Ask the LLM to suggest fixes for containers that failed to start.

        failing_containers: list of dicts with keys:
            system_name, image, command, error, zone
        """
        system_msg, user_msg = await build_container_repair_prompt(failing_containers)
        response, _usage = await self.llm.generate_with_tools(
            user_msg,
            tools=[_FETCH_IMAGE_CONFIG_TOOL, _VALIDATE_IMAGE_TOOL],
            tool_executor=_image_tool_executor,
            system_message=system_msg,
        )
        raw = extract_yaml_from_response(response)

        fixes = raw.get("fixes", {})
        if not isinstance(fixes, dict):
            logger.warning("LLM returned invalid container fixes format")
            return world_model

        for sys_name, fix_data in fixes.items():
            if not isinstance(fix_data, dict):
                continue
            system = world_model.systems.get(sys_name)
            if not system or not system.deploy:
                logger.warning("Fix references unknown system '%s' — skipping", sys_name)
                continue

            # Apply command fix
            if "command" in fix_data:
                old_cmd = system.deploy.command
                new_cmd = fix_data["command"]
                if new_cmd is None:
                    system.deploy.command = None
                    logger.info("Container repair: %s command set to null (was %r)", sys_name, old_cmd)
                elif isinstance(new_cmd, list):
                    system.deploy.command = coerce_string_list(new_cmd) or None
                    logger.info("Container repair: %s command → %r", sys_name, system.deploy.command)

            # Apply image fix
            if "image" in fix_data and fix_data["image"]:
                old_img = system.deploy.image
                system.deploy.image = str(fix_data["image"])
                logger.info("Container repair: %s image %s → %s", sys_name, old_img, system.deploy.image)

            # Apply env fix — add missing keys and replace existing values
            if "env" in fix_data and isinstance(fix_data["env"], list):
                existing_by_key: dict[str, int] = {}
                for idx, e in enumerate(system.deploy.env):
                    if "=" in e:
                        existing_by_key[e.split("=", 1)[0]] = idx
                for env_entry in fix_data["env"]:
                    env_str = str(env_entry)
                    key = env_str.split("=", 1)[0] if "=" in env_str else env_str
                    if key not in existing_by_key:
                        system.deploy.env.append(env_str)
                        logger.info("Container repair: %s added env %s", sys_name, key)
                    elif system.deploy.env[existing_by_key[key]] != env_str:
                        old_val = system.deploy.env[existing_by_key[key]]
                        system.deploy.env[existing_by_key[key]] = env_str
                        logger.info("Container repair: %s updated env %s (was %s)", sys_name, key, old_val.split("=", 1)[1][:30] if "=" in old_val else "?")

        return world_model

    @staticmethod
    def _parse_zone_simulate(zone_data: dict) -> "ZoneSimulate | None":
        """Parse zone simulate section, supporting both nested and flat formats."""
        sim = zone_data.get("simulate")
        exposure_flat = zone_data.get("exposure")
        if not sim and not exposure_flat:
            return None
        if isinstance(sim, dict):
            return ZoneSimulate(
                exposure=str(sim.get("exposure", exposure_flat or "internal")),
                sensitivity=str(sim.get("sensitivity", "medium")),
                trust_level=str(sim.get("trust_level", "medium")),
            )
        # Flat format: exposure at zone level
        return ZoneSimulate(
            exposure=str(exposure_flat or "internal"),
        )

    def _build_world_model(self, raw: dict) -> WorldModel:
        """Convert parsed YAML dict into a WorldModel."""
        wm = WorldModel()

        # Organization
        org_data = raw.get("organization")
        if isinstance(org_data, dict):
            wm.organization = Organization(
                name=str(org_data.get("name", "Honeynet Corp")),
                type=str(org_data.get("type", "")),
            )
        elif isinstance(org_data, str):
            wm.organization = Organization(name=org_data)

        # Zones
        zones_raw = raw.get("zones", {})
        if isinstance(zones_raw, dict):
            for zone_name, zone_data in zones_raw.items():
                zone_name = str(zone_name).strip()
                if not zone_name:
                    continue
                if isinstance(zone_data, dict):
                    wm.zones[zone_name] = WorldZone(
                        name=zone_name,
                        deploy=ZoneDeploy(
                            network_name=str(zone_data.get("network_name", zone_name)),
                            driver=str(zone_data.get("driver", "bridge")),
                            internal=coerce_bool(zone_data.get("internal", True), True),
                        ),
                        simulate=self._parse_zone_simulate(zone_data),
                    )
                else:
                    wm.zones[zone_name] = WorldZone(
                        name=zone_name,
                        deploy=ZoneDeploy(network_name=zone_name),
                    )

        # Systems
        systems_raw = raw.get("systems", {})
        if isinstance(systems_raw, dict):
            for sys_name, sys_data in systems_raw.items():
                sys_name = str(sys_name).strip()
                if not sys_name or not isinstance(sys_data, dict):
                    continue

                kind = SystemKind.coerce(str(sys_data.get("kind", "unknown")))

                # Deploy section — created when ``image`` is present OR when
                # ``catalog_archetype`` can later resolve to an image.
                deploy = None
                deploy_raw = sys_data.get("deploy", {})
                has_image = isinstance(deploy_raw, dict) and deploy_raw.get("image")
                has_archetype = isinstance(deploy_raw, dict) and deploy_raw.get("catalog_archetype")
                if has_image or has_archetype:
                    healthcheck_raw = deploy_raw.get("healthcheck")
                    healthcheck = None
                    if isinstance(healthcheck_raw, dict) and healthcheck_raw.get("test"):
                        try:
                            retries = int(healthcheck_raw.get("retries", 3))
                        except (ValueError, TypeError):
                            retries = 3
                        test_val = healthcheck_raw["test"]
                        healthcheck = {
                            "test": list(test_val) if isinstance(test_val, list) else ["CMD-SHELL", str(test_val)],
                            "interval": str(healthcheck_raw.get("interval", _HC_DEFAULT_INTERVAL)),
                            "timeout": str(healthcheck_raw.get("timeout", _HC_DEFAULT_TIMEOUT)),
                            "retries": retries,
                            "start_period": str(healthcheck_raw.get("start_period", "30s")),
                        }

                    ca = deploy_raw.get("catalog_archetype")
                    catalog_archetype = str(ca).strip() if ca else None

                    # Zone fallback: LLM may place zone at system level
                    # (especially companion systems from Phase 3) rather than
                    # inside the deploy block.  Check both locations.
                    zone_val = deploy_raw.get("zone") or sys_data.get("zone") or ""
                    deploy = SystemDeploy(
                        image=str(deploy_raw.get("image", "")),
                        zone=str(zone_val),
                        ports=coerce_int_list(deploy_raw.get("ports")),
                        env=coerce_string_list(deploy_raw.get("env")),
                        volumes=coerce_volume_mounts(deploy_raw.get("volumes")),
                        command=coerce_string_list(deploy_raw.get("command")) or None,
                        # depends_on fallback: Phase 1 skeleton places depends_on
                        # at system level, not inside deploy.  Check both.
                        depends_on=coerce_string_list(
                            deploy_raw.get("depends_on") or sys_data.get("depends_on")
                        ),
                        healthcheck=healthcheck,
                        catalog_archetype=catalog_archetype,
                    )

                # Simulate section
                simulate = None
                sim_raw = sys_data.get("simulate", {})
                if isinstance(sim_raw, dict):
                    simulate = SystemSimulate(
                        hostname=str(sim_raw.get("hostname", "")),
                        role=str(sim_raw.get("role", "")),
                        issues=coerce_string_list(sim_raw.get("issues")),
                        secrets=coerce_string_list(sim_raw.get("secrets")),
                        behaviors=coerce_string_list(sim_raw.get("behaviors")),
                        services=coerce_string_list(sim_raw.get("services")),
                    )

                wm.systems[sys_name] = System(
                    name=sys_name,
                    kind=kind,
                    owner=str(sys_data.get("owner", "")) or None,
                    deploy=deploy,
                    simulate=simulate,
                )

        # Secrets
        secrets_raw = raw.get("secrets", {})
        if isinstance(secrets_raw, dict):
            for sec_name, sec_data in secrets_raw.items():
                if isinstance(sec_data, dict):
                    wm.secrets[sec_name] = Secret(
                        name=str(sec_name),
                        type=str(sec_data.get("type", "password")),
                        value=str(sec_data.get("value", "")),
                    )

        # Guard: strip deploy config from systems that have neither an image
        # nor a catalog_archetype — they cannot be deployed.
        for sys_name, sys in wm.systems.items():
            if sys.deploy and not sys.deploy.image and not sys.deploy.catalog_archetype:
                logger.warning(
                    "System '%s' has deploy config but no image and no catalog_archetype "
                    "— marking as non-deployable (deploy=None)",
                    sys_name,
                )
                sys.deploy = None

        return wm
