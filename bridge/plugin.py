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
from .tool_schemas import BIND, CLOSE, EVALUATE, EVENT, EVIDENCE, PLAN, SNAPSHOT, START
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


def _normalized_scope_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _same_task_scope(task: dict[str, Any], args: dict[str, Any]) -> bool:
    """Idempotency is valid only for the same objective, workspace, and non-goals."""
    try:
        task_workspace = os.path.normcase(os.path.normpath(str(Path(task["workspace"]["path"]).expanduser().resolve())))
        requested_workspace = os.path.normcase(os.path.normpath(str(Path(args["workspace"]).expanduser().resolve())))
    except (KeyError, TypeError, ValueError, OSError):
        return False
    task_non_goals = sorted(_normalized_scope_text(item).casefold() for item in task.get("non_goals", []))
    requested_non_goals = sorted(_normalized_scope_text(item).casefold() for item in args.get("non_goals", []))
    return (
        _normalized_scope_text(task.get("objective")) == _normalized_scope_text(args.get("objective"))
        and task_workspace == requested_workspace
        and task_non_goals == requested_non_goals
    )


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
            if binding and binding.task_id and not bool(args.get("new_task")):
                snapshot = client.snapshot(binding.task_id)
                task = snapshot["task"]
                state = task.get("state")
                if not _same_task_scope(task, args):
                    _log_event("task_start_scope_mismatch", task_id=binding.task_id, state=state)
                    return _json({
                        "task_id": binding.task_id,
                        "state": state,
                        "idempotent": False,
                        "writes_allowed": False,
                        "error": {
                            "code": "TASK_SCOPE_MISMATCH",
                            "message": "The bound task has a different objective, workspace, or non-goals. Re-call adaptive_task_start with new_task=true to create a separate scoped task; the existing task will remain unchanged.",
                        },
                    })
                if state in {"CLOSED", "CANCELLED"}:
                    return _json({
                        "task_id": binding.task_id,
                        "state": state,
                        "idempotent": False,
                        "writes_allowed": False,
                        "error": {
                            "code": "TASK_NOT_REUSABLE",
                            "message": "A closed or cancelled task cannot be reused. Re-call adaptive_task_start with new_task=true.",
                        },
                    })
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
                workspace_revision=revision,
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
                          "planner": planned, "writes_allowed": state == "READY",
                          "idempotent": False, "replaced_binding_task_id": binding.task_id if binding and binding.task_id else None})
        except Exception as exc:
            _log_event("task_start_blocked", reason=type(exc).__name__, has_task_id=bool(binding and binding.task_id))
            if session_id:
                current_binding = sessions.get(session_id)
                preserve_old_binding = bool(
                    args.get("new_task")
                    and binding
                    and binding.task_id
                    and current_binding
                    and current_binding.task_id == binding.task_id
                )
                if not preserve_old_binding:
                    if current_binding:
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
                Mode.PROJECT,
                task_id=task_id,
                workspace=task["workspace"]["path"],
                workspace_revision=task["workspace"].get("revision"),
            ))
            return _json(snapshot)
        except Exception as exc:
            return _error(exc)

    def task_plan(args: dict[str, Any], **kwargs: Any) -> str:
        try:
            task_id = str(args.get("task_id") or "").strip()
            if not task_id:
                raise ValueError("adaptive_task_plan requires task_id")
            session_id = str(kwargs.get("session_id") or "")
            binding = sessions.get(session_id)
            if not binding or binding.mode != Mode.PROJECT or binding.task_id != task_id:
                return _json({"error": {
                    "code": "PLAN_NOT_BOUND",
                    "message": "adaptive_task_plan requires a session binding for this task_id.",
                    "details": {"task_id": task_id},
                }})
            before = client.snapshot(task_id)
            task = before["task"]
            state = task.get("state")
            if state not in {"PLANNING", "BLOCKED", "REWORK_REQUIRED", "VERIFIER_DEFECT"}:
                return _json({"error": {
                    "code": "PLAN_NOT_ALLOWED",
                    "message": f"Cannot submit a plan while task is {state}.",
                    "details": {"task_id": task_id, "state": state},
                }})
            if task.get("plan_id") and not str(args.get("repair_reason") or "").strip():
                return _json({"error": {
                    "code": "PLAN_REPAIR_REASON_REQUIRED",
                    "message": "Replacing a committed plan requires repair_reason.",
                    "details": {"task_id": task_id, "old_plan_id": task.get("plan_id")},
                }})
            payload = {key: value for key, value in args.items() if key != "task_id"}
            payload.setdefault("protocol_version", "1.0")
            client.action(task_id, "plan", payload)
            # The action response is not the binding source of truth. Read the
            # Runtime again so a proxy/cache or a future action response shape
            # cannot leave this session on a stale workspace revision.
            snapshot = client.snapshot(task_id)
            refreshed_task = snapshot["task"]
            workspace = refreshed_task.get("workspace") or {}
            sessions.update(
                session_id,
                mode=Mode.PROJECT,
                task_id=task_id,
                project_state=ProjectState.ACTIVE,
                workspace=workspace.get("path"),
                workspace_revision=workspace.get("revision"),
                active_todo_id=None,
                revision_sync_error=None,
            )
            _log_event("task_plan", task_id=task_id, state=refreshed_task.get("state"), has_task_id=True)
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
                                workspace=result["task"]["workspace"]["path"],
                                workspace_revision=result["task"]["workspace"].get("revision"),
                                revision_sync_error=None)
                if sessions.get(session_id) is None:
                    sessions.set(session_id, SessionBinding(
                        Mode.PROJECT,
                        task_id,
                        todo_id,
                        result["task"]["workspace"]["path"],
                        result["task"]["workspace"].get("revision"),
                    ))
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
        ("adaptive_task_plan", PLAN, task_plan),
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
