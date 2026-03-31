"""
YAML extraction and repair utilities for LLM response parsing.

Handles markdown code blocks, common LLM mistakes, and YAML repair.
"""

import logging
import re

import yaml

logger = logging.getLogger(__name__)

_YAML_EQUALS_MAPPING_RE = re.compile(
    r'^\s*[a-z_][a-z0-9_-]*\s*=\s*.+\s*$',
    re.IGNORECASE,
)


def extract_yaml_from_response(text: str) -> dict:
    """
    Extract YAML from LLM response, handling markdown code blocks.

    Args:
        text: Raw LLM response

    Returns:
        Parsed YAML as dict

    Raises:
        ValueError: If YAML parsing fails
    """
    text = text.strip()

    # Try ```yaml ... ``` first
    yaml_match = re.search(r'```ya?ml\s*\n?([\s\S]*?)\n?```', text, re.IGNORECASE)
    if yaml_match:
        text = yaml_match.group(1).strip()
    else:
        # Try generic code block
        code_match = re.search(r'```\s*\n?([\s\S]*?)\n?```', text)
        if code_match:
            text = code_match.group(1).strip()

    # Remove any leading prose before YAML
    # YAML typically starts with a key: pattern
    lines = text.split('\n')
    yaml_start = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped and re.match(r'^[a-z_][a-z0-9_]*:', stripped, re.IGNORECASE):
            yaml_start = i
            break

    if yaml_start > 0:
        text = '\n'.join(lines[yaml_start:])

    if any(_YAML_EQUALS_MAPPING_RE.match(line) for line in text.split('\n')):
        text = _repair_yaml(text)

    try:
        return _load_yaml_mapping(text, source="LLM response")
    except ValueError as e:
        logger.warning(f"YAML parse error: {e}")
        repaired = _repair_yaml(text)
        try:
            return _load_yaml_mapping(repaired, source="YAML repair")
        except ValueError as e2:
            logger.error(f"YAML repair failed: {e2}")
            raise ValueError(f"Failed to parse YAML: {e2}") from e2


def _load_yaml_mapping(text: str, source: str) -> dict:
    """Load YAML and ensure the top-level document is a mapping.

    If the text contains multiple YAML documents (``---`` separators), all
    dict-typed documents are merged (later keys win).  This handles the common
    LLM pattern of splitting zones/systems/secrets across documents.
    """
    try:
        docs = list(yaml.safe_load_all(text))
    except yaml.YAMLError as exc:
        raise ValueError(str(exc)) from exc

    # Filter out None documents (bare ``---`` separators)
    docs = [d for d in docs if d is not None]
    if not docs:
        raise ValueError(f"{source} produced empty document.")

    # Merge multiple dict documents; reject non-dict documents.
    # Deep-merge known collection keys so that LLM multi-document splits
    # (e.g. systems in doc 1 + more systems in doc 2) are combined rather
    # than overwritten by a flat dict.update().
    _DEEP_MERGE_KEYS = {"systems", "zones", "secrets"}
    result: dict = {}
    for doc in docs:
        if isinstance(doc, dict):
            for key in _DEEP_MERGE_KEYS:
                if (
                    key in result
                    and key in doc
                    and isinstance(result[key], dict)
                    and isinstance(doc[key], dict)
                ):
                    result[key].update(doc[key])
                    del doc[key]
            result.update(doc)
        else:
            logger.warning(
                "%s: skipping non-dict YAML document (got %s)",
                source, type(doc).__name__,
            )
    if not result:
        non_dict_types = [type(d).__name__ for d in docs]
        raise ValueError(
            f"{source} produced non-dict YAML (got {', '.join(non_dict_types)})"
        )
    return result


def _repair_yaml(text: str) -> str:
    """
    Attempt to repair common YAML errors from LLM outputs.

    Common issues:
    - Tabs instead of spaces
    - Inconsistent indentation
    - Missing colons
    - Unquoted special characters
    """
    # Replace tabs with spaces
    text = text.replace('\t', '  ')

    lines = []
    for line in text.split('\n'):
        equals_match = re.match(
            r'^(\s*)([a-z_][a-z0-9_-]*)(\s*)=(\s*)(.+?)\s*$',
            line,
            re.IGNORECASE,
        )
        if equals_match:
            indent, key, _ws_before, _ws_after, value = equals_match.groups()
            # Only convert key=value to key: value for YAML mapping entries.
            # Skip lines that look like env var assignments (values containing
            # '=' or ALL_CAPS keys typical of env vars) to avoid corrupting
            # "MY_VAR=admin123" into a nested YAML mapping.
            _is_env_var_key = re.match(r'^[A-Z][A-Z0-9_]*$', key)
            if '=' not in value and not _is_env_var_key:
                line = f"{indent}{key}: {value}"

        # Quote values with YAML-significant characters when they are bare scalars.
        if ': ' in line and not re.match(r'^\s*-\s', line):
            match = re.match(r'^(\s*[a-z_][a-z0-9_]*:\s*)(.+)$', line, re.IGNORECASE)
            if match:
                key_part, value_part = match.groups()
                # Skip values that are already quoted (idempotency)
                if value_part and not value_part.startswith(('"', "'", '[', '{')):
                    # Skip URI-like values — they contain colons but are valid YAML scalars
                    _URI_PREFIXES = ("http://", "https://", "postgres://", "postgresql://",
                                     "redis://", "amqp://", "mysql://", "mongodb://",
                                     "mongodb+srv://", "ftp://", "ssh://", "tcp://")
                    if ':' in value_part or '#' in value_part:
                        if not value_part.lower().startswith(_URI_PREFIXES):
                            # YAML double-quoted strings interpret escape
                            # sequences (\n, \t, \\, etc.), so we must escape
                            # backslashes first, then double quotes.
                            escaped = value_part.replace('\\', '\\\\').replace('"', '\\"')
                            line = key_part + f'"{escaped}"'
        lines.append(line)

    return '\n'.join(lines)
