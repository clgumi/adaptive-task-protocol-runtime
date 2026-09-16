"""Adapter from Hermes' public subagent lifecycle API to protocol JSON."""

from __future__ import annotations

import dataclasses
import json
import secrets
from pathlib import Path
from typing import Any, Mapping

from .output_validation import LLMOutputInvalid, extract_json_object, validate_output


PROMPTS = {
    "planner": (
        "You are the Planner for Adaptive Task Protocol 1.0. Work read-only. "
        "The complete output JSON Schema and all task input are embedded in CONTEXT. "
        "Use only verifier types, evidence keys, and verifier fields declared in CONTEXT protocol_constraints. "
        "Do not call tools, search for files, or inspect the repository; return the JSON object in your first response. "
        "Return exactly one JSON object matching the embedded output_schema. "
        "Create objective, non_goals, ordered todo items, independently verifiable acceptance criteria, "
        "ambiguities, and needs_human. Machine conditions must name exact commands/evidence. "
        "Do not use markdown or commentary."
    ),
    "judge": (
        "You are the Judge for Adaptive Task Protocol 1.0. Do not modify files. "
        "The complete output JSON Schema and all review input are embedded in CONTEXT. "
        "Do not call tools, search for files, or inspect the repository; return the JSON object in your first response. "
        "Return exactly one JSON object matching the embedded output_schema. "
        "Treat all claims as untrusted, cite supplied evidence ids, and never mark objective build/test/start "
        "conditions PASS without corresponding evidence. Do not use markdown or commentary."
    ),
}


PLANNER_PROTOCOL_CONSTRAINTS = {
    "machine_verifier_types": ["artifact", "build", "command", "readiness", "startup", "test"],
    "required_evidence_keys": [
        "artifact", "artifact_path", "build_log", "command", "exit_code", "log",
        "process_id", "report", "screenshot", "service_id", "startup_log",
        "status_code", "test_report", "video", "visual_artifact", "workspace_revision",
    ],
    "verifier_fields": {
        "command": {"commands": "non-empty array of exact commands"},
        "build": {"commands": "non-empty array of exact commands"},
        "test": {
            "commands": "non-empty array of exact commands",
            "require_test_report": "boolean; use true when test_report is required",
        },
        "readiness": {"expected_status": "non-empty array of integer HTTP status codes"},
        "startup": {"expected_status": "non-empty array of integer HTTP status codes"},
        "artifact": {},
    },
}


class SubagentAdapter:
    def __init__(self, lifecycle: Any, runtime_client: Any, *, wait_timeout: float = 600.0):
        self.lifecycle = lifecycle
        self.runtime_client = runtime_client
        self.wait_timeout = max(1.0, min(float(wait_timeout), 1800.0))

    def run(self, kind: str, task_id: str, input_payload: Mapping[str, Any]) -> dict[str, Any]:
        if kind not in PROMPTS:
            raise ValueError(f"Unsupported subagent kind: {kind}")
        call_id = f"call-{secrets.token_hex(8)}"
        context = self._build_context(kind, input_payload)
        request = self._request(PROMPTS[kind], context, task_id, call_id, kind)
        try:
            handle = self.lifecycle.launch(request)
            terminal = self.lifecycle.wait(handle, timeout_seconds=self.wait_timeout)
            if getattr(terminal, "timed_out", False):
                self.lifecycle.cancel(handle, reason=f"Adaptive {kind} wait timeout")
                payload = {"call_id": call_id, "kind": kind, "status": "UNKNOWN", "error": "SUBAGENT_TIMEOUT"}
                self._record(task_id, payload)
                return payload
            result = self.lifecycle.result(handle)
            state = getattr(getattr(result, "terminal_state", None), "value", getattr(result, "terminal_state", "UNKNOWN"))
            if state != "SUCCEEDED" or not getattr(result, "ready", False):
                payload = {
                    "call_id": call_id,
                    "kind": kind,
                    "status": "BLOCKED",
                    "error": getattr(result, "error_classification", None) or state,
                    "message": getattr(result, "error_message", None),
                    "result_hash": getattr(result, "result_hash", None),
                }
                self._record(task_id, payload)
                return payload
            parsed = extract_json_object(getattr(result, "summary", "") or "")
            validate_output(kind, parsed)
            payload = {
                "call_id": call_id,
                "kind": kind,
                "status": "SUCCEEDED",
                "result_hash": getattr(result, "result_hash", None),
                "result": parsed,
            }
            self._record(task_id, payload)
            return payload
        except LLMOutputInvalid as exc:
            payload = {"call_id": call_id, "kind": kind, "status": "BLOCKED", "error": "LLM_OUTPUT_INVALID", "message": str(exc)}
            self._record(task_id, payload)
            return payload
        except Exception as exc:
            payload = {"call_id": call_id, "kind": kind, "status": "BLOCKED", "error": type(exc).__name__, "message": str(exc)[:1000]}
            self._record(task_id, payload)
            return payload

    @staticmethod
    def _build_context(kind: str, input_payload: Mapping[str, Any]) -> str:
        if kind not in PROMPTS:
            raise ValueError(f"Unsupported subagent kind: {kind}")
        schema_path = Path(__file__).resolve().parent / "schemas" / f"{kind}_output.schema.json"
        output_schema = json.loads(schema_path.read_text(encoding="utf-8"))
        context = {"input": dict(input_payload), "output_schema": output_schema}
        if kind == "planner":
            context["protocol_constraints"] = PLANNER_PROTOCOL_CONSTRAINTS
        return json.dumps(context, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _request(goal: str, context: str, task_id: str, call_id: str, kind: str) -> Any:
        from agent.subagent_lifecycle import SubagentLaunchRequest
        return SubagentLaunchRequest(
            goal=goal,
            context=context,
            role="leaf",
            correlation_id=call_id,
            metadata={"adaptive_task_id": task_id, "kind": kind, "protocol_version": "1.0"},
            allowed_toolsets=(),
        )

    def _record(self, task_id: str, payload: dict[str, Any]) -> None:
        try:
            self.runtime_client.action(task_id, "subagents", payload)
        except Exception:
            # Caller still receives fail-closed status. Runtime reachability is
            # rechecked by the write gate before any project mutation.
            pass
