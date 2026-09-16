"""HTTP-independent protocol dispatcher used by the server and contract tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit

from .errors import NotFoundError, RuntimeProtocolError
from .evidence import EvidenceService
from .gates import GateService
from .ledger import TaskLedger
from .models import PROTOCOL_VERSION, require_mapping, require_protocol
from .schema_validation import SchemaRegistry
from .storage import Storage


class RuntimeProtocol:
    def __init__(self, storage: Storage, schema_dir: str | Path | None = None):
        self.storage = storage
        self.schemas = SchemaRegistry(schema_dir or Path(__file__).resolve().parent / "schemas")
        self.ledger = TaskLedger(storage)
        self.evidence = EvidenceService(storage)
        self.gates = GateService(storage)

    def dispatch(self, method: str, raw_path: str, body: Any = None) -> tuple[int, dict[str, Any]]:
        method = method.upper()
        path = unquote(urlsplit(raw_path).path).rstrip("/") or "/"
        if method == "GET" and path == "/health":
            return 200, {"ready": True, "protocol_version": PROTOCOL_VERSION}
        if method == "POST" and path == "/v1/tasks":
            payload = require_mapping(body)
            require_protocol(payload.get("protocol_version"))
            return 201, self.ledger.create_task(payload)
        parts = path.split("/")
        if len(parts) not in {4, 5} or parts[:3] != ["", "v1", "tasks"] or not parts[3]:
            raise NotFoundError("route", f"{method} {path}")
        task_id = parts[3]
        action = parts[4] if len(parts) == 5 else ""
        if method == "GET" and not action:
            return 200, self.storage.snapshot(task_id)
        payload = require_mapping(body)
        if method == "POST" and action == "plan":
            self.schemas.validate("planner_output", payload)
            return 200, self.ledger.submit_plan(task_id, payload)
        if method == "POST" and action == "events":
            return 200, self.ledger.append_event(task_id, payload)
        if method == "POST" and action == "evidence":
            normalized = {**dict(payload), "task_id": task_id}
            self.schemas.validate("evidence", normalized)
            return 201, self.evidence.submit(task_id, normalized)
        if method == "POST" and action == "evaluate":
            judge = payload.get("judge_result")
            if judge is not None:
                self.schemas.validate("judge_output", judge)
            return 200, self.gates.evaluate(task_id, judge)
        if method == "POST" and action == "rework":
            return 200, self.ledger.request_rework(task_id, payload)
        if method == "POST" and action == "close":
            judge = payload.get("judge_result")
            if judge is not None:
                self.schemas.validate("judge_output", judge)
            return 200, self.gates.close(task_id, judge)
        if method == "POST" and action == "subagents":
            call_id = str(payload.get("call_id", "")).strip()
            kind = str(payload.get("kind", "")).strip()
            status = str(payload.get("status", "")).strip()
            if not call_id or kind not in {"planner", "judge", "classifier"} or not status:
                raise RuntimeProtocolError(
                    "SCHEMA_VALIDATION_FAILED",
                    "subagent call requires call_id, supported kind, and status.",
                )
            self.storage.record_subagent_call(
                task_id, call_id, kind, status, dict(payload), payload.get("result_hash")
            )
            return 201, {"recorded": True, "call_id": call_id}
        raise NotFoundError("route", f"{method} {path}")
