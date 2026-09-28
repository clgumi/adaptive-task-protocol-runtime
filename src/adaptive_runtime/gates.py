"""Evidence gate and close policy."""

from __future__ import annotations

from typing import Any, Mapping

from .errors import ConflictError, RuntimeProtocolError
from .evidence import EvidenceService
from .models import ACStatus, Evidence, EvidenceStatus, GateResult, TaskState, utc_now, validate_transition
from .storage import Storage
from .verifiers import verify_judged_criterion, verify_machine_criterion


class GateService:
    def __init__(self, storage: Storage):
        self.storage = storage

    def evaluate(self, task_id: str, judge_result: Mapping[str, Any] | None = None) -> dict[str, Any]:
        snapshot = self.storage.snapshot(task_id)
        task = snapshot["task"]
        current = TaskState(task["state"])
        if current == TaskState.CLOSED:
            raise ConflictError("TASK_CLOSED", "Closed tasks cannot be evaluated without an explicit reopen operation.")
        if current == TaskState.SELF_CHECK:
            self.storage.append_event(task_id, "review_started", {}, new_state=TaskState.REVIEWING)
            snapshot = self.storage.snapshot(task_id)
            task = snapshot["task"]
            current = TaskState.REVIEWING
        if current not in {TaskState.REVIEWING, TaskState.GATE_PASSED}:
            raise ConflictError(
                "GATE_NOT_READY",
                f"Task must be in SELF_CHECK, REVIEWING, or GATE_PASSED; current state is {current.value}.",
            )
        snapshot = self._revalidate_evidence(snapshot)
        task = snapshot["task"]
        judge_by_id = self._validate_judge(task_id, task, judge_result) if judge_result else {}
        revision = task["workspace"]["revision"]
        updated: list[dict[str, Any]] = []
        blocking: list[str] = []
        has_blocked = False
        for criterion in task["acceptance_criteria"]:
            relevant = [item for item in snapshot["evidence"] if item["criterion_id"] == criterion["id"]]
            if criterion.get("kind") == "machine_verifiable":
                outcome = verify_machine_criterion(criterion, relevant, revision)
            else:
                judge_item = judge_by_id.get(criterion["id"])
                if judge_item is None and criterion.get("status") == ACStatus.PASS.value:
                    judge_item = {
                        "status": ACStatus.PASS.value,
                        "reason": criterion.get("reason") or "Previously verified Judge result.",
                        "evidence_refs": criterion.get("evidence_refs", []),
                    }
                outcome = verify_judged_criterion(criterion, relevant, revision, judge_item)
            if outcome["status"] == ACStatus.PASS.value:
                missing = self._missing_required_evidence(criterion, relevant, revision)
                if missing:
                    outcome = {
                        **outcome,
                        "status": ACStatus.NOT_RUN.value,
                        "reason": f"Required evidence is missing: {', '.join(missing)}.",
                    }
            merged = {**criterion, **outcome}
            status = ACStatus(merged["status"])
            merged["evidence_status"] = (
                EvidenceStatus.VERIFIED.value if status == ACStatus.PASS else EvidenceStatus.PENDING.value
            )
            if status == ACStatus.PASS:
                merged["verified_revision"] = revision
                merged["verified_at"] = utc_now()
            else:
                merged["verified_revision"] = None
                merged["verified_at"] = None
            if criterion.get("required", True) and status not in {ACStatus.PASS, ACStatus.WAIVED}:
                blocking.append(criterion["id"])
                has_blocked = has_blocked or status == ACStatus.BLOCKED
            if status == ACStatus.WAIVED and not self._valid_waiver(criterion):
                merged["status"] = ACStatus.BLOCKED.value
                merged["reason"] = "WAIVED requires an explicit reason and approval record."
                if criterion["id"] not in blocking:
                    blocking.append(criterion["id"])
                has_blocked = True
            updated.append(merged)
        passed = not blocking
        target = TaskState.GATE_PASSED if passed else TaskState.BLOCKED if has_blocked else TaskState.REWORK_REQUIRED
        if current != target:
            validate_transition(current, target)
        result = GateResult(
            task_id=task_id,
            passed=passed,
            state=target,
            criteria=updated,
            blocking_criteria=blocking,
            plan_id=task.get("plan_id"),
            contract_version=task.get("contract_version", 1),
        )
        self.storage.update_criteria_and_gate(task_id, updated, result)
        return result.to_dict()

    def _revalidate_evidence(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        task = snapshot["task"]
        criteria = {item["id"]: item for item in task["acceptance_criteria"]}
        service = EvidenceService(self.storage)
        changed = False
        for raw in snapshot["evidence"]:
            criterion = criteria.get(raw.get("criterion_id"))
            if criterion is None:
                continue
            evidence = Evidence.from_dict(raw)
            previous = (evidence.status.value, evidence.validation_message)
            service._verify_integrity(evidence, task, criterion)
            if previous != (evidence.status.value, evidence.validation_message):
                self.storage.update_evidence(evidence)
                changed = True
        return self.storage.snapshot(task["task_id"]) if changed else snapshot

    def close(self, task_id: str, judge_result: Mapping[str, Any] | None = None) -> dict[str, Any]:
        gate = self.evaluate(task_id, judge_result)
        if not gate["passed"]:
            return {"closed": False, "state": gate["state"], "blocking_criteria": gate["blocking_criteria"], "gate": gate}
        validate_transition(TaskState.GATE_PASSED, TaskState.CLOSED)
        self.storage.append_event(task_id, "task_closed", {"gate_evaluated_at": gate["evaluated_at"]}, new_state=TaskState.CLOSED)
        return {"closed": True, "state": TaskState.CLOSED.value, "blocking_criteria": [], "gate": gate}

    @staticmethod
    def _valid_waiver(criterion: dict[str, Any]) -> bool:
        waiver = criterion.get("waiver")
        return isinstance(waiver, dict) and bool(waiver.get("reason")) and bool(waiver.get("approved_by"))

    @staticmethod
    def _missing_required_evidence(criterion: dict[str, Any], evidence: list[dict[str, Any]], revision: str) -> list[str]:
        usable = [item for item in evidence if item.get("status") == EvidenceStatus.VERIFIED.value
                  and item.get("workspace_revision") == revision]
        missing: list[str] = []
        for required in criterion.get("required_evidence", []):
            if required == "workspace_revision":
                present = bool(usable)
            elif required == "exit_code":
                present = any(item.get("exit_code") is not None for item in usable)
            elif required == "command":
                present = any(item.get("command") for item in usable)
            elif required == "test_report":
                present = any(item.get("type") == "test_report" for item in usable)
            elif required == "build_log":
                present = any(item.get("artifact_path") and item.get("type") in {"command_result", "build_log"} for item in usable)
            elif required == "startup_log":
                present = any(item.get("artifact_path") and item.get("type") in {"readiness", "readiness_check", "startup", "startup_log"} for item in usable)
            elif required in {"artifact", "artifact_path", "report", "screenshot", "video", "visual_artifact", "log"}:
                present = any(item.get("artifact_path") for item in usable)
            else:
                present = any(item.get("metadata", {}).get(required) is not None for item in usable)
            if not present:
                missing.append(required)
        return missing

    @staticmethod
    def _validate_judge(task_id: str, task: dict[str, Any], result: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        if result.get("task_id") != task_id:
            raise RuntimeProtocolError("JUDGE_TASK_MISMATCH", "Judge result task_id does not match the evaluated task.")
        known = {item["id"] for item in task["acceptance_criteria"]}
        items = result.get("criteria")
        if not isinstance(items, list):
            raise RuntimeProtocolError("SCHEMA_VALIDATION_FAILED", "Judge criteria must be an array.")
        unknown = sorted({str(item.get("id")) for item in items if item.get("id") not in known})
        if unknown:
            raise RuntimeProtocolError("UNKNOWN_CRITERION", "Judge returned unknown acceptance criteria.", details={"ids": unknown})
        by_id = {item["id"]: dict(item) for item in items}
        if len(by_id) != len(items):
            raise RuntimeProtocolError("SCHEMA_VALIDATION_FAILED", "Judge criterion ids must be unique.")
        return by_id
