"""Session bindings, semantic routing, and fail-closed Project write hooks."""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from .classifier import Classification, Mode, classify
from .routing import RoutingEnvelope
from .workspace_revision import compute_workspace_revision


LOGGER = logging.getLogger("adaptive_task_protocol.bridge")

BRIDGE_TOOLS = {
    "adaptive_task_start", "adaptive_task_bind", "adaptive_task_event",
    "adaptive_task_plan",
    "adaptive_task_submit_evidence", "adaptive_task_evaluate",
    "adaptive_task_close", "adaptive_task_snapshot",
}
WRITE_TOOLS = {
    "write_file", "patch", "patch_file", "apply_patch", "edit_file",
    "delete_file", "move_file", "create_file", "computer", "computer_use",
}
TERMINAL_TOOLS = {"terminal", "terminal_exec", "execute_command", "shell", "powershell"}
SAFE_COMMAND = re.compile(
    r"^\s*(?:(?:[A-Za-z_][A-Za-z0-9_]*=[^\s]+)\s+)*(?:rg\b|grep\b|findstr\b|"
    r"get-content\b|get-childitem\b|dir\b|ls\b|pwd\b|"
    r"git\s+(?:status|diff|log|show|rev-parse)\b|"
    r"python\s+(?:--version|-m\s+(?:unittest|compileall))\b)",
    re.IGNORECASE,
)
DANGEROUS_SHELL_TAIL = re.compile(r"(?:>|>>|\|\s*(?:tee|set-content|out-file)\b|;|(?<!&)\&(?!&))", re.IGNORECASE)


class ProjectState(str, Enum):
    NONE = "NONE"
    CANDIDATE = "PROJECT_CANDIDATE"
    ACTIVE = "PROJECT_ACTIVE"


@dataclass(slots=True)
class SessionBinding:
    mode: Mode
    task_id: str | None = None
    active_todo_id: str | None = None
    workspace: str | None = None
    workspace_revision: str | None = None
    revision_sync_error: str | None = None
    project_state: ProjectState = ProjectState.NONE
    mutation_blocked: bool = False
    classification_source: str = "deterministic"
    classification_confidence: float = 0.0

    def __post_init__(self) -> None:
        if self.mode == Mode.PROJECT:
            if self.task_id:
                self.project_state = ProjectState.ACTIVE
            elif self.project_state == ProjectState.NONE:
                self.project_state = ProjectState.CANDIDATE
        elif self.project_state != ProjectState.NONE:
            self.project_state = ProjectState.NONE


class SessionRegistry:
    def __init__(self):
        self._lock = threading.RLock()
        self._bindings: dict[str, SessionBinding] = {}

    def get(self, session_id: str) -> SessionBinding | None:
        with self._lock:
            return self._bindings.get(session_id)

    def set(self, session_id: str, binding: SessionBinding) -> None:
        if not session_id:
            return
        with self._lock:
            self._bindings[session_id] = binding

    def update(self, session_id: str, **changes: Any) -> SessionBinding | None:
        with self._lock:
            binding = self._bindings.get(session_id)
            if binding:
                for key, value in changes.items():
                    setattr(binding, key, value)
            return binding

    def clear(self, session_id: str) -> None:
        if session_id:
            with self._lock:
                self._bindings.pop(session_id, None)


def _is_safe_terminal_command(command: str) -> bool:
    command = str(command or "").strip()
    if not command or DANGEROUS_SHELL_TAIL.search(command):
        return False
    # Permit a chain of independently allow-listed inspection/check commands,
    # but never authorize a safe prefix followed by an arbitrary command.
    parts = re.split(r"\s*&&\s*", command)
    return all(SAFE_COMMAND.match(part) for part in parts if part.strip())


def is_high_risk(tool_name: str, args: Any) -> bool:
    name = str(tool_name or "").lower()
    if name in BRIDGE_TOOLS:
        return False
    if name in WRITE_TOOLS:
        return True
    if name in TERMINAL_TOOLS:
        command = ""
        if isinstance(args, dict):
            command = str(args.get("command") or args.get("cmd") or "")
        return not _is_safe_terminal_command(command)
    return any(token in name for token in ("write", "delete", "patch", "edit", "execute"))


def _emit(logger: Any, event: str, **fields: Any) -> None:
    payload = {"event": event, **fields}
    try:
        logger.info(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    except Exception:
        LOGGER.info(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _safe_fallback(reason: str) -> Classification:
    return Classification(
        mode=Mode.OPERATION,
        reason=f"safe fallback: {reason}",
        confidence=0.0,
        source="fallback",
        reason_codes=(reason,),
        task_intent="inspect",
        mutation_blocked=True,
    )


def _is_explicit_task_continuation(text: str) -> bool:
    normalized = " ".join(str(text or "").strip().split())
    return bool(re.fullmatch(
        r"(?:continue|resume|继续|继续做|接着做|继续这个任务|继续当前任务|继续之前的任务|恢复任务)[.!。！?？]*",
        normalized,
        re.IGNORECASE,
    ))


def _is_affirmative_task_continuation_request(text: str) -> bool:
    normalized = " ".join(str(text or "").strip().split())
    if _is_explicit_task_continuation(normalized):
        return True

    verb = r"(?:continue|resume|pick\s+up|carry\s+on)"
    target = r"(?:(?:the|this|that|active|current|previous|existing|bound|same)\s+)*(?:task|project(?:\s+task)?|work(?:\s+item)?)"
    affirmative_tails = (
        rf"(?:\s+(?:with|on|working\s+on)\s+{target}|\s+{target}|\s+where we left off)?"
    )
    english_forms = (
        rf"(?:please\s+)?{verb}{affirmative_tails}",
        rf"(?:let's|lets)\s+{verb}{affirmative_tails}",
        rf"(?:can|could|would)\s+you\s+(?:please\s+)?{verb}{affirmative_tails}",
        rf"i\s+(?:want|would like)\s+to\s+{verb}{affirmative_tails}",
    )
    punctuation = r"[.!?。！？]*"
    if any(re.fullmatch(pattern + punctuation, normalized, re.IGNORECASE) for pattern in english_forms):
        return True

    chinese = r"(?:请|麻烦)?(?:继续|接着|恢复)(?:一下)?(?:做|执行)?(?:这个|当前|之前|刚才|上次|上一个|上一轮)?(?:的)?(?:任务|项目|工作)(?:吧|一下)?[。！？!?.]*"
    return bool(re.fullmatch(chinese, normalized))


def make_pre_llm_hook(
    registry: SessionRegistry,
    client: Any,
    router: Any = None,
    *,
    logger: Any = LOGGER,
    profile: str = "",
    surface: str = "",
):
    def pre_llm_call(*, session_id: str = "", user_message: Any = "", conversation_history: Any = None, **_: Any):
        binding = registry.get(session_id)
        current = str(user_message or "")
        recent = _recent_text(conversation_history)
        # Classify every inbound turn from its current message before applying
        # the explicit continuation rule. Durable session bindings are context,
        # never a substitute for per-turn classification.
        decision = classify(current)
        if binding and binding.task_id and _is_affirmative_task_continuation_request(current):
            decision = Classification(
                mode=Mode.PROJECT,
                reason="explicit continuation of the resumable Project Task",
                confidence=0.99,
                source="deterministic",
                reason_codes=("explicit_task_continuation",),
                task_intent="continue",
            )
        if decision.needs_subagent:
            if router is not None:
                active_task = None
                if binding and binding.task_id:
                    active_task = {
                        "task_id": binding.task_id,
                        "active_todo_id": binding.active_todo_id,
                    }
                try:
                    decision = router.classify(RoutingEnvelope(
                        user_message=current[:6_000],
                        recent_context=recent[-6_000:],
                        active_task=active_task,
                    ))
                except Exception as exc:
                    decision = _safe_fallback(type(exc).__name__)
            else:
                decision = _safe_fallback("subagent_unavailable")

        # Router context may explain references, but it cannot manufacture a
        # continuation intent that is absent from the current user turn.
        if (
            decision.mode == Mode.PROJECT
            and decision.task_intent == "continue"
            and not _is_affirmative_task_continuation_request(current)
        ):
            decision = _safe_fallback("continuation_not_explicit")

        _emit(
            logger,
            "classification",
            mode=decision.mode.value,
            confidence=round(decision.confidence, 4),
            source=decision.source,
            reason_codes=list(decision.reason_codes),
            task_intent=decision.task_intent,
            project_state=(ProjectState.CANDIDATE.value if decision.mode == Mode.PROJECT else ProjectState.NONE.value),
            has_task_id=False,
            profile=profile or None,
            surface=surface or None,
        )
        if decision.mode == Mode.PROJECT and decision.task_intent == "continue" and binding and binding.task_id:
            try:
                snapshot = client.snapshot(binding.task_id)
                task = snapshot["task"]
                if task.get("state") not in {"CLOSED", "CANCELLED"}:
                    workspace = task["workspace"]
                    active_todo_id = next((
                        item["id"] for item in task.get("todo", [])
                        if str(item.get("status", "")).upper() == "IN_PROGRESS"
                    ), None)
                    registry.update(
                        session_id,
                        mode=Mode.PROJECT,
                        task_id=binding.task_id,
                        active_todo_id=active_todo_id,
                        workspace=workspace.get("path"),
                        workspace_revision=workspace.get("revision"),
                        project_state=ProjectState.ACTIVE,
                        mutation_blocked=False,
                        classification_source=decision.source,
                        classification_confidence=decision.confidence,
                    )
                    return {"context": (
                        f"ADAPTIVE PROJECT TASK: task_id={binding.task_id}; state={task['state']}; "
                        f"plan_id={task.get('plan_id')}; active_todo={active_todo_id or 'none'}. "
                        "This turn explicitly continues the bound Task. Record the active TODO before project writes, "
                        "submit revision-bound evidence, and call adaptive_task_close only after review."
                    )}
            except Exception as exc:
                registry.update(
                    session_id,
                    mode=Mode.PROJECT,
                    project_state=ProjectState.ACTIVE,
                    mutation_blocked=True,
                    revision_sync_error=f"Runtime unavailable: {exc}",
                    classification_source=decision.source,
                    classification_confidence=decision.confidence,
                )
                return {"context": "ADAPTIVE PROJECT MODE: Runtime is unavailable; mutating tools are blocked (fail-closed)."}
        if decision.mode == Mode.PROJECT:
            changes = {
                "mode": Mode.PROJECT,
                "project_state": ProjectState.CANDIDATE,
                "mutation_blocked": False,
                "classification_source": decision.source,
                "classification_confidence": decision.confidence,
            }
            if binding:
                # Keep a resumable durable Task id, but do not attach it to this
                # new Project turn until scope matching or explicit continuation.
                registry.update(session_id, **changes)
            else:
                registry.set(session_id, SessionBinding(**changes))
            existing = f" Existing task_id={binding.task_id} is resumable but is not attached to this turn." if binding and binding.task_id else " No Runtime task_id exists yet."
            return {"context": (
                "ADAPTIVE PROJECT MODE REQUIRED: PROJECT CANDIDATE: this turn is classified as Project but has no Task attached. "
                "Call adaptive_task_start; reuse only if objective, workspace, and non-goals match, otherwise use new_task=true."
                f"{existing} Candidate status alone never authorizes writes."
            )}
        if decision.mutation_blocked:
            changes = {
                "mode": Mode.OPERATION,
                "project_state": ProjectState.NONE,
                "mutation_blocked": True,
                "classification_source": decision.source,
                "classification_confidence": decision.confidence,
            }
            if binding:
                registry.update(session_id, **changes)
            else:
                registry.set(session_id, SessionBinding(**changes))
            return {"context": "ADAPTIVE SAFE OPERATION: only read-only analysis is allowed until routing succeeds."}
        if binding:
            registry.update(
                session_id,
                mode=decision.mode,
                project_state=ProjectState.NONE,
                mutation_blocked=False,
                classification_source=decision.source,
                classification_confidence=decision.confidence,
            )
        return None
    return pre_llm_call


def _recent_text(history: Any) -> str:
    if not isinstance(history, list):
        return ""
    parts: list[str] = []
    for item in history[-6:]:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if isinstance(content, str):
            parts.append(content[:4_000])
        elif isinstance(content, list):
            parts.extend(
                str(part.get("text", ""))[:4_000]
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
    return "\n".join(parts)


def make_pre_tool_hook(registry: SessionRegistry, client: Any):
    def pre_tool_call(*, tool_name: str = "", args: Any = None, session_id: str = "", **_: Any):
        try:
            binding = registry.get(session_id)
            if not binding or not is_high_risk(tool_name, args):
                return None
            if binding.mutation_blocked:
                return {"action": "block", "message": "BLOCKED: semantic routing is in safe read-only fallback mode."}
            if binding.mode != Mode.PROJECT:
                return None
            if binding.project_state == ProjectState.CANDIDATE or not binding.task_id:
                return {"action": "block", "message": "BLOCKED: Project Candidate has no Runtime task_id. Call adaptive_task_start first."}
            if binding.revision_sync_error:
                try:
                    # Revision-sync failures are retryable, not a permanent session lock.
                    # Reconcile against the Runtime's authoritative task snapshot before
                    # deciding whether the next scoped write must remain blocked.
                    snapshot = client.snapshot(binding.task_id)
                    task = snapshot["task"]
                    workspace = task["workspace"]["path"]
                    revision = compute_workspace_revision(workspace)
                    if revision != task["workspace"].get("revision"):
                        client.action(binding.task_id, "events", {
                            "event_type": "workspace_revision_changed",
                            "workspace_revision": revision,
                            "details": {"reason": "pre_tool_retry_after_revision_sync_error", "active_todo_id": binding.active_todo_id},
                        })
                        snapshot = client.snapshot(binding.task_id)
                        task = snapshot["task"]
                        if task["workspace"].get("revision") != revision:
                            raise RuntimeError("Runtime did not persist the reconciled workspace revision.")
                    registry.update(
                        session_id,
                        workspace=workspace,
                        workspace_revision=revision,
                        revision_sync_error=None,
                    )
                except Exception as exc:
                    reason = f"Revision sync recovery failed: {type(exc).__name__}: {str(exc)[:240]}"
                    registry.update(session_id, revision_sync_error=reason)
                    return {"action": "block", "message": f"BLOCKED by Adaptive Runtime: {reason}"}
            else:
                snapshot = client.snapshot(binding.task_id)
            task = snapshot["task"]
            if task["state"] not in {"READY", "EXECUTING"}:
                return {"action": "block", "message": f"BLOCKED: Runtime task state is {task['state']}; READY or EXECUTING is required."}
            if not task.get("plan_id"):
                return {"action": "block", "message": "BLOCKED: Project task has no committed plan_id."}
            if not binding.active_todo_id:
                return {"action": "block", "message": "BLOCKED: Select an active TODO with adaptive_task_event before project writes."}
            known_todos = {item["id"] for item in task.get("todo", [])}
            if binding.active_todo_id not in known_todos:
                return {"action": "block", "message": "BLOCKED: Active TODO is not part of the current Task Contract."}
            path_error = _path_scope_error(tool_name, args, task["workspace"]["path"])
            if path_error:
                return {"action": "block", "message": f"BLOCKED: {path_error}"}
            return None
        except Exception as exc:
            return {"action": "block", "message": f"BLOCKED: Adaptive Runtime gate failed closed: {type(exc).__name__}: {str(exc)[:300]}"}
    return pre_tool_call


def make_post_tool_hook(registry: SessionRegistry, client: Any):
    def post_tool_call(*, tool_name: str = "", args: Any = None, session_id: str = "", status: str = "", **_: Any):
        binding = registry.get(session_id)
        if not binding or binding.mode != Mode.PROJECT or binding.project_state != ProjectState.ACTIVE or not binding.task_id or not is_high_risk(tool_name, args):
            return None
        if status.lower() in {"blocked", "error", "cancelled", "failed"}:
            return None
        try:
            snapshot = client.snapshot(binding.task_id)
            workspace = snapshot["task"]["workspace"]["path"]
            revision = compute_workspace_revision(workspace)
            if revision != snapshot["task"]["workspace"]["revision"]:
                client.action(binding.task_id, "events", {
                    "event_type": "workspace_revision_changed",
                    "workspace_revision": revision,
                    "tool_name": tool_name,
                    "active_todo_id": binding.active_todo_id,
                })
            registry.update(
                session_id,
                workspace=workspace,
                workspace_revision=revision,
                revision_sync_error=None,
            )
        except Exception as exc:
            registry.update(session_id, revision_sync_error=f"Revision sync failed after {tool_name}: {type(exc).__name__}: {str(exc)[:240]}")
        return None
    return post_tool_call


def _path_scope_error(tool_name: str, args: Any, workspace: str) -> str | None:
    if str(tool_name).lower() in TERMINAL_TOOLS or not isinstance(args, dict):
        return None
    raw = args.get("path") or args.get("file_path") or args.get("target")
    if not isinstance(raw, str) or not raw.strip():
        return None
    root = Path(workspace).expanduser().resolve()
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    if candidate != root and not candidate.is_relative_to(root):
        return f"write target {candidate} is outside task workspace {root}."
    return None
