"""Parse JSON-only Planner/Judge summaries and validate local schemas."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class LLMOutputInvalid(ValueError):
    pass


def extract_json_object(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    decoder = json.JSONDecoder()
    for index, char in enumerate(raw):
        if char != "{":
            continue
        try:
            value, end = decoder.raw_decode(raw[index:])
        except json.JSONDecodeError:
            continue
        trailing = raw[index + end:].strip()
        if isinstance(value, dict) and (not trailing or trailing == "```"):
            return value
    raise LLMOutputInvalid("LLM_OUTPUT_INVALID: summary does not contain one complete JSON object.")


def validate_output(kind: str, payload: dict[str, Any], schema_dir: str | Path | None = None) -> None:
    if kind not in {"planner", "judge"}:
        raise LLMOutputInvalid(f"Unsupported LLM output kind: {kind}")
    try:
        from .schema_validation import validate_schema
        directory = Path(schema_dir) if schema_dir else Path(__file__).resolve().parent / "schemas"
        validate_schema(directory / f"{kind}_output.schema.json", payload)
    except Exception as exc:
        if isinstance(exc, LLMOutputInvalid):
            raise
        raise LLMOutputInvalid(f"LLM_OUTPUT_INVALID: {exc}") from exc
