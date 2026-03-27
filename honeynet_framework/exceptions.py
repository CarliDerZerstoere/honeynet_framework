"""Exception hierarchy for the Honeynet Framework.

This module defines a structured exception hierarchy to replace bare
`except Exception:` blocks throughout the codebase. Using specific
exception types enables:

1. Better error handling - catch only what you can handle
2. Clearer error messages - know exactly what went wrong
3. Better debugging - stack traces show the actual cause
4. Better testing - verify specific failure modes

Usage:
    from honeynet_framework.exceptions import (
        HoneynetError,  # Base for all framework exceptions
        ConfigurationError,
        DeploymentError,
        CatalogError,
        ExtractionError,
        ValidationError,
        LLMError,
    )

    try:
        deploy_container(...)
    except DeploymentTimeoutError as e:
        logger.warning("Deployment timed out: %s", e)
        # Retry or fallback
    except DeploymentError as e:
        logger.error("Deployment failed: %s", e)
        # General deployment failure handling
"""

from __future__ import annotations


class HoneynetError(Exception):
    """Base exception for all Honeynet Framework errors.

    All framework-specific exceptions should inherit from this class.
    This allows catching all framework errors with a single except clause:

        try:
            run_pipeline(...)
        except HoneynetError as e:
            logger.error("Pipeline failed: %s", e)
    """

    def __init__(self, message: str, cause: Exception | None = None):
        super().__init__(message)
        self.message = message
        self.cause = cause

    def __str__(self) -> str:
        if self.cause:
            return f"{self.message} (caused by: {self.cause})"
        return self.message


# ---------------------------------------------------------------------------
# Configuration Errors
# ---------------------------------------------------------------------------


class ConfigurationError(HoneynetError):
    """Error in configuration or settings."""

    pass


class MissingConfigError(ConfigurationError):
    """Required configuration value is missing."""

    def __init__(self, key: str, hint: str = ""):
        message = f"Missing required configuration: {key}"
        if hint:
            message += f". {hint}"
        super().__init__(message)
        self.key = key


class InvalidConfigError(ConfigurationError):
    """Configuration value is invalid."""

    def __init__(self, key: str, value: str, reason: str = ""):
        message = f"Invalid configuration for '{key}': {value}"
        if reason:
            message += f". {reason}"
        super().__init__(message)
        self.key = key
        self.value = value


# ---------------------------------------------------------------------------
# Deployment Errors
# ---------------------------------------------------------------------------


class DeploymentError(HoneynetError):
    """Base class for deployment-related errors."""

    pass


class DeploymentTimeoutError(DeploymentError):
    """Deployment operation timed out."""

    def __init__(self, operation: str, timeout_seconds: float, details: str = ""):
        message = f"Deployment operation '{operation}' timed out after {timeout_seconds:.1f}s"
        if details:
            message += f": {details}"
        super().__init__(message)
        self.operation = operation
        self.timeout_seconds = timeout_seconds


class ContainerStartError(DeploymentError):
    """Container failed to start."""

    def __init__(self, container_name: str, exit_code: int | None = None, logs: str = ""):
        message = f"Container '{container_name}' failed to start"
        if exit_code is not None:
            message += f" (exit code: {exit_code})"
        super().__init__(message)
        self.container_name = container_name
        self.exit_code = exit_code
        self.logs = logs


class HealthCheckError(DeploymentError):
    """Container health check failed."""

    def __init__(self, container_name: str, attempts: int = 0, last_status: str = ""):
        message = f"Health check failed for container '{container_name}'"
        if attempts:
            message += f" after {attempts} attempts"
        if last_status:
            message += f": {last_status}"
        super().__init__(message)
        self.container_name = container_name
        self.attempts = attempts


class TerraformError(DeploymentError):
    """Terraform/OpenTofu operation failed."""

    def __init__(self, operation: str, exit_code: int, stderr: str = ""):
        message = f"Terraform {operation} failed (exit code: {exit_code})"
        if stderr:
            # Truncate long stderr
            truncated = stderr[:500] + "..." if len(stderr) > 500 else stderr
            message += f": {truncated}"
        super().__init__(message)
        self.operation = operation
        self.exit_code = exit_code
        self.stderr = stderr


# ---------------------------------------------------------------------------
# Catalog Errors
# ---------------------------------------------------------------------------


class CatalogError(HoneynetError):
    """Base class for catalog-related errors."""

    pass


class CatalogNotFoundError(CatalogError):
    """Catalog or catalog entry not found."""

    def __init__(self, item: str, catalog_path: str = ""):
        message = f"Catalog item not found: {item}"
        if catalog_path:
            message += f" (searched in: {catalog_path})"
        super().__init__(message)
        self.item = item


class ArchetypeNotFoundError(CatalogError):
    """Archetype not found in catalog."""

    def __init__(self, archetype: str, suggestions: list[str] | None = None):
        message = f"Archetype not found: {archetype}"
        if suggestions:
            message += f". Did you mean: {', '.join(suggestions[:3])}?"
        super().__init__(message)
        self.archetype = archetype
        self.suggestions = suggestions or []


class ImageResolutionError(CatalogError):
    """Failed to resolve container image."""

    def __init__(self, image_ref: str, reason: str = ""):
        message = f"Failed to resolve image: {image_ref}"
        if reason:
            message += f". {reason}"
        super().__init__(message)
        self.image_ref = image_ref


# ---------------------------------------------------------------------------
# Extraction Errors
# ---------------------------------------------------------------------------


class ExtractionError(HoneynetError):
    """Base class for extraction-related errors."""

    pass


class ParseError(ExtractionError):
    """Failed to parse extracted content."""

    def __init__(self, content_type: str, reason: str = "", raw_content: str = ""):
        message = f"Failed to parse {content_type}"
        if reason:
            message += f": {reason}"
        super().__init__(message)
        self.content_type = content_type
        self.raw_content = raw_content


class ScopeExtractionError(ExtractionError):
    """Failed to extract scope intent from user request."""

    def __init__(self, reason: str, user_request: str = ""):
        message = f"Failed to extract scope intent: {reason}"
        super().__init__(message)
        self.user_request = user_request[:200] if user_request else ""


# ---------------------------------------------------------------------------
# Validation Errors
# ---------------------------------------------------------------------------


class WorldModelValidationError(HoneynetError):
    """World model validation failed."""

    def __init__(self, rule: str, details: str, fix_hint: str = ""):
        message = f"Validation failed [{rule}]: {details}"
        if fix_hint:
            message += f". Fix: {fix_hint}"
        super().__init__(message)
        self.rule = rule
        self.details = details
        self.fix_hint = fix_hint


class RepairError(HoneynetError):
    """World model repair failed."""

    def __init__(self, rule: str, system_name: str, reason: str = ""):
        message = f"Failed to repair [{rule}] for system '{system_name}'"
        if reason:
            message += f": {reason}"
        super().__init__(message)
        self.rule = rule
        self.system_name = system_name


# ---------------------------------------------------------------------------
# LLM Errors
# ---------------------------------------------------------------------------


class LLMError(HoneynetError):
    """Base class for LLM-related errors."""

    pass


class LLMConnectionError(LLMError):
    """Failed to connect to LLM provider."""

    def __init__(self, provider: str, base_url: str, cause: Exception | None = None):
        message = f"Failed to connect to {provider} at {base_url}"
        super().__init__(message, cause)
        self.provider = provider
        self.base_url = base_url


class LLMTimeoutError(LLMError):
    """LLM request timed out."""

    def __init__(self, provider: str, timeout_seconds: float, operation: str = "generate"):
        message = f"LLM {operation} timed out after {timeout_seconds:.1f}s ({provider})"
        super().__init__(message)
        self.provider = provider
        self.timeout_seconds = timeout_seconds
        self.operation = operation


class LLMResponseError(LLMError):
    """LLM returned an invalid or unexpected response."""

    def __init__(self, provider: str, status_code: int | None = None, body: str = ""):
        message = f"Invalid response from {provider}"
        if status_code is not None:
            message += f" (HTTP {status_code})"
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.body = body[:500] if body else ""


class ModelNotFoundError(LLMError):
    """Requested LLM model not found."""

    def __init__(self, model: str, provider: str, available_models: list[str] | None = None):
        message = f"Model '{model}' not found on {provider}"
        if available_models:
            message += f". Available: {', '.join(available_models[:5])}"
        super().__init__(message)
        self.model = model
        self.provider = provider
        self.available_models = available_models or []


# ---------------------------------------------------------------------------
# Docker Errors
# ---------------------------------------------------------------------------


class DockerError(HoneynetError):
    """Base class for Docker-related errors."""

    pass


class DockerNotAvailableError(DockerError):
    """Docker daemon is not available."""

    def __init__(self, reason: str = ""):
        message = "Docker daemon is not available"
        if reason:
            message += f": {reason}"
        super().__init__(message)


class DockerCommandError(DockerError):
    """Docker command failed."""

    def __init__(self, command: str, exit_code: int, stderr: str = ""):
        message = f"Docker command failed: {command} (exit code: {exit_code})"
        super().__init__(message)
        self.command = command
        self.exit_code = exit_code
        self.stderr = stderr


# ---------------------------------------------------------------------------
# Network/IO Errors
# ---------------------------------------------------------------------------


class NetworkError(HoneynetError):
    """Network-related error."""

    def __init__(self, operation: str, url: str = "", cause: Exception | None = None):
        message = f"Network error during {operation}"
        if url:
            message += f" ({url})"
        super().__init__(message, cause)
        self.operation = operation
        self.url = url


class FileIOError(HoneynetError):
    """File I/O error."""

    def __init__(self, operation: str, path: str, cause: Exception | None = None):
        message = f"File {operation} failed: {path}"
        super().__init__(message, cause)
        self.operation = operation
        self.path = path
