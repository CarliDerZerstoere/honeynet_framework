"""
Honeynet Framework - Generate and deploy honeynets from natural language prompts.

Usage:
    from honeynet_framework import HoneynetOrchestrator
    from honeynet_framework.orchestrator import OrchestratorConfig
    from honeynet_framework.llm import LLMConfig

    config = OrchestratorConfig(
        llm_config=LLMConfig(provider="ollama", model="qwen2.5-coder:32b"),
    )
    orchestrator = HoneynetOrchestrator(config)
    result = await orchestrator.deploy("Create a honeynet mimicking...")
"""

from .orchestrator import HoneynetOrchestrator, OrchestratorConfig
from .llm import LLMConfig
from .models import DeploymentResult, DeploymentStatus
from .enums_pipeline import FailureStage

__version__ = "0.3.0"
__all__ = [
    "HoneynetOrchestrator",
    "OrchestratorConfig",
    "LLMConfig",
    "DeploymentResult",
    "DeploymentStatus",
    "FailureStage",
]

# Submodule summary (for discoverability):
#   command_policy   — Deterministic command validation + repair per image type
#   image_patterns   — Shared image classification patterns (single source of truth)
#   pipeline_artifacts — File I/O for metrics, reports, and snapshots
#   repair_depends   — Fuzzy depends_on reference repair
#   detection/       — 3-zone attacker detection (ALLOW / REVIEW / LIKELY_AGENT)
