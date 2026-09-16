"""Small validator for the checked-in protocol schemas.

The Runtime intentionally has no third-party runtime dependencies. This module
implements the JSON Schema subset used by this project: type, required,
properties, items, enum, const, pattern, minLength, minimum, and additionalProperties.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .errors import RuntimeProtocolError


class SchemaRegistry:
    def __init__(self, schema_dir: str | Path):
        self.schema_dir = Path(schema_dir)
        self._cache: dict[str, dict[str, Any]] = {}

    def load(self, name: str) -> dict[str, Any]:
        if name not in self._cache:
            path = (self.schema_dir / f"{name}.schema.json").resolve()
            if path.parent != self.schema_dir.resolve():
                raise RuntimeProtocolError("SCHEMA_NOT_FOUND", "Invalid schema name.", 500)
            try:
                self._cache[name] = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeProtocolError("SCHEMA_NOT_FOUND", f"Schema {name!r} is unavailable.", 500) from exc
        return self._cache[name]

    def validate(self, name: str, value: Any) -> None:
        errors: list[str] = []
        _validate_node(self.load(name), value, "$", errors)
        if errors:
            raise RuntimeProtocolError(
                "SCHEMA_VALIDATION_FAILED",
                f"Payload does not match {name} schema.",
                details={"errors": errors[:20]},
            )


def _is_type(expected: str, value: Any) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }.get(expected, True)


def _validate_node(schema: dict[str, Any], value: Any, path: str, errors: list[str]) -> None:
    expected = schema.get("type")
    allowed_types = expected if isinstance(expected, list) else [expected] if expected else []
    if allowed_types and not any(_is_type(item, value) for item in allowed_types):
        errors.append(f"{path}: expected {' or '.join(allowed_types)}")
        return
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: must equal {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: must be one of {schema['enum']!r}")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            errors.append(f"{path}: string is too short")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: string is too long")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            errors.append(f"{path}: does not match required pattern")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: must be <= {schema['maximum']}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{path}: not enough items")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate_node(item_schema, item, f"{path}[{index}]", errors)
    if isinstance(value, dict):
        for required in schema.get("required", []):
            if required not in value:
                errors.append(f"{path}.{required}: required property is missing")
        properties = schema.get("properties", {})
        for key, child in value.items():
            if key in properties:
                _validate_node(properties[key], child, f"{path}.{key}", errors)
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}.{key}: additional property is not allowed")

