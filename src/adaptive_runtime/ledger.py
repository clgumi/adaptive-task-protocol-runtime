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
        committed = snapshot["task"]
        current = TaskState(committed["state"])
        if current not in {TaskState.PLANNING, TaskState.BLOCKED, TaskState.REWORK_REQUIRED, TaskState.VERIFIER_DEFECT}:
            raise ConflictError("PLAN_NOT_ALLOWED", f"Cannot submit a plan while task is {current.value}.")
        old_plan_id = committed.get("plan_id")
        repair_reason = ""
        if old_plan_id:
            repair_reason = str(planner_output.get("repair_reason") or "").strip()
            if not repair_reason:
                raise ConflictError(
                    "PLAN_REPAIR_REASON_REQUIRED",
                    "Replacing a committed plan requires a concise repair_reason.",
                    {"task_id": task_id, "old_plan_id": old_plan_id},
                )
            proposed_objective = str(planner_output.get("objective") or "").strip()
            proposed_non_goals = planner_output.get("non_goals", [])
            normalize = lambda values: sorted(" ".join(str(value).split()).casefold() for value in values)
            if proposed_objective != committed.get("objective") or normalize(proposed_non_goals) != normalize(committed.get("non_goals", [])):
                raise ConflictError(
                    "PLAN_SCOPE_CHANGE_REQUIRES_NEW_TASK",
                    "Plan repair cannot change the committed objective or non-goals; create an explicitly scoped new task instead.",
                    {"task_id": task_id, "old_plan_id": old_plan_id},
                )
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
        validate_transition(current, target)
        self.storage.replace_plan(contract, {
            "plan_id": contract.plan_id,
            "old_plan_id": old_plan_id,
            "repair_reason": repair_reason or None,
            "contract_version": contract.contract_version,
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
        dependency_map = {item.id: set(item.depends_on) for item in todo}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(todo_id: str) -> None:
            if todo_id in visiting:
                raise RuntimeProtocolError("SCHEMA_VALIDATION_FAILED", "Todo dependencies must be acyclic.")
            if todo_id in visited:
                return
            visiting.add(todo_id)
            for dependency in dependency_map[todo_id]:
                visit(dependency)
            visiting.remove(todo_id)
            visited.add(todo_id)

        for todo_id in dependency_map:
            visit(todo_id)
        blank_verifications = [item.id for item in todo if not item.verification.strip()]
        if blank_verifications:
            raise RuntimeProtocolError(
                "SCHEMA_VALIDATION_FAILED",
                "Every Todo must have a concrete verification description.",
                details={"todo_ids": blank_verifications},
            )
        for criterion in criteria:
            if criterion.kind.value != "machine_verifiable":
                continue
            verifier = criterion.verifier
            verifier_type = str(verifier.get("type") or "").strip()
            if verifier_type in {"command", "build", "test"}:
                commands = verifier.get("commands") or ([verifier["command"]] if verifier.get("command") else [])
                if not isinstance(commands, list) or not commands or any(not isinstance(command, str) or not command.strip() for command in commands):
                    raise RuntimeProtocolError(
                        "VERIFIER_PREFLIGHT_INVALID",
                        f"Criterion {criterion.id} requires at least one non-empty verifier command.",
                    )
                if len(set(commands)) != len(commands):
                    raise RuntimeProtocolError(
                        "VERIFIER_PREFLIGHT_INVALID",
                        f"Criterion {criterion.id} contains duplicate verifier commands.",
                    )
            elif verifier_type in {"readiness", "startup"}:
                expected = verifier.get("expected_status", [200, 204])
                expected = expected if isinstance(expected, list) else [expected]
                if not expected or any(not isinstance(code, int) or isinstance(code, bool) or code < 100 or code > 599 for code in expected):
                    raise RuntimeProtocolError(
                        "VERIFIER_PREFLIGHT_INVALID",
                        f"Criterion {criterion.id} has an invalid expected_status set.",
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
            details = payload.get("details") if isinstance(payload.get("details"), Mapping) else {}
            reason = str(details.get("reason") or payload.get("reason") or "").strip()
            if target == TaskState.BLOCKED and target != current and not reason:
                raise RuntimeProtocolError(
                    "BLOCK_REASON_REQUIRED",
                    "Moving a task to BLOCKED requires a durable reason in details.reason.",
                )
            if target == TaskState.VERIFIER_DEFECT:
                if event_type != "verifier_defect_reported" or not reason:
                    raise RuntimeProtocolError(
                        "VERIFIER_DEFECT_REASON_REQUIRED",
                        "Use verifier_defect_reported with a concise details.reason before repairing the plan.",
                    )
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
