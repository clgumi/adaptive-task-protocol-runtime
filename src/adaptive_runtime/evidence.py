"""Evidence integrity, containment, and freshness validation."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

from .errors import ConflictError, RuntimeProtocolError
from .models import Evidence, EvidenceStatus, TaskState
from .storage import Storage


ARTIFACT_REQUIREMENTS = {
    "artifact", "artifact_path", "build_log", "log", "report", "screenshot",
    "startup_log", "test_report", "video", "visual_artifact",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_within(path: Path, roots: list[Path]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


class EvidenceService:
    def __init__(self, storage: Storage):
        self.storage = storage

    def submit(self, task_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        snapshot = self.storage.snapshot(task_id)
        task = snapshot["task"]
        state = TaskState(task["state"])
        if state == TaskState.CLOSED:
            raise ConflictError("TASK_CLOSED", "Evidence cannot be added to a closed task.")
        evidence = Evidence.from_dict({
            **dict(payload),
            "task_id": task_id,
            "plan_id": task.get("plan_id"),
            "contract_version": task.get("contract_version", 1),
        })
        criterion = next((item for item in task["acceptance_criteria"] if item["id"] == evidence.criterion_id), None)
        if criterion is None:
            raise RuntimeProtocolError(
                "UNKNOWN_CRITERION",
                f"Criterion {evidence.criterion_id!r} does not belong to task {task_id!r}.",
            )
        evidence = self._verify_integrity(evidence, task, criterion)
        self.storage.insert_evidence(evidence)
        return evidence.to_dict()

    def _verify_integrity(self, evidence: Evidence, task: dict[str, Any], criterion: dict[str, Any]) -> Evidence:
        current_plan_id = task.get("plan_id")
        current_contract_version = task.get("contract_version", 1)
        legacy_unversioned = evidence.plan_id is None and evidence.contract_version is None and current_contract_version == 1
        if not legacy_unversioned and (evidence.plan_id != current_plan_id or evidence.contract_version != current_contract_version):
            evidence.status = EvidenceStatus.STALE
            evidence.validation_message = "Evidence belongs to a different plan or contract version."
            return evidence
        if evidence.workspace_revision != task["workspace"]["revision"]:
            evidence.status = EvidenceStatus.STALE
            evidence.validation_message = "Evidence revision does not match the current workspace revision."
            return evidence
        missing: list[str] = []
        if evidence.type == "command_result":
            if evidence.exit_code is None:
                missing.append("exit_code")
            if not evidence.command:
                missing.append("command")
        if evidence.type in {"artifact", "build_log", "test_report", "screenshot", "startup_log", "visual_artifact"}:
            if not evidence.artifact_path:
                missing.append("artifact_path")
        if missing:
            evidence.status = EvidenceStatus.INVALID
            evidence.validation_message = f"Evidence object is incomplete: {', '.join(sorted(missing))}."
            return evidence
        if evidence.artifact_path:
            artifact = Path(evidence.artifact_path).expanduser().resolve()
            roots = [Path(task["workspace"]["path"]).expanduser().resolve(), self.storage.task_dir(evidence.task_id)]
            if not _is_within(artifact, roots):
                evidence.status = EvidenceStatus.INVALID
                evidence.validation_message = "Artifact path is outside the task workspace and Runtime task directory."
                return evidence
            if not artifact.is_file():
                evidence.status = EvidenceStatus.INVALID
                evidence.validation_message = "Artifact file does not exist."
                return evidence
            if not evidence.artifact_sha256:
                evidence.status = EvidenceStatus.INVALID
                evidence.validation_message = "Artifact SHA-256 is required when artifact_path is present."
                return evidence
            actual_hash = sha256_file(artifact)
            if actual_hash.lower() != evidence.artifact_sha256.lower():
                evidence.status = EvidenceStatus.INVALID
                evidence.validation_message = "Artifact SHA-256 does not match the submitted hash."
                return evidence
        elif evidence.artifact_sha256:
            evidence.status = EvidenceStatus.INVALID
            evidence.validation_message = "artifact_sha256 cannot be verified without artifact_path."
            return evidence
        evidence.status = EvidenceStatus.VERIFIED
        evidence.validation_message = "Evidence integrity and freshness verified."
        return evidence
