"""Versioned protocol models with dependency-free validation."""

from __future__ import annotations

import dataclasses
import enum
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from .errors import RuntimeProtocolError

PROTOCOL_VERSION = "1.0"
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class TaskMode(str, enum.Enum):
    CHAT = "chat"
    OPERATION = "operation"
    PROJECT = "project"


class TaskState(str, enum.Enum):
    CREATED = "CREATED"
    PLANNING = "PLANNING"
    READY = "READY"
    EXECUTING = "EXECUTING"
    SELF_CHECK = "SELF_CHECK"
    REVIEWING = "REVIEWING"
    REWORK_REQUIRED = "REWORK_REQUIRED"
    VERIFIER_DEFECT = "VERIFIER_DEFECT"
    BLOCKED = "BLOCKED"
    GATE_PASSED = "GATE_PASSED"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


class ACStatus(str, enum.Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_RUN = "NOT_RUN"
    BLOCKED = "BLOCKED"
    WAIVED = "WAIVED"
    PASS_UNVERIFIED = "PASS_UNVERIFIED"


class EvidenceStatus(str, enum.Enum):
    PENDING = "pending"
    VERIFIED = "verified"
    INVALID = "invalid"
    STALE = "stale"


class CriterionKind(str, enum.Enum):
    MACHINE = "machine_verifiable"
    SEMANTIC = "semantic"
    VISUAL = "visual"


MACHINE_VERIFIER_TYPES = frozenset({
    "artifact", "build", "command", "readiness", "startup", "test",
})

REQUIRED_EVIDENCE_KEYS = frozenset({
    "artifact", "artifact_path", "build_log", "command", "exit_code", "log",
    "process_id", "report", "screenshot", "service_id", "startup_log",
    "status_code", "test_report", "video", "visual_artifact", "workspace_revision",
})


TERMINAL_STATES = {TaskState.CLOSED, TaskState.CANCELLED}

ALLOWED_TRANSITIONS: dict[TaskState, set[TaskState]] = {
    TaskState.CREATED: {TaskState.PLANNING, TaskState.BLOCKED, TaskState.CANCELLED},
    TaskState.PLANNING: {TaskState.READY, TaskState.BLOCKED, TaskState.CANCELLED},
    TaskState.READY: {TaskState.EXECUTING, TaskState.BLOCKED, TaskState.CANCELLED},
    TaskState.EXECUTING: {TaskState.SELF_CHECK, TaskState.REVIEWING, TaskState.VERIFIER_DEFECT, TaskState.BLOCKED, TaskState.CANCELLED},
    TaskState.SELF_CHECK: {TaskState.REVIEWING, TaskState.REWORK_REQUIRED, TaskState.VERIFIER_DEFECT, TaskState.BLOCKED, TaskState.CANCELLED},
    TaskState.REVIEWING: {TaskState.REWORK_REQUIRED, TaskState.VERIFIER_DEFECT, TaskState.GATE_PASSED, TaskState.BLOCKED, TaskState.CANCELLED},
    TaskState.REWORK_REQUIRED: {TaskState.EXECUTING, TaskState.READY, TaskState.VERIFIER_DEFECT, TaskState.BLOCKED, TaskState.CANCELLED},
    TaskState.GATE_PASSED: {TaskState.CLOSED, TaskState.REWORK_REQUIRED, TaskState.BLOCKED},
    TaskState.BLOCKED: {TaskState.PLANNING, TaskState.READY, TaskState.EXECUTING, TaskState.VERIFIER_DEFECT, TaskState.CANCELLED},
    TaskState.VERIFIER_DEFECT: {TaskState.READY, TaskState.BLOCKED, TaskState.CANCELLED},
    TaskState.CLOSED: set(),
    TaskState.CANCELLED: set(),
}


def require_mapping(value: Any, name: str = "payload") -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimeProtocolError("SCHEMA_VALIDATION_FAILED", f"{name} must be an object.")
    return value


def require_string(value: Any, name: str, *, allow_empty: bool = False, max_length: int = 16_384) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise RuntimeProtocolError("SCHEMA_VALIDATION_FAILED", f"{name} must be a non-empty string.")
    if len(value) > max_length:
        raise RuntimeProtocolError("SCHEMA_VALIDATION_FAILED", f"{name} exceeds {max_length} characters.")
    return value.strip() if not allow_empty else value


def require_id(value: Any, name: str) -> str:
    result = require_string(value, name, max_length=128)
    if not ID_PATTERN.fullmatch(result):
        raise RuntimeProtocolError("SCHEMA_VALIDATION_FAILED", f"{name} has an invalid identifier format.")
    return result


def require_protocol(value: Any) -> str:
    if value != PROTOCOL_VERSION:
        raise RuntimeProtocolError(
            "PROTOCOL_VERSION_UNSUPPORTED",
            f"protocol_version must be {PROTOCOL_VERSION!r}.",
            details={"supported": PROTOCOL_VERSION, "received": value},
        )
    return PROTOCOL_VERSION


def _string_list(value: Any, name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise RuntimeProtocolError("SCHEMA_VALIDATION_FAILED", f"{name} must be an array of non-empty strings.")
    return [item.strip() for item in value]


@dataclass(slots=True)
class TodoItem:
    id: str
    title: str
    depends_on: list[str] = field(default_factory=list)
    verification: str = ""
    status: str = "PENDING"

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "TodoItem":
        raw = require_mapping(raw, "todo item")
        return cls(
            id=require_id(raw.get("id"), "todo.id"),
            title=require_string(raw.get("title"), "todo.title", max_length=2_000),
            depends_on=_string_list(raw.get("depends_on", []), "todo.depends_on"),
            verification=require_string(raw.get("verification", ""), "todo.verification", allow_empty=True, max_length=4_000),
            status=str(raw.get("status", "PENDING")),
        )

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass(slots=True)
class AcceptanceCriterion:
    id: str
    description: str
    kind: CriterionKind
    verifier: dict[str, Any]
    required_evidence: list[str]
    required: bool = True
    status: ACStatus = ACStatus.NOT_RUN
    evidence_refs: list[str] = field(default_factory=list)
    evidence_status: EvidenceStatus = EvidenceStatus.PENDING
    verified_revision: str | None = None
    verified_at: str | None = None
    verifier_type: str | None = None
    reason: str | None = None
    waiver: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AcceptanceCriterion":
        raw = require_mapping(raw, "acceptance criterion")
        try:
            kind = CriterionKind(raw.get("kind"))
        except ValueError as exc:
            raise RuntimeProtocolError("SCHEMA_VALIDATION_FAILED", "acceptance_criteria.kind is invalid.") from exc
        verifier = raw.get("verifier")
        if not isinstance(verifier, dict) or not isinstance(verifier.get("type"), str):
            raise RuntimeProtocolError("SCHEMA_VALIDATION_FAILED", "acceptance_criteria.verifier.type is required.")
        verifier_type = verifier["type"].strip()
        if kind == CriterionKind.MACHINE and verifier_type not in MACHINE_VERIFIER_TYPES:
            raise RuntimeProtocolError(
                "SCHEMA_VALIDATION_FAILED",
                f"Unsupported machine verifier type: {verifier_type or '<empty>'}.",
                details={"supported": sorted(MACHINE_VERIFIER_TYPES)},
            )
        required_evidence = _string_list(raw.get("required_evidence", []), "acceptance_criteria.required_evidence")
        unknown_evidence = sorted(set(required_evidence) - REQUIRED_EVIDENCE_KEYS)
        if unknown_evidence:
            raise RuntimeProtocolError(
                "SCHEMA_VALIDATION_FAILED",
                "acceptance_criteria.required_evidence contains unsupported keys.",
                details={"unsupported": unknown_evidence, "supported": sorted(REQUIRED_EVIDENCE_KEYS)},
            )
        try:
            status = ACStatus(raw.get("status", ACStatus.NOT_RUN.value))
            evidence_status = EvidenceStatus(raw.get("evidence_status", EvidenceStatus.PENDING.value))
        except ValueError as exc:
            raise RuntimeProtocolError("SCHEMA_VALIDATION_FAILED", "acceptance criterion status is invalid.") from exc
        return cls(
            id=require_id(raw.get("id"), "acceptance_criteria.id"),
            description=require_string(raw.get("description"), "acceptance_criteria.description", max_length=4_000),
            kind=kind,
            verifier=dict(verifier),
            required_evidence=required_evidence,
            required=bool(raw.get("required", True)),
            status=status,
            evidence_refs=_string_list(raw.get("evidence_refs", []), "acceptance_criteria.evidence_refs"),
            evidence_status=evidence_status,
            verified_revision=raw.get("verified_revision"),
            verified_at=raw.get("verified_at"),
            verifier_type=raw.get("verifier_type"),
            reason=raw.get("reason"),
            waiver=dict(raw["waiver"]) if isinstance(raw.get("waiver"), Mapping) else None,
        )

    def to_dict(self) -> dict[str, Any]:
        result = dataclasses.asdict(self)
        result["kind"] = self.kind.value
        result["status"] = self.status.value
        result["evidence_status"] = self.evidence_status.value
        return result


@dataclass(slots=True)
class TaskContract:
    task_id: str
    mode: TaskMode
    objective: str
    workspace: dict[str, str]
    non_goals: list[str] = field(default_factory=list)
    todo: list[TodoItem] = field(default_factory=list)
    acceptance_criteria: list[AcceptanceCriterion] = field(default_factory=list)
    state: TaskState = TaskState.PLANNING
    created_by: str = "hermes-bridge"
    created_at: str = field(default_factory=utc_now)
    protocol_version: str = PROTOCOL_VERSION
    contract_version: int = 1
    plan_id: str | None = None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "TaskContract":
        raw = require_mapping(raw, "task contract")
        require_protocol(raw.get("protocol_version"))
        workspace = require_mapping(raw.get("workspace"), "workspace")
        try:
            mode = TaskMode(raw.get("mode"))
            state = TaskState(raw.get("state", TaskState.PLANNING.value))
        except ValueError as exc:
            raise RuntimeProtocolError("SCHEMA_VALIDATION_FAILED", "task mode or state is invalid.") from exc
        contract_version = raw.get("contract_version", 1)
        if not isinstance(contract_version, int) or isinstance(contract_version, bool) or contract_version < 1:
            raise RuntimeProtocolError("SCHEMA_VALIDATION_FAILED", "contract_version must be a positive integer.")
        return cls(
            task_id=require_id(raw.get("task_id"), "task_id"),
            mode=mode,
            objective=require_string(raw.get("objective"), "objective"),
            workspace={
                "path": require_string(workspace.get("path"), "workspace.path", max_length=4_096),
                "revision": require_string(workspace.get("revision"), "workspace.revision", max_length=512),
            },
            non_goals=_string_list(raw.get("non_goals", []), "non_goals"),
            todo=[TodoItem.from_dict(item) for item in raw.get("todo", [])],
            acceptance_criteria=[AcceptanceCriterion.from_dict(item) for item in raw.get("acceptance_criteria", [])],
            state=state,
            created_by=require_string(raw.get("created_by", "hermes-bridge"), "created_by", max_length=256),
            created_at=require_string(raw.get("created_at", utc_now()), "created_at", max_length=64),
            contract_version=contract_version,
            plan_id=raw.get("plan_id"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "contract_version": self.contract_version,
            "task_id": self.task_id,
            "mode": self.mode.value,
            "objective": self.objective,
            "non_goals": list(self.non_goals),
            "workspace": dict(self.workspace),
            "todo": [item.to_dict() for item in self.todo],
            "acceptance_criteria": [item.to_dict() for item in self.acceptance_criteria],
            "state": self.state.value,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "plan_id": self.plan_id,
        }


@dataclass(slots=True)
class Evidence:
    evidence_id: str
    task_id: str
    criterion_id: str
    type: str
    workspace_revision: str
    produced_at: str
    producer: str
    artifact_path: str | None = None
    artifact_sha256: str | None = None
    command: str | None = None
    exit_code: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    plan_id: str | None = None
    contract_version: int | None = None
    status: EvidenceStatus = EvidenceStatus.PENDING
    validation_message: str | None = None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Evidence":
        raw = require_mapping(raw, "evidence")
        require_protocol(raw.get("protocol_version"))
        exit_code = raw.get("exit_code")
        if exit_code is not None and (not isinstance(exit_code, int) or isinstance(exit_code, bool)):
            raise RuntimeProtocolError("SCHEMA_VALIDATION_FAILED", "exit_code must be an integer.")
        contract_version = raw.get("contract_version")
        if contract_version is not None and (not isinstance(contract_version, int) or isinstance(contract_version, bool) or contract_version < 1):
            raise RuntimeProtocolError("SCHEMA_VALIDATION_FAILED", "evidence contract_version must be a positive integer.")
        metadata = raw.get("metadata", {})
        if not isinstance(metadata, dict):
            raise RuntimeProtocolError("SCHEMA_VALIDATION_FAILED", "metadata must be an object.")
        try:
            status = EvidenceStatus(raw.get("status", EvidenceStatus.PENDING.value))
        except ValueError as exc:
            raise RuntimeProtocolError("SCHEMA_VALIDATION_FAILED", "evidence status is invalid.") from exc
        return cls(
            evidence_id=require_id(raw.get("evidence_id"), "evidence_id"),
            task_id=require_id(raw.get("task_id"), "task_id"),
            criterion_id=require_id(raw.get("criterion_id"), "criterion_id"),
            type=require_string(raw.get("type"), "type", max_length=128),
            workspace_revision=require_string(raw.get("workspace_revision"), "workspace_revision", max_length=512),
            produced_at=require_string(raw.get("produced_at"), "produced_at", max_length=64),
            producer=require_string(raw.get("producer"), "producer", max_length=256),
            artifact_path=raw.get("artifact_path"),
            artifact_sha256=raw.get("artifact_sha256"),
            command=raw.get("command"),
            exit_code=exit_code,
            metadata=dict(metadata),
            plan_id=raw.get("plan_id"),
            contract_version=contract_version,
            status=status,
            validation_message=raw.get("validation_message"),
        )

    def to_dict(self) -> dict[str, Any]:
        result = dataclasses.asdict(self)
        result["protocol_version"] = PROTOCOL_VERSION
        result["status"] = self.status.value
        return result


@dataclass(slots=True)
class GateResult:
    task_id: str
    passed: bool
    state: TaskState
    criteria: list[dict[str, Any]]
    blocking_criteria: list[str]
    evaluated_at: str = field(default_factory=utc_now)
    plan_id: str | None = None
    contract_version: int | None = None
    protocol_version: str = PROTOCOL_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "task_id": self.task_id,
            "passed": self.passed,
            "state": self.state.value,
            "criteria": self.criteria,
            "blocking_criteria": self.blocking_criteria,
            "evaluated_at": self.evaluated_at,
            "plan_id": self.plan_id,
            "contract_version": self.contract_version,
        }


def validate_transition(current: TaskState, target: TaskState) -> None:
    if target == current:
        return
    if target not in ALLOWED_TRANSITIONS[current]:
        raise RuntimeProtocolError(
            "INVALID_STATE_TRANSITION",
            f"Cannot transition task from {current.value} to {target.value}.",
            status_code=409,
            details={"current": current.value, "target": target.value},
        )
