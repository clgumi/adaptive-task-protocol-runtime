"""Stable runtime error types and HTTP-safe error envelopes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class RuntimeProtocolError(Exception):
    code: str
    message: str
    status_code: int = 400
    details: dict[str, Any] | None = None

    def __str__(self) -> str:
        return self.message

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": {"code": self.code, "message": self.message}}
        if self.details:
            payload["error"]["details"] = self.details
        return payload


class NotFoundError(RuntimeProtocolError):
    def __init__(self, resource: str, resource_id: str):
        super().__init__(
            "NOT_FOUND",
            f"{resource} {resource_id!r} was not found.",
            404,
            {"resource": resource, "id": resource_id},
        )


class ConflictError(RuntimeProtocolError):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(code, message, 409, details)


class StorageError(RuntimeProtocolError):
    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__("STORAGE_ERROR", message, 500, details)

