"""
Pipeline and observability enums (failure stages, telemetry contract).
"""

from enum import Enum


class FailureStage(str, Enum):
    """Machine-readable stage where a deploy run stopped or completed."""

    NONE = "none"
    COMPLETED = "completed"
    PARTIAL_COMPLETED = "partial_completed"
    EXTRACTION = "extraction"
    WORLD_MODEL_VALIDATION = "world_model_validation"
    SEMANTIC_JUDGE = "semantic_judge"
    IMAGE_RESOLUTION = "image_resolution"
    DEPLOY_COMPILATION = "deploy_compilation"
    INIT = "init"
    FMT = "fmt"
    VALIDATE = "validate"
    PLAN = "plan"
    APPLY = "apply"
    RENDER = "render"
    CLEANUP = "cleanup"
    RUNTIME_VERIFY = "runtime_verify"
    STARTUP_PROBE = "startup_probe"
    UNKNOWN = "unknown"
    ARTIFACT_WRITE = "artifact_write"
    CATALOG_SNAPSHOT = "catalog_snapshot"
