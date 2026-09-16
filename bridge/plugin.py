"""Thin Hermes Bridge: routing, Runtime tools, subagent adapter, and write Gate."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Callable

from .gate_hooks import (
    ProjectState,
    SessionBinding,
    SessionRegistry,
    make_post_tool_hook,
    make_pre_llm_hook,
    make_pre_tool_hook,
)
from .classifier import Mode
from .runtime_client import RuntimeClient
from .routing import RoutingSubagent
from .subagent_adapter import SubagentAdapter
from .tool_schemas import BIND, CLOSE, EVALUATE, EVENT, EVIDENCE, SNAPSHOT, START
from .workspace_revision import compute_workspace_revision


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _error(exc: Exception) -> str:
    payload = getattr(exc, "payload", None)
    return _json(payload if isinstance(payload, dict) else {
        "error": {"code": type(exc).__name__, "message": str(exc)[:1000]}
    })


LOGGER = logging.getLogger("adaptive_task_protocol.plugin")


def _log_event(event: str, **fields: Any) -> None:
    LOGGER.info(_json({"event": event, **fields}))


def _repo_token_file(ctx: Any) -> Path:
    configured = str(ctx.get_config("token_file", "") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parents[1] / "runtime-data" / "auth-token"


def register(ctx: Any) -> None:
    base_url = str(ctx.get_config("runtime_url", os.getenv("ADAPTIVE_RUNTIME_URL", "http://127.0.0.1:8790")))
    timeout = float(ctx.get_config("request_timeout", 5.0))
    client = RuntimeClient(base_url, token_file=_repo_token_file(ctx), timeout=timeout)
    sessions = SessionRegistry()
    adapter = SubagentAdapter(ctx.subagent_lifecycle, client, wait_timeout=float(ctx.get_config("subagent_wait_timeout", 600.0)))
    router = RoutingSubagent(ctx.subagent_lifecycle, timeout_seconds=float(ctx.get_config("routing_wait_timeout", 45.0)))

    def task_start(args: dict[str, Any], **kwargs: Any) -> str:
        session_id = str(kwargs.get("session_id") or "")
        binding = sessions.get(session_id)
        try:
            if binding and binding.mutation_blocked:
                _log_event("task_start_blocked", reason="safe_routing_fallback", has_task_id=False)
                return _json({"task_id": None, "state": "BLOCKED", "writes_allowed": False,
                              "error": {"code": "SAFE_ROUTING_FALLBACK", "message": "Semantic routing is read-only."}})
            if binding and binding.task_id:
                snapshot = client.snapshot(binding.task_id)
                task = snapshot["task"]
                _log_event("task_start_idempotent", task_id=binding.task_id, state=task.get("state"), has_task_id=True)
                return _json({
                    "task_id": binding.task_id,
                    "state": task.get("state"),
                    "plan_id": task.get("plan_id"),
                    "idempotent": True,
                    "writes_allowed": task.get("state") in {"READY", "EXECUTING"},
                })
            health = client.health()
            if not health.get("ready"):
                raise RuntimeError("Runtime health check did not report ready=true.")
            workspace = str(args.get("workspace") or "").strip()
            revision = str(args.get("workspace_revision") or "").strip() or compute_workspace_revision(workspace)
            _log_event("task_start_candidate", has_task_id=False, workspace_revision=revision)
            created = client.create_task({
                "protocol_version": "1.0",
                "mode": "project",
                "objective": args.get("objective"),
                "non_goals": args.get("non_goals", []),
                "workspace": {"path": str(Path(workspace).expanduser().resolve()), "revision": revision},
                "created_by": "hermes-bridge",
            })
            task_id = created["task"]["task_id"]
            sessions.set(session_id, SessionBinding(
                Mode.PROJECT,
                task_id=task_id,
                workspace=workspace,
                project_state=ProjectState.ACTIVE,
                classification_source=binding.classification_source if binding else "explicit_tool",
                classification_confidence=binding.classification_confidence if binding else 1.0,
            ))
            planner_input = {
                "protocol_version": "1.0",
                "user_objective": args.get("objective"),
                "workspace": {"path": workspace, "revision": revision},
                "non_goals": args.get("non_goals", []),
                "project_context": args.get("project_context", {}),
            }
            planned = adapter.run("planner", task_id, planner_input)
            if planned.get("status") != "SUCCEEDED":
                _log_event("task_start_blocked", task_id=task_id, state="PLANNING", has_task_id=True)
                return _json({"task_id": task_id, "state": "PLANNING", "planner": planned, "writes_allowed": False})
            snapshot = client.action(task_id, "plan", planned["result"])
            state = snapshot["task"]["state"]
            _log_event("task_start", task_id=task_id, state=state, has_task_id=True)
            return _json({"task_id": task_id, "state": state, "plan_id": snapshot["task"].get("plan_id"),
                          "planner": planned, "writes_allowed": state == "READY"})
        except Exception as exc:
            _log_event("task_start_blocked", reason=type(exc).__name__, has_task_id=bool(binding and binding.task_id))
            if session_id:
                if binding:
                    sessions.update(session_id, revision_sync_error=f"Project initialization failed: {exc}")
                else:
                    sessions.set(session_id, SessionBinding(Mode.PROJECT, revision_sync_error=f"Project initialization failed: {exc}"))
            return _error(exc)

    def task_bind(args: dict[str, Any], **kwargs: Any) -> str:
        try:
            task_id = str(args.get("task_id") or "")
            snapshot = client.snapshot(task_id)
            task = snapshot["task"]
            sessions.set(str(kwargs.get("session_id") or ""), SessionBinding(
                Mode.PROJECT, task_id=task_id, workspace=task["workspace"]["path"]
            ))
            return _json(snapshot)
        except Exception as exc:
            return _error(exc)

    def task_event(args: dict[str, Any], **kwargs: Any) -> str:
        try:
            task_id = str(args.get("task_id") or "")
            event_type = str(args.get("event_type") or "")
            payload = {key: value for key, value in args.items() if key != "task_id" and value is not None}
            session_id = str(kwargs.get("session_id") or "")
            if event_type == "todo_started":
                todo_id = str(args.get("todo_id") or "").strip()
                if not todo_id:
                    raise ValueError("todo_started requires todo_id")
                before = client.snapshot(task_id)
                if before["task"]["state"] in {"READY", "REWORK_REQUIRED"}:
                    payload["state"] = "EXECUTING"
                result = client.action(task_id, "events", payload)
                sessions.update(session_id, task_id=task_id, mode=Mode.PROJECT, project_state=ProjectState.ACTIVE,
                                active_todo_id=todo_id,
                                workspace=result["task"]["workspace"]["path"], revision_sync_error=None)
                if sessions.get(session_id) is None:
                    sessions.set(session_id, SessionBinding(Mode.PROJECT, task_id, todo_id, result["task"]["workspace"]["path"]))
                return _json(result)
            result = client.action(task_id, "events", payload)
            return _json(result)
        except Exception as exc:
            return _error(exc)

    def task_evidence(args: dict[str, Any], **_: Any) -> str:
        try:
            task_id = str(args.get("task_id") or "")
            payload = {**args, "protocol_version": "1.0", "producer": "hermes-primary"}
            payload.pop("task_id", None)
            return _json(client.action(task_id, "evidence", payload))
        except Exception as exc:
            return _error(exc)

    def task_evaluate(args: dict[str, Any], **_: Any) -> str:
        try:
            task_id = str(args.get("task_id") or "")
            judge_result = args.get("judge_result")
            if judge_result is None and args.get("judge_input") is not None:
                judged = adapter.run("judge", task_id, args["judge_input"])
                if judged.get("status") != "SUCCEEDED":
                    return _json({"task_id": task_id, "state": "BLOCKED", "judge": judged, "passed": False})
                judge_result = judged["result"]
            return _json(client.action(task_id, "evaluate", {"judge_result": judge_result} if judge_result else {}))
        except Exception as exc:
            return _error(exc)

    def task_close(args: dict[str, Any], **kwargs: Any) -> str:
        try:
            task_id = str(args.get("task_id") or "")
            payload = {"judge_result": args["judge_result"]} if args.get("judge_result") else {}
            result = client.action(task_id, "close", payload)
            session_id = str(kwargs.get("session_id") or "")
            state = result.get("task", {}).get("state") if isinstance(result, dict) else None
            if result.get("closed") or state in {"CLOSED", "CANCELLED"}:
                sessions.clear(session_id)
                _log_event("task_binding_cleared", task_id=task_id, state=state)
            return _json(result)
        except Exception as exc:
            return _error(exc)

    def task_snapshot(args: dict[str, Any], **_: Any) -> str:
        try:
            return _json(client.snapshot(str(args.get("task_id") or "")))
        except Exception as exc:
            return _error(exc)

    tools: tuple[tuple[str, dict[str, Any], Callable[..., str]], ...] = (
        ("adaptive_task_start", START, task_start),
        ("adaptive_task_bind", BIND, task_bind),
        ("adaptive_task_event", EVENT, task_event),
        ("adaptive_task_submit_evidence", EVIDENCE, task_evidence),
        ("adaptive_task_evaluate", EVALUATE, task_evaluate),
        ("adaptive_task_close", CLOSE, task_close),
        ("adaptive_task_snapshot", SNAPSHOT, task_snapshot),
    )
    for name, schema, handler in tools:
        ctx.register_tool(name=name, toolset="adaptive_task_protocol", schema=schema, handler=handler, emoji="🧭")
    ctx.register_hook("pre_llm_call", make_pre_llm_hook(sessions, client, router))
    ctx.register_hook("pre_tool_call", make_pre_tool_hook(sessions, client))
    ctx.register_hook("post_tool_call", make_post_tool_hook(sessions, client))
