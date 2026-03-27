"""Tests for the multi-phase extraction architecture.

Tests mock all LLM calls — no network/Docker required.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from honeynet_framework.extraction.multi_phase import MultiPhaseExtractor
from honeynet_framework.extraction.extractor import WorldModelExtractor
from honeynet_framework.llm import LLMConfig, TokenUsage


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

PHASE1_SKELETON_YAML = """\
organization:
  name: "Test Corp"

zones:
  dmz:
    internal: false
  internal:
    internal: true

systems:
  web_proxy:
    kind: web
    zone: dmz
    depends_on: [api_server]
  api_server:
    kind: runtime
    zone: internal
    depends_on: [main_db]
  main_db:
    kind: database
    zone: internal
    depends_on: []
  cache:
    kind: database
    zone: internal
    depends_on: []
"""

PHASE2_CONFIG_YAML = """\
systems:
  web_proxy:
    deploy:
      image: "nginx:1.25-alpine"
      ports: [80]
      env: ["NGINX_HOST=test.local"]
      command: null
      healthcheck:
        test: ["CMD-SHELL", "curl -sf http://localhost/ || exit 1"]
        interval: "30s"
        timeout: "10s"
        retries: 3
  api_server:
    deploy:
      image: "traefik:v3.0"
      ports: [8080]
      env: []
      command: null
      healthcheck:
        test: ["CMD-SHELL", "true"]
        interval: "30s"
        timeout: "10s"
        retries: 3
  main_db:
    deploy:
      image: "postgres:16-alpine"
      ports: [5432]
      env: ["POSTGRES_PASSWORD=admin123"]
      command: null
      healthcheck:
        test: ["CMD", "pg_isready", "-U", "postgres"]
        interval: "30s"
        timeout: "10s"
        retries: 3
  cache:
    deploy:
      image: "redis:7-alpine"
      ports: [6379]
      env: []
      command: null
      healthcheck:
        test: ["CMD", "redis-cli", "ping"]
        interval: "30s"
        timeout: "10s"
        retries: 3
"""

PHASE3_ENRICH_YAML = """\
systems:
  web_proxy:
    simulate:
      hostname: "proxy01.dmz.testcorp.local"
      role: "Reverse Proxy"
  api_server:
    simulate:
      hostname: "api01.core.testcorp.internal"
      role: "API Gateway"
  main_db:
    simulate:
      hostname: "db01.data.testcorp.internal"
      role: "Primary Database"
  cache:
    simulate:
      hostname: "cache01.data.testcorp.internal"
      role: "Session Cache"

secrets:
  db_admin_password:
    type: credential
    value: admin123

companion_systems:
  pgadmin:
    kind: web
    zone: internal
    depends_on: [main_db]
    deploy:
      image: "dpage/pgadmin4:latest"
      ports: [5050]
      env: ["PGADMIN_DEFAULT_EMAIL=admin@test.local", "PGADMIN_DEFAULT_PASSWORD=admin"]
      command: null
      healthcheck:
        test: ["CMD-SHELL", "true"]
        interval: "30s"
        timeout: "10s"
        retries: 3
    simulate:
      hostname: "pgadmin.ops.testcorp.internal"
      role: "Database Admin Panel"
"""


def _make_mock_llm(*responses):
    """Create a mock LLM provider that returns the given YAML strings in sequence."""
    llm = AsyncMock()
    llm.config = LLMConfig(provider="ollama", model="test", max_tokens=8192)

    call_count = 0
    async def _generate(prompt, system_message=None, **kwargs):
        nonlocal call_count
        idx = min(call_count, len(responses) - 1)
        call_count += 1
        return responses[idx], TokenUsage(prompt_tokens=100, completion_tokens=100)

    async def _generate_with_tools(prompt, tools, tool_executor, system_message=None, **kwargs):
        nonlocal call_count
        idx = min(call_count, len(responses) - 1)
        call_count += 1
        return responses[idx], TokenUsage(prompt_tokens=100, completion_tokens=100)

    llm.generate = _generate
    llm.generate_with_tools = _generate_with_tools
    return llm


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestMultiPhaseExtractor:
    """Test the MultiPhaseExtractor class."""

    def test_phase1_produces_valid_skeleton(self):
        """Phase 1 should parse skeleton YAML into a dict with systems and zones."""
        llm = _make_mock_llm(PHASE1_SKELETON_YAML)
        mp = MultiPhaseExtractor(llm)
        result = asyncio.run(mp._phase1_architecture("test prompt", None))
        assert "systems" in result
        assert "zones" in result
        assert len(result["systems"]) == 4
        assert "web_proxy" in result["systems"]
        assert result["systems"]["web_proxy"]["kind"] == "web"

    def test_phase2_merges_deploy_configs(self):
        """Phase 2 should produce deploy configs that merge onto the skeleton."""
        # Phase 1 returns skeleton, Phase 2 returns config for each batch
        llm = _make_mock_llm(PHASE1_SKELETON_YAML, PHASE2_CONFIG_YAML)
        mp = MultiPhaseExtractor(llm)

        with patch.object(mp, "_prefetch_oci_configs", new=AsyncMock(return_value={})):
            skeleton = asyncio.run(mp._phase1_architecture("test prompt", None))
            configs = asyncio.run(mp._phase2_config(skeleton, "test prompt"))

        merged = mp._merge_skeleton_and_configs(skeleton, configs)
        assert "main_db" in merged["systems"]
        db_sys = merged["systems"]["main_db"]
        assert db_sys["deploy"]["image"] == "postgres:16-alpine"
        # Zone should be preserved from skeleton
        assert db_sys.get("zone") == "internal" or db_sys["deploy"].get("zone") == "internal"

    def test_phase3_adds_simulate_and_companions(self):
        """Phase 3 should add simulate blocks and companion systems."""
        import yaml
        skeleton = yaml.safe_load(PHASE1_SKELETON_YAML)
        configs = yaml.safe_load(PHASE2_CONFIG_YAML)
        enrichment = yaml.safe_load(PHASE3_ENRICH_YAML)

        mp = MultiPhaseExtractor(AsyncMock())
        merged = mp._merge_skeleton_and_configs(skeleton, configs)
        merged = mp._merge_enrichment(merged, enrichment)

        # Simulate blocks should be on existing systems
        assert merged["systems"]["web_proxy"]["simulate"]["hostname"] == "proxy01.dmz.testcorp.local"
        # Companion system should be added
        assert "pgadmin" in merged["systems"]
        assert merged["systems"]["pgadmin"]["deploy"]["image"] == "dpage/pgadmin4:latest"
        # Secrets should be merged
        assert "db_admin_password" in merged["secrets"]

    def test_phase3_failure_returns_valid_model(self):
        """If Phase 3 fails, Phase 1+2 WorldModel should still be returned."""
        # Phase 1 skeleton, Phase 2 config, Phase 3 fails with invalid YAML
        llm = _make_mock_llm(
            PHASE1_SKELETON_YAML,
            PHASE2_CONFIG_YAML,
            "THIS IS NOT VALID YAML {{{{",
        )
        mp = MultiPhaseExtractor(llm)

        with patch.object(mp, "_prefetch_oci_configs", new=AsyncMock(return_value={})):
            result = asyncio.run(mp.extract_raw("test prompt"))

        # Should succeed with Phase 1+2 data despite Phase 3 failure
        assert "systems" in result
        assert len(result["systems"]) >= 4
        assert result["systems"]["main_db"]["deploy"]["image"] == "postgres:16-alpine"

    def test_batching_groups_by_zone(self):
        """Systems in the same zone should be grouped together."""
        systems = [
            {"name": "a", "zone": "dmz"},
            {"name": "b", "zone": "internal"},
            {"name": "c", "zone": "dmz"},
            {"name": "d", "zone": "internal"},
            {"name": "e", "zone": "data"},
        ]
        batches = MultiPhaseExtractor._batch_systems(systems, max_batch_size=3)
        # dmz systems (a, c) should be in the same batch
        batch_names = [
            {s["name"] for s in batch} for batch in batches
        ]
        # At least a and c should be together
        found_dmz_batch = any(
            "a" in names and "c" in names for names in batch_names
        )
        assert found_dmz_batch

    def test_batching_respects_max_size(self):
        """No batch should exceed max_batch_size."""
        systems = [{"name": f"s{i}", "zone": "same"} for i in range(20)]
        batches = MultiPhaseExtractor._batch_systems(systems, max_batch_size=8)
        for batch in batches:
            assert len(batch) <= 8

    def test_full_multi_phase_pipeline(self):
        """Full pipeline: Phase 1 → 2 → 3 → merged dict."""
        llm = _make_mock_llm(
            PHASE1_SKELETON_YAML,
            PHASE2_CONFIG_YAML,
            PHASE3_ENRICH_YAML,
        )
        mp = MultiPhaseExtractor(llm)

        with patch.object(mp, "_prefetch_oci_configs", new=AsyncMock(return_value={})):
            result = asyncio.run(mp.extract_raw("banking core platform"))

        # Verify all phases contributed
        assert result["organization"]["name"] == "Test Corp"
        assert "dmz" in result["zones"]
        assert result["systems"]["main_db"]["deploy"]["image"] == "postgres:16-alpine"
        assert result["systems"]["web_proxy"]["simulate"]["hostname"] == "proxy01.dmz.testcorp.local"
        assert "pgadmin" in result["systems"]  # companion from Phase 3
        assert "db_admin_password" in result.get("secrets", {})


class TestMultiPhaseFlag:
    """Test that the multi_phase flag gates behavior correctly."""

    def test_flag_false_uses_single_call(self):
        """multi_phase=False should use the existing single-call path."""
        llm = AsyncMock()
        llm.config = LLMConfig()
        ext = WorldModelExtractor(llm, multi_phase=False)
        assert ext.multi_phase is False

    def test_flag_true_enables_multi_phase(self):
        """multi_phase=True should be stored on the extractor."""
        llm = AsyncMock()
        llm.config = LLMConfig()
        ext = WorldModelExtractor(llm, multi_phase=True)
        assert ext.multi_phase is True


class TestPhasePrompts:
    """Test that phase prompt builders produce valid output."""

    def test_phase1_prompt_contains_scale_guidance(self):
        from honeynet_framework.extraction.prompts import build_phase1_architecture_prompt
        sys_msg, user_msg, max_sys = build_phase1_architecture_prompt(
            "Create a banking honeynet with postgres, kafka, nginx"
        )
        assert "SCALE GUIDANCE" in user_msg
        assert max_sys > 0
        assert "skeleton" in user_msg.lower() or "topology" in user_msg.lower()

    def test_phase2_prompt_lists_systems(self):
        from honeynet_framework.extraction.prompts import build_phase2_config_prompt
        batch = [
            {"name": "db", "kind": "database", "zone": "internal", "depends_on": []},
            {"name": "web", "kind": "web", "zone": "dmz", "depends_on": ["db"]},
        ]
        sys_msg, user_msg = build_phase2_config_prompt(batch, {}, 0, 1)
        assert "system: db" in user_msg
        assert "system: web" in user_msg
        assert "batch 1/1" in user_msg

    def test_phase3_prompt_lists_existing_systems(self):
        from honeynet_framework.extraction.prompts import build_phase3_enrich_prompt
        systems = [
            {"name": "db", "kind": "database", "zone": "internal", "image": "postgres:16"},
        ]
        sys_msg, user_msg = build_phase3_enrich_prompt(
            systems, "banking honeynet", "Acme Bank", ["dmz", "internal"]
        )
        assert "Acme Bank" in user_msg
        assert "db" in user_msg
        assert "postgres:16" in user_msg
