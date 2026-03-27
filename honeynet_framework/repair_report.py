"""
Structured repair/validation categories (distinct from models.ValidationError names).

Use for optional FailureReport JSON — avoids importing conflicting ``ValidationError`` types.
"""

from enum import Enum


class RepairFailureCategory(str, Enum):
    """High-level failure bucket for automation and reporting."""

    FORMAT_ERROR = "format_error"
    CONSTRAINT_VIOLATION = "constraint_violation"
    MISSING_RESOURCE = "missing_resource"
    POLICY = "policy"
    UNFIXABLE = "unfixable"
