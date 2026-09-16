"""JSON Schema subset validator bundled with the standalone Bridge."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def validate_schema(schema_path: str | Path, value: Any) -> None:
    schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    errors: list[str] = []
    _node(schema, value, "$", errors)
    if errors:
        raise ValueError("; ".join(errors[:20]))


def _matches(expected: str, value: Any) -> bool:
    return {
        "object": isinstance(value, dict), "array": isinstance(value, list), "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool), "null": value is None,
    }.get(expected, True)


def _node(schema: dict[str, Any], value: Any, path: str, errors: list[str]) -> None:
    raw_type = schema.get("type")
    types = raw_type if isinstance(raw_type, list) else [raw_type] if raw_type else []
    if types and not any(_matches(item, value) for item in types):
        errors.append(f"{path}: invalid type")
        return
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: must equal {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: invalid enum value")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            errors.append(f"{path}: string too short")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            errors.append(f"{path}: pattern mismatch")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: above maximum")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{path}: too few items")
        if isinstance(schema.get("items"), dict):
            for index, item in enumerate(value):
                _node(schema["items"], item, f"{path}[{index}]", errors)
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}.{key}: required")
        for key, item in value.items():
            child = schema.get("properties", {}).get(key)
            if isinstance(child, dict):
                _node(child, item, f"{path}.{key}", errors)

