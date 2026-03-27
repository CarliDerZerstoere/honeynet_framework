"""Multi-phase extraction — splits WorldModel generation into 3 focused LLM calls.

Phase 1: Architecture Agent — topology skeleton (zones, systems, dependencies)
Phase 2: Config Agent — deploy configs per system (image, env, ports, healthcheck)
Phase 3: Enrich Agent — simulate blocks, secrets, companion systems

Each phase has a focused prompt and constrained output, eliminating the token
budget / truncation problem that causes hard scenarios to fail with a single call.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from ..llm import LLMProvider, TokenUsage
from ..retrieval import select_images_for_scenario
from ..image_resolver import fetch_image_config
from .yaml_parsing import extract_yaml_from_response
from .prompts import (
    build_phase1_architecture_prompt,
    build_phase2_config_prompt,
    build_phase3_enrich_prompt,
)

logger = logging.getLogger(__name__)

# Re-export the tool definitions and executor used during Phase 2.
_VALIDATE_IMAGE_TOOL_DEF = {
    "name": "validate_docker_image",
    "description": (
        "Check whether a Docker image:tag exists and is pullable from a registry. "
        "Call this for EVERY image you plan to include before writing your YAML."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "image_ref": {
                "type": "string",
                "description": "Full image reference, e.g. 'nginx:1.25-alpine'",
            }
        },
        "required": ["image_ref"],
    },
}

_FETCH_IMAGE_CONFIG_TOOL_DEF = {
    "name": "fetch_image_config",
    "description": (
        "Fetch the default Env, Cmd, Entrypoint and ExposedPorts for a Docker image."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "image_ref": {"type": "string", "description": "Full image reference"}
        },
        "required": ["image_ref"],
    },
}


async def _image_tool_executor(tool_name: str, tool_input: dict) -> str:
    """Execute image validation/config tools (shared with single-call extractor)."""
    from ..image_resolver import check_image_exists, fetch_image_config as _fetch

    if tool_name == "validate_docker_image":
        image_ref = tool_input.get("image_ref", "").strip()
        if not image_ref:
            return json.dumps({"error": "empty image_ref"})
        result = await check_image_exists(image_ref)
        return json.dumps(result)
    if tool_name == "fetch_image_config":
        image_ref = tool_input.get("image_ref", "").strip()
        if not image_ref:
            return json.dumps({"error": "empty image_ref"})
        result = await _fetch(image_ref)
        return json.dumps(result)
    return json.dumps({"error": f"unknown tool: {tool_name}"})


class MultiPhaseExtractor:
    """Three-phase extraction: Architecture → Config → Enrich.

    Returns a raw dict that can be passed to ``_build_world_model()`` in the
    same way as the single-call extractor's YAML output.
    """

    def __init__(self, llm_provider: LLMProvider) -> None:
        self.llm = llm_provider
        self.total_usage = TokenUsage()

    async def extract_raw(
        self,
        user_request: str,
        *,
        scenario_context: str | None = None,
    ) -> dict:
        """Run all 3 phases and return a merged raw dict."""
        # Phase 1: Architecture skeleton
        skeleton = await self._phase1_architecture(user_request, scenario_context)
        if not skeleton or not skeleton.get("systems"):
            raise ValueError(
                "Phase 1 (Architecture) produced empty or invalid skeleton"
            )
        logger.info(
            "Phase 1 complete: %d systems, %d zones",
            len(skeleton.get("systems", {})),
            len(skeleton.get("zones", {})),
        )

        # Phase 2: Deploy configs (batched)
        configs = await self._phase2_config(skeleton, user_request)
        merged = self._merge_skeleton_and_configs(skeleton, configs)
        logger.info(
            "Phase 2 complete: %d systems configured",
            sum(
                1 for s in merged.get("systems", {}).values()
                if isinstance(s, dict) and s.get("deploy", {}).get("image")
            ),
        )

        # Phase 3: Enrich (optional — failure is non-fatal)
        try:
            enrichment = await self._phase3_enrich(merged, user_request)
            merged = self._merge_enrichment(merged, enrichment)
            merged["_enrichment_applied"] = True
            logger.info("Phase 3 complete: enrichment applied")
        except Exception as exc:
            merged["_enrichment_applied"] = False
            logger.warning(
                "Phase 3 (Enrich) failed — WorldModel will lack simulate blocks: %s",
                exc,
            )

        return merged

    # ------------------------------------------------------------------
    # Phase 1: Architecture Agent
    # ------------------------------------------------------------------

    async def _phase1_architecture(
        self,
        user_request: str,
        scenario_context: str | None,
    ) -> dict:
        """Generate topology skeleton YAML (org, zones, systems with kind/zone/depends_on)."""
        system_msg, user_msg, max_sys = build_phase1_architecture_prompt(
            user_request, scenario_context,
        )
        token_budget = max_sys * 80 + 500
        return await self._call_with_retry(
            system_msg, user_msg, max_tokens=token_budget, phase_name="Phase 1",
        )

    # ------------------------------------------------------------------
    # Phase 2: Config Agent (batched)
    # ------------------------------------------------------------------

    async def _phase2_config(
        self, skeleton: dict, user_request: str,
    ) -> dict:
        """Enrich skeleton with deploy configs (image, env, ports, healthcheck)."""
        systems_dict = skeleton.get("systems", {})
        system_list = [
            {"name": name, **(info if isinstance(info, dict) else {})}
            for name, info in systems_dict.items()
        ]

        # Pre-fetch OCI configs for scenario-relevant images
        image_configs = await self._prefetch_oci_configs(user_request)

        # Batch systems by zone affinity
        batches = self._batch_systems(system_list)
        logger.info(
            "Phase 2: %d systems in %d batches", len(system_list), len(batches),
        )

        # Process each batch
        all_configs: dict = {"systems": {}}
        successful_batches = 0
        for i, batch in enumerate(batches):
            try:
                sys_msg, user_msg = build_phase2_config_prompt(
                    batch, image_configs, i, len(batches),
                )
                token_budget = len(batch) * 500 + 1000
                result = await self._call_with_retry(
                    sys_msg, user_msg,
                    max_tokens=token_budget,
                    phase_name=f"Phase 2 batch {i + 1}/{len(batches)}",
                    tools=[_VALIDATE_IMAGE_TOOL_DEF, _FETCH_IMAGE_CONFIG_TOOL_DEF],
                    tool_executor=_image_tool_executor,
                )
                for sys_name, sys_data in result.get("systems", {}).items():
                    all_configs["systems"][sys_name] = sys_data
                successful_batches += 1
            except Exception as exc:
                names = [s["name"] for s in batch]
                logger.warning(
                    "Phase 2 batch %d/%d failed (systems: %s): %s",
                    i + 1, len(batches), names, exc,
                )
                all_configs.setdefault("_dropped_systems", []).extend(names)

        if successful_batches == 0:
            raise ValueError("Phase 2 (Config) failed: all batches produced errors")

        if all_configs.get("_dropped_systems"):
            logger.warning(
                "Phase 2: %d system(s) dropped due to batch failures: %s",
                len(all_configs["_dropped_systems"]),
                all_configs["_dropped_systems"],
            )

        return all_configs

    # ------------------------------------------------------------------
    # Phase 3: Enrich Agent
    # ------------------------------------------------------------------

    async def _phase3_enrich(
        self, merged: dict, user_request: str,
    ) -> dict:
        """Add simulate blocks, secrets, and companion systems."""
        systems_dict = merged.get("systems", {})
        org_name = ""
        org = merged.get("organization")
        if isinstance(org, dict):
            org_name = org.get("name", "")
        elif isinstance(org, str):
            org_name = org

        zone_names = list(merged.get("zones", {}).keys())

        systems_summary = []
        for name, info in systems_dict.items():
            if not isinstance(info, dict):
                continue
            deploy = info.get("deploy", {}) if isinstance(info.get("deploy"), dict) else {}
            systems_summary.append({
                "name": name,
                "kind": info.get("kind", "unknown"),
                "zone": info.get("zone", deploy.get("zone", "")),
                "image": deploy.get("image", ""),
            })

        sys_msg, user_msg = build_phase3_enrich_prompt(
            systems_summary, user_request, org_name, zone_names,
        )
        token_budget = len(systems_summary) * 150 + 2000
        return await self._call_with_retry(
            sys_msg, user_msg, max_tokens=token_budget, phase_name="Phase 3",
        )

    # ------------------------------------------------------------------
    # Merging
    # ------------------------------------------------------------------

    def _merge_skeleton_and_configs(
        self, skeleton: dict, configs: dict,
    ) -> dict:
        """Overlay Phase 2 deploy configs onto Phase 1 skeleton."""
        merged = {
            "organization": skeleton.get("organization", {}),
            "zones": dict(skeleton.get("zones", {})),
            "systems": {},
            "secrets": skeleton.get("secrets", {}),
        }

        skeleton_systems = skeleton.get("systems", {})
        config_systems = configs.get("systems", {})

        for sys_name, skel_info in skeleton_systems.items():
            if not isinstance(skel_info, dict):
                skel_info = {}
            sys_entry = dict(skel_info)

            # Overlay deploy config from Phase 2
            cfg = config_systems.get(sys_name, {})
            if isinstance(cfg, dict):
                deploy = cfg.get("deploy", cfg)
                if isinstance(deploy, dict) and deploy.get("image"):
                    # Merge zone from skeleton into deploy
                    if "zone" not in deploy and "zone" in skel_info:
                        deploy["zone"] = skel_info["zone"]
                    # Merge depends_on from skeleton into deploy
                    if "depends_on" not in deploy and "depends_on" in skel_info:
                        deploy["depends_on"] = skel_info["depends_on"]
                    sys_entry["deploy"] = deploy

            # Ensure zone and depends_on are always set (from skeleton)
            if "deploy" in sys_entry and isinstance(sys_entry["deploy"], dict):
                if "zone" not in sys_entry["deploy"] and "zone" in skel_info:
                    sys_entry["deploy"]["zone"] = skel_info["zone"]
                if "depends_on" not in sys_entry["deploy"] and "depends_on" in skel_info:
                    sys_entry["deploy"]["depends_on"] = skel_info["depends_on"]

            merged["systems"][sys_name] = sys_entry

        # Systems that Phase 2 produced but weren't in skeleton (rare edge case)
        for sys_name, cfg in config_systems.items():
            if sys_name not in merged["systems"] and isinstance(cfg, dict):
                merged["systems"][sys_name] = cfg

        return merged

    def _merge_enrichment(self, merged: dict, enrichment: dict) -> dict:
        """Overlay Phase 3 simulate blocks, secrets, and companion systems."""
        enrich_systems = enrichment.get("systems", {})
        for sys_name, enrich_data in enrich_systems.items():
            if not isinstance(enrich_data, dict):
                continue
            if sys_name in merged["systems"]:
                # Add simulate block to existing system
                sim = enrich_data.get("simulate")
                if isinstance(sim, dict):
                    merged["systems"][sys_name]["simulate"] = sim
            # else: system in enrichment but not in merged — skip (companion systems are separate)

        # Merge secrets
        enrich_secrets = enrichment.get("secrets", {})
        if isinstance(enrich_secrets, dict):
            merged_secrets = merged.get("secrets", {})
            if not isinstance(merged_secrets, dict):
                merged_secrets = {}
            merged_secrets.update(enrich_secrets)
            merged["secrets"] = merged_secrets

        # Merge companion systems (new systems from Phase 3)
        # Ensure each companion has a zone assigned — the LLM often puts zone
        # at the system level but not inside the deploy block.  Propagate it,
        # and if still missing, assign the first internal zone as fallback.
        available_zones = list(merged.get("zones", {}).keys())
        internal_zones = [
            z for z, zd in merged.get("zones", {}).items()
            if isinstance(zd, dict) and zd.get("internal", True)
        ]
        fallback_zone = internal_zones[0] if internal_zones else (available_zones[0] if available_zones else "")

        companions = enrichment.get("companion_systems", {})
        if isinstance(companions, dict):
            for comp_name, comp_data in companions.items():
                if isinstance(comp_data, dict) and comp_name not in merged["systems"]:
                    # Ensure zone is set in deploy block
                    sys_zone = comp_data.get("zone", "")
                    deploy = comp_data.get("deploy")
                    if isinstance(deploy, dict):
                        if not deploy.get("zone") and sys_zone:
                            deploy["zone"] = sys_zone
                        elif not deploy.get("zone"):
                            deploy["zone"] = fallback_zone
                            logger.info(
                                "Phase 3 companion '%s' had no zone — assigned fallback '%s'",
                                comp_name, fallback_zone,
                            )
                    elif sys_zone:
                        # No deploy block yet — zone at system level is fine,
                        # _build_world_model will pick it up via fallback
                        pass
                    else:
                        # No zone anywhere — set at system level for fallback
                        comp_data["zone"] = fallback_zone
                        logger.info(
                            "Phase 3 companion '%s' had no zone — assigned fallback '%s'",
                            comp_name, fallback_zone,
                        )
                    merged["systems"][comp_name] = comp_data
                    logger.info("Phase 3 added companion system: %s", comp_name)

        return merged

    # ------------------------------------------------------------------
    # Batching
    # ------------------------------------------------------------------

    @staticmethod
    def _batch_systems(
        system_list: list[dict], max_batch_size: int = 8,
    ) -> list[list[dict]]:
        """Group systems by zone affinity, max ``max_batch_size`` per batch."""
        by_zone: dict[str, list[dict]] = {}
        for sys_info in system_list:
            zone = sys_info.get("zone", "__none__")
            by_zone.setdefault(zone, []).append(sys_info)

        batches: list[list[dict]] = []
        current_batch: list[dict] = []

        for zone_systems in by_zone.values():
            for sys_info in zone_systems:
                current_batch.append(sys_info)
                if len(current_batch) >= max_batch_size:
                    batches.append(current_batch)
                    current_batch = []

        if current_batch:
            batches.append(current_batch)

        return batches or [system_list]

    # ------------------------------------------------------------------
    # LLM call with retry
    # ------------------------------------------------------------------

    async def _call_with_retry(
        self,
        system_msg: str,
        user_msg: str,
        *,
        max_tokens: int = 8192,
        phase_name: str = "",
        max_retries: int = 1,
        tools: list[dict] | None = None,
        tool_executor=None,
    ) -> dict:
        """Call LLM, parse YAML, retry on parse failure."""
        last_error: Exception | None = None
        provider_cap = _provider_max_output_tokens(self.llm.config)
        effective_max_tokens = min(max(max_tokens, 2000), provider_cap)

        for attempt in range(1 + max_retries):
            try:
                if tools and tool_executor:
                    response, usage = await self.llm.generate_with_tools(
                        user_msg,
                        tools=tools,
                        tool_executor=tool_executor,
                        system_message=system_msg,
                        max_tokens=effective_max_tokens,
                    )
                else:
                    response, usage = await self.llm.generate(
                        user_msg,
                        system_message=system_msg,
                    )
                self.total_usage = self.total_usage + usage

                raw = extract_yaml_from_response(response)
                if not isinstance(raw, dict) or not raw:
                    raise ValueError(f"YAML parsed to empty or non-dict: {type(raw)}")
                return raw

            except Exception as exc:
                last_error = exc
                if attempt < max_retries:
                    logger.warning(
                        "%s attempt %d failed (%s) — retrying with error feedback",
                        phase_name, attempt + 1, exc,
                    )
                    # Self-refinement: feed the error back to the LLM
                    user_msg = (
                        f"Your previous output caused an error:\n{exc}\n\n"
                        f"Please fix the error and output valid YAML.\n\n"
                        f"Original request:\n{user_msg}"
                    )

        raise ValueError(
            f"{phase_name} failed after {1 + max_retries} attempts: {last_error}"
        ) from last_error

    # ------------------------------------------------------------------
    # OCI pre-fetch
    # ------------------------------------------------------------------

    async def _prefetch_oci_configs(
        self, user_request: str,
    ) -> dict[str, dict]:
        """Pre-fetch OCI configs for scenario-relevant images."""
        relevant_images = select_images_for_scenario(user_request)
        if not relevant_images:
            return {}

        image_configs: dict[str, dict] = {}
        sem = asyncio.Semaphore(5)

        async def _fetch_one(img: str) -> None:
            async with sem:
                try:
                    cfg = await fetch_image_config(img, timeout=10)
                    if "error" not in cfg:
                        image_configs[img] = cfg
                except Exception:
                    pass

        await asyncio.gather(*[_fetch_one(img) for img in relevant_images])
        logger.info(
            "Phase 2 pre-fetch: %d/%d OCI configs retrieved",
            len(image_configs), len(relevant_images),
        )
        return image_configs


def _provider_max_output_tokens(config) -> int:
    """Return the max output token limit for the configured provider."""
    caps = {
        "ollama": 32768,
        "openai": 16384,
        "anthropic": 32768,
    }
    return caps.get(config.provider, 32768)
