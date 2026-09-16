"""Bounded semantic routing for ambiguous Hermes turns.

The router is deliberately separate from the deterministic classifier. It is
used only when lexical rules cannot safely decide between chat, operation, and
project. The leaf subagent receives a bounded, redacted envelope and must
return one JSON object; failures degrade to a non-mutating operation route.
"""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass
from typing import Any, Mapping

from .classifier import Classification, Mode
from .output_validation import LLMOutputInvalid, extract_json_object

LOGGER = logging.getLogger("adaptive_task_protocol.routing")

ROUTER_PROMPT = (
    "You are a routing classifier for Adaptive Task Protocol 1.0. "
    "Classify the user turn as chat, operation, or project. "
    "Project means sustained or multi-step work with an artifact, workspace, "
    "delivery, testing, deployment, or acceptance—not merely mentioning a project. "
    "Chat means explanation, discussion, diagnosis, or advice without requested "
    "external-state work. Operation means a bounded lookup, inspection, message, "
    "or one-off action. Do not call tools. Do not modify files. "
    "Return exactly one JSON object with mode, confidence, reason_codes, "
    "task_intent, and summary."
)

ROUTER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["mode", "confidence", "reason_codes", "task_intent", "summary"],
    "properties": {
        "mode": {"type": "string", "enum": ["chat", "operation", "project"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason_codes": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
        "task_intent": {"type": "string", "enum": ["none", "start", "continue", "inspect"]},
        "summary": {"type": "string", "maxLength": 500},
    },
}

VALID_MODES = frozenset({"chat", "operation", "project"})
VALID_TASK_INTENTS = frozenset({"none", "start", "continue", "inspect"})


class RoutingOutputInvalid(ValueError):
    """The leaf router did not return the fixed routing contract."""


@dataclass(frozen=True, slots=True)
class RoutingEnvelope:
    user_message: str
    recent_context: str = ""
    active_task: Mapping[str, Any] | None = None


def parse_routing_output(payload: Mapping[str, Any]) -> Classification:
    if not isinstance(payload, Mapping):
        raise RoutingOutputInvalid("routing output must be an object")
    allowed = {"mode", "confidence", "reason_codes", "task_intent", "summary"}
    if set(payload) != allowed:
        raise RoutingOutputInvalid("routing output must contain exactly the fixed JSON fields")
    mode = payload.get("mode")
    if mode not in VALID_MODES:
        raise RoutingOutputInvalid("routing mode is invalid")
    confidence = payload.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise RoutingOutputInvalid("routing confidence must be numeric")
    confidence = float(confidence)
    if not 0 <= confidence <= 1:
        raise RoutingOutputInvalid("routing confidence must be between 0 and 1")
    reason_codes = payload.get("reason_codes")
    if not isinstance(reason_codes, list) or len(reason_codes) > 8 or any(not isinstance(item, str) for item in reason_codes):
        raise RoutingOutputInvalid("reason_codes must be a string array with at most eight items")
    task_intent = payload.get("task_intent")
    if task_intent not in VALID_TASK_INTENTS:
        raise RoutingOutputInvalid("task_intent is invalid")
    summary = payload.get("summary")
    if not isinstance(summary, str) or len(summary) > 500:
        raise RoutingOutputInvalid("summary must be a short string")
    if confidence < 0.60:
        return Classification(
            Mode.OPERATION,
            "subagent confidence below safe threshold",
            confidence,
            source="fallback",
            reason_codes=("low_confidence",),
            task_intent="inspect",
            needs_subagent=False,
            mutation_blocked=True,
        )
    return Classification(
        Mode(mode),
        ";".join(reason_codes) or summary[:120] or "subagent route",
        confidence,
        source="subagent",
        reason_codes=tuple(reason_codes),
        task_intent=task_intent,
        needs_subagent=False,
        mutation_blocked=False,
    )


def _safe_active_task(active_task: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not active_task:
        return None
    # Do not pass workspace paths, evidence payloads, or arbitrary task data to
    # the routing leaf; only the state needed to resolve continuation intent.
    return {
        key: str(active_task[key])[:300]
        for key in ("task_id", "state", "plan_id", "active_todo_id")
        if active_task.get(key) is not None
    }


class RoutingSubagent:
    """Call a restricted leaf subagent without requiring a Runtime task."""

    def __init__(self, lifecycle: Any, *, timeout_seconds: float = 45.0):
        self.lifecycle = lifecycle
        self.timeout_seconds = max(1.0, min(float(timeout_seconds), 120.0))

    def classify(self, envelope: RoutingEnvelope) -> Classification:
        if self.lifecycle is None:
            return self._fallback("subagent_unavailable")
        call_id = f"route-{secrets.token_hex(8)}"
        context = json.dumps(
            {
                "input": {
                    "user_message": envelope.user_message[:6_000],
                    "recent_context": envelope.recent_context[-6_000:],
                    "active_task": _safe_active_task(envelope.active_task),
                },
                "output_schema": ROUTER_SCHEMA,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        try:
            request = self._request(context, call_id)
            handle = self.lifecycle.launch(request)
            terminal = self.lifecycle.wait(handle, timeout_seconds=self.timeout_seconds)
            if getattr(terminal, "timed_out", False):
                self.lifecycle.cancel(handle, reason="Adaptive routing classification timeout")
                return self._fallback("subagent_timeout")
            result = self.lifecycle.result(handle)
            state = getattr(getattr(result, "terminal_state", None), "value", getattr(result, "terminal_state", "UNKNOWN"))
            if state != "SUCCEEDED" or not getattr(result, "ready", False):
                return self._fallback("subagent_blocked")
            payload = extract_json_object(getattr(result, "summary", "") or "")
            return parse_routing_output(payload)
        except (RoutingOutputInvalid, LLMOutputInvalid, ValueError, TypeError) as exc:
            LOGGER.info(json.dumps({"event": "routing_subagent_fallback", "reason": "invalid_output", "error": str(exc)[:160]}, ensure_ascii=False, sort_keys=True))
            return self._fallback("invalid_output")
        except Exception as exc:
            LOGGER.info(json.dumps({"event": "routing_subagent_fallback", "reason": type(exc).__name__}, ensure_ascii=False, sort_keys=True))
            return self._fallback("subagent_error")

    @staticmethod
    def _request(context: str, call_id: str) -> Any:
        from agent.subagent_lifecycle import SubagentLaunchRequest
        return SubagentLaunchRequest(
            goal=ROUTER_PROMPT,
            context=context,
            role="leaf",
            correlation_id=call_id,
            metadata={"adaptive_route": True, "protocol_version": "1.0"},
            allowed_toolsets=(),
        )

    @staticmethod
    def _fallback(reason: str) -> Classification:
        return Classification(
            Mode.OPERATION,
            f"safe fallback: {reason}",
            0.0,
            source="fallback",
            reason_codes=(reason,),
            task_intent="inspect",
            needs_subagent=False,
            mutation_blocked=True,
        )
