"""
Validation result models.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ValidationError:
    """A validation error or warning."""
    rule: str
    details: str
    severity: str = "ERROR"
    fix_hint: Optional[str] = None


@dataclass
class ValidationResult:
    """Result of a validation step."""
    passed: bool
    errors: list[ValidationError] = field(default_factory=list)
    warnings: list[ValidationError] = field(default_factory=list)

    @property
    def status(self) -> str:
        return "APPROVED" if self.passed else "REJECTED"
