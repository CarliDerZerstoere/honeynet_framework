"""
Structured failure reporting for validation and pipeline errors (P2).

Extends the existing ``ValidationError``/``ValidationResult`` with machine-readable
classification (``FailureType``) and a serializable ``FailureReport`` for
``validation_report.json`` artifacts.

Does NOT replace ``validator.py`` — the validator produces ``ValidationResult``;
this module converts it into a richer report when desired.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .models.validation import ValidationError, ValidationResult


class ValidationFailureType(str, Enum):
    """Machine-readable failure classification for validation errors.

    Renamed from FailureType to avoid collision with
    ``repair_types.FailureType`` which classifies *runtime* failures.
    """
    STRUCTURAL = "structural"
    REFERENCE = "reference"
    CONSTRAINT = "constraint"
    CYCLE = "cycle"
    IMAGE = "image"
    POLICY = "policy"
    FORMAT = "format"
    PLUGIN = "plugin"
    UNKNOWN = "unknown"


# Backwards-compatible alias so existing imports keep working.
FailureType = ValidationFailureType


class FailureSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


# Map validator rule names to FailureType for classification
_RULE_TO_TYPE: dict[str, FailureType] = {
    "MIN_ZONES": FailureType.STRUCTURAL,
    "MIN_SYSTEMS": FailureType.STRUCTURAL,
    "UNIQUE_NETWORK_NAME": FailureType.CONSTRAINT,
    "REF_ZONE": FailureType.REFERENCE,
    "REQUIRED_IMAGE": FailureType.STRUCTURAL,
    "REF_DEPENDS_ON": FailureType.REFERENCE,
    "SELF_DEPENDENCY": FailureType.CYCLE,
    "PORT_RANGE": FailureType.CONSTRAINT,
    "CIRCULAR_DEPENDENCY": FailureType.CYCLE,
    # Command policy violations (structural errors, auto-repaired)
    "INVALID_COMMAND": FailureType.POLICY,
    # Warning rules (promotable to errors) — classify as policy/runtime contract
    "ONE_SHOT_COMMAND": FailureType.POLICY,
    "FICTIONAL_SCRIPT": FailureType.POLICY,
    "MISSING_REQUIRED_CONFIG": FailureType.POLICY,
    "HEALTHCHECK_BINARY_MISMATCH": FailureType.IMAGE,
}


@dataclass
class ClassifiedError:
    """A validation error enriched with failure type classification."""
    rule: str
    details: str
    failure_type: FailureType
    severity: FailureSeverity = FailureSeverity.ERROR
    fix_hint: Optional[str] = None

    def to_dict(self) -> dict:
        d = {
            "rule": self.rule,
            "details": self.details,
            "failure_type": self.failure_type.value,
            "severity": self.severity.value,
        }
        if self.fix_hint:
            d["fix_hint"] = self.fix_hint
        return d


@dataclass
class FailureReport:
    """Serializable failure report for validation_report.json."""
    passed: bool
    total_errors: int = 0
    total_warnings: int = 0
    errors: list[ClassifiedError] = field(default_factory=list)
    warnings: list[ClassifiedError] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "total_errors": self.total_errors,
            "total_warnings": self.total_warnings,
            "errors": [e.to_dict() for e in self.errors],
            "warnings": [w.to_dict() for w in self.warnings],
        }

    def summary(self) -> str:
        if self.passed:
            return "Validation passed"
        lines = [f"Validation failed: {self.total_errors} error(s), {self.total_warnings} warning(s)"]
        for e in self.errors[:10]:
            lines.append(f"  [{e.failure_type.value}] {e.rule}: {e.details}")
        return "\n".join(lines)


def classify_validation_error(ve: ValidationError) -> ClassifiedError:
    """Convert a ValidationError to a ClassifiedError with type classification."""
    failure_type = _RULE_TO_TYPE.get(ve.rule, FailureType.UNKNOWN)
    _severity_map = {
        "WARNING": FailureSeverity.WARNING,
        "INFO": FailureSeverity.INFO,
    }
    severity = _severity_map.get(ve.severity, FailureSeverity.ERROR)
    return ClassifiedError(
        rule=ve.rule,
        details=ve.details,
        failure_type=failure_type,
        severity=severity,
        fix_hint=ve.fix_hint,
    )


def from_validation_result(result: ValidationResult) -> FailureReport:
    """Convert a ValidationResult into a FailureReport with classified errors."""
    classified_errors = [classify_validation_error(e) for e in result.errors]
    classified_warnings = [classify_validation_error(w) for w in result.warnings]
    return FailureReport(
        passed=result.passed,
        total_errors=len(classified_errors),
        total_warnings=len(classified_warnings),
        errors=classified_errors,
        warnings=classified_warnings,
    )
