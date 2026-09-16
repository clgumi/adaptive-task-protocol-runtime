"""Task lifecycle operations built on the durable store."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any, Mapping

from .errors import ConflictError, RuntimeProtocolError
from .models import (
    AcceptanceCriterion,
    TaskContract,
    TaskMode,
    TaskState,
    TodoItem,
    validate_transition,
)
from .storage import Storage


def _new_id(prefix: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{prefix}-{stamp}-{secrets.token_hex(4)}"


class TaskLedger:
    def __init__(self, storage: Storage):
        self.storage = storage

    def create_task(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        mode = payload.get("mode", TaskMode.PROJECT.value)
        if mode != TaskMode.PROJECT.value:
            raise RuntimeProtocolError("MODE_NOT_SUPPORTED", "Runtime creates only project tasks.")
        workspace = payload.get("workspace")
        if not isinstance(workspace, Mapping):
            raise RuntimeProtocolError("SCHEMA_VALIDATION_FAILED", "workspace is required.")
        contract = TaskContract.from_dict({
            "protocol_version": payload.get("protocol_version"),
            "task_id": payload.get("task_id") or _new_id("task"),
            "mode": mode,
            "objective": payload.get("objective"),
            "non_goals": payload.get("non_goals", []),
            "workspace": dict(workspace),
            "todo": [],
            "acceptance_criteria": [],
            "state": TaskState.PLANNING.value,
            "created_by": payload.get("created_by", "hermes-bridge"),
        })
        self.storage.create_task(contract)
        return self.storage.snapshot(contract.task_id)

    def submit_plan(self, task_id: str, planner_output: Mapping[str, Any]) -> dict[str, Any]:
        snapshot = self.storage.snapshot(task_id)
        current = TaskState(snapshot["task"]["state"])
        if current not in {TaskState.PLANNING, TaskState.BLOCKED}:
            raise ConflictError("PLAN_NOT_ALLOWED", f"Cannot submit a plan while task is {current.value}.")
        todo = [TodoItem.from_dict(item) for item in planner_output.get("todo", [])]
        criteria = [AcceptanceCriterion.from_dict(item) for item in planner_output.get("acceptance_criteria", [])]
        if not todo or not criteria:
            raise RuntimeProtocolError("SCHEMA_VALIDATION_FAILED", "Plan must contain todo and acceptance criteria.")
        self._validate_plan_links(todo, criteria)
        target = TaskState.BLOCKED if planner_output.get("needs_human") else TaskState.READY
        contract = TaskContract.from_dict({
            **snapshot["task"],
            "objective": planner_output.get("objective"),
            "non_goals": planner_output.get("non_goals", []),
            "todo": [item.to_dict() for item in todo],
            "acceptance_criteria": [item.to_dict() for item in criteria],
            "state": target.value,
            "contract_version": int(snapshot["task"].get("contract_version", 1)) + 1,
            "plan_id": _new_id("plan"),
        })
        self.storage.replace_plan(contract, {
            "plan_id": contract.plan_id,
            "ambiguities": planner_output.get("ambiguities", []),
            "needs_human": bool(planner_output.get("needs_human")),
        })
        return self.storage.snapshot(task_id)

    @staticmethod
    def _validate_plan_links(todo: list[TodoItem], criteria: list[AcceptanceCriterion]) -> None:
        todo_ids = [item.id for item in todo]
        criterion_ids = [item.id for item in criteria]
        if len(set(todo_ids)) != len(todo_ids) or len(set(criterion_ids)) != len(criterion_ids):
            raise RuntimeProtocolError("SCHEMA_VALIDATION_FAILED", "Todo and criterion ids must be unique.")
        unknown_dependencies = sorted({dep for item in todo for dep in item.depends_on if dep not in set(todo_ids)})
        if unknown_dependencies:
            raise RuntimeProtocolError(
                "SCHEMA_VALIDATION_FAILED",
                "Todo contains unknown dependencies.",
                details={"unknown_dependencies": unknown_dependencies},
            )

    def append_event(self, task_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        event_type = str(payload.get("event_type", "")).strip()
        if not event_type:
            raise RuntimeProtocolError("SCHEMA_VALIDATION_FAILED", "event_type is required.")
        snapshot = self.storage.snapshot(task_id)
        current = TaskState(snapshot["task"]["state"])
        target_raw = payload.get("state")
        try:
            target = TaskState(target_raw) if target_raw else None
        except ValueError as exc:
            raise RuntimeProtocolError("SCHEMA_VALIDATION_FAILED", f"Unknown task state: {target_raw!r}.") from exc
        if target is not None:
            validate_transition(current, target)
        revision = payload.get("workspace_revision")
        if revision is not None and (not isinstance(revision, str) or not revision.strip()):
            raise RuntimeProtocolError("SCHEMA_VALIDATION_FAILED", "workspace_revision must be a non-empty string.")
        self.storage.append_event(task_id, event_type, dict(payload), new_state=target,
                                  workspace_revision=revision.strip() if isinstance(revision, str) else None)
        return self.storage.snapshot(task_id)

    def request_rework(self, task_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        items = payload.get("items")
        if not isinstance(items, list) or not items or any(not isinstance(item, str) or not item.strip() for item in items):
            raise RuntimeProtocolError("SCHEMA_VALIDATION_FAILED", "rework items must be a non-empty string array.")
        snapshot = self.storage.snapshot(task_id)
        current = TaskState(snapshot["task"]["state"])
        if current == TaskState.CLOSED:
            raise ConflictError("TASK_CLOSED", "A closed task must be explicitly reopened before rework.")
        if current != TaskState.REWORK_REQUIRED:
            validate_transition(current, TaskState.REWORK_REQUIRED)
        self.storage.append_event(task_id, "rework_required", dict(payload), new_state=TaskState.REWORK_REQUIRED)
        return self.storage.snapshot(task_id)

    def start_or_resume(self, task_id: str) -> dict[str, Any]:
        snapshot = self.storage.snapshot(task_id)
        current = TaskState(snapshot["task"]["state"])
        validate_transition(current, TaskState.EXECUTING)
        self.storage.append_event(task_id, "execution_started", {}, new_state=TaskState.EXECUTING)
        return self.storage.snapshot(task_id)
