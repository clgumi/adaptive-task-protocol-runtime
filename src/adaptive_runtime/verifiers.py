"""Deterministic acceptance-criterion verifiers."""

from __future__ import annotations

from typing import Any

from .models import ACStatus, EvidenceStatus


def _current_verified(evidence: list[dict[str, Any]], revision: str) -> list[dict[str, Any]]:
    return [
        item for item in evidence
        if item.get("status") == EvidenceStatus.VERIFIED.value and item.get("workspace_revision") == revision
    ]


def _result(status: ACStatus, refs: list[str], reason: str, verifier_type: str) -> dict[str, Any]:
    return {
        "status": status.value,
        "evidence_refs": refs,
        "reason": reason,
        "verifier_type": verifier_type,
    }


def verify_machine_criterion(criterion: dict[str, Any], evidence: list[dict[str, Any]], revision: str) -> dict[str, Any]:
    verifier = criterion.get("verifier", {})
    verifier_type = str(verifier.get("type", "")).strip()
    usable = _current_verified(evidence, revision)
    if not usable:
        if evidence and all(item.get("status") == EvidenceStatus.STALE.value for item in evidence):
            return _result(ACStatus.NOT_RUN, [], "Only stale evidence is available.", verifier_type)
        return _result(ACStatus.NOT_RUN, [], "No verified evidence is available.", verifier_type)

    if verifier_type in {"command", "build", "test"}:
        commands = verifier.get("commands") or ([verifier["command"]] if verifier.get("command") else [])
        if not isinstance(commands, list) or not commands:
            return _result(ACStatus.BLOCKED, [], "Verifier has no configured command.", verifier_type)
        matched: list[dict[str, Any]] = []
        for command in commands:
            candidate = next((item for item in usable if item.get("command") == command), None)
            if candidate is None:
                return _result(ACStatus.NOT_RUN, [item["evidence_id"] for item in matched],
                               f"No verified result for command: {command}", verifier_type)
            matched.append(candidate)
        refs = [item["evidence_id"] for item in matched]
        failed = [item for item in matched if item.get("exit_code") != 0]
        if failed:
            return _result(ACStatus.FAIL, refs, "One or more configured commands returned a non-zero exit code.", verifier_type)
        if verifier_type == "test" or verifier.get("require_test_report"):
            report = next((item for item in usable if item.get("type") == "test_report"), None)
            if report is None:
                report = next((item for item in usable if "failures" in item.get("metadata", {})), None)
            if report is None:
                return _result(ACStatus.NOT_RUN, refs, "A verified test report is required.", verifier_type)
            refs.append(report["evidence_id"])
            failures = report.get("metadata", {}).get("failures")
            if not isinstance(failures, int):
                return _result(ACStatus.NOT_RUN, refs, "Test report does not contain an integer failures count.", verifier_type)
            if failures != 0:
                return _result(ACStatus.FAIL, refs, f"Test report contains {failures} failure(s).", verifier_type)
        return _result(ACStatus.PASS, refs, "All configured commands completed successfully.", verifier_type)

    if verifier_type in {"readiness", "startup"}:
        candidates = [item for item in usable if item.get("type") in {"readiness", "readiness_check", "startup"}]
        candidate = next((item for item in candidates if (
            (item.get("metadata", {}).get("service_id") or item.get("metadata", {}).get("process_id"))
            and isinstance(item.get("metadata", {}).get("status_code"), int)
        )), candidates[0] if candidates else None)
        if candidate is None:
            return _result(ACStatus.NOT_RUN, [], "No verified readiness result is available.", verifier_type)
        refs = [candidate["evidence_id"]]
        metadata = candidate.get("metadata", {})
        status_code = metadata.get("status_code")
        service_id = metadata.get("service_id") or metadata.get("process_id")
        if not service_id:
            return _result(ACStatus.NOT_RUN, refs, "Readiness evidence is missing a service or process identifier.", verifier_type)
        if not isinstance(status_code, int):
            return _result(ACStatus.NOT_RUN, refs, "Readiness evidence is missing an integer status_code.", verifier_type)
        expected = verifier.get("expected_status", [200, 204])
        expected = expected if isinstance(expected, list) else [expected]
        if status_code not in expected:
            return _result(ACStatus.FAIL, refs, f"Readiness returned unexpected status {status_code}.", verifier_type)
        return _result(ACStatus.PASS, refs, "Service passed the configured readiness check.", verifier_type)

    if verifier_type == "artifact":
        refs = [item["evidence_id"] for item in usable if item.get("artifact_path")]
        if not refs:
            return _result(ACStatus.NOT_RUN, [], "No verified artifact is available.", verifier_type)
        return _result(ACStatus.PASS, refs, "Required artifact evidence is verified.", verifier_type)

    return _result(ACStatus.BLOCKED, [], f"Unsupported deterministic verifier type: {verifier_type or '<empty>'}.", verifier_type)


def verify_judged_criterion(criterion: dict[str, Any], evidence: list[dict[str, Any]], revision: str,
                            judge_item: dict[str, Any] | None) -> dict[str, Any]:
    verifier_type = str(criterion.get("verifier", {}).get("type", "judge"))
    if judge_item is None:
        return _result(ACStatus.NOT_RUN, [], "No Judge result is available.", verifier_type)
    try:
        status = ACStatus(judge_item.get("status"))
    except ValueError:
        return _result(ACStatus.BLOCKED, [], "Judge returned an invalid status.", verifier_type)
    refs = list(judge_item.get("evidence_refs") or [])
    if status == ACStatus.PASS:
        if not refs:
            return _result(ACStatus.PASS_UNVERIFIED, [], "Judge reported PASS without evidence references.", verifier_type)
        evidence_by_id = {item["evidence_id"]: item for item in evidence}
        invalid_refs = [ref for ref in refs if ref not in evidence_by_id]
        stale_refs = [ref for ref in refs if ref in evidence_by_id and (
            evidence_by_id[ref].get("status") != EvidenceStatus.VERIFIED.value
            or evidence_by_id[ref].get("workspace_revision") != revision
        )]
        if invalid_refs or stale_refs:
            return _result(ACStatus.PASS_UNVERIFIED, refs, "Judge references missing, invalid, or stale evidence.", verifier_type)
    return _result(status, refs, str(judge_item.get("reason") or "Judge result supplied."), verifier_type)

