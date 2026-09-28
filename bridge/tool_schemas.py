"""Schemas exposed to the Hermes model for Adaptive Bridge tools."""

START = {
    "description": "Create and plan an Adaptive Runtime Project task before any project write. If the session is already bound to a different task scope, review the TASK_SCOPE_MISMATCH result and set new_task=true only when this request should be a separate task.",
    "parameters": {
        "type": "object",
        "properties": {
            "objective": {"type": "string"},
            "workspace": {"type": "string"},
            "workspace_revision": {"type": "string"},
            "non_goals": {"type": "array", "items": {"type": "string"}},
            "project_context": {"type": "object"},
            "new_task": {"type": "boolean", "description": "Explicitly create a separate task when the current session is bound to another task scope."}
        },
        "required": ["objective", "workspace"]
    }
}
BIND = {
    "description": "Bind this Hermes session to an existing Adaptive Runtime task after a process restart.",
    "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}
}
PLAN = {
    "description": "Submit or repair the plan for a bound Adaptive Runtime task. Plan repair is allowed only in PLANNING, BLOCKED, REWORK_REQUIRED, or VERIFIER_DEFECT and cannot change a committed task scope.",
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "protocol_version": {"type": "string"},
            "objective": {"type": "string"},
            "non_goals": {"type": "array", "items": {"type": "string"}},
            "todo": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "title": {"type": "string"},
                        "depends_on": {"type": "array", "items": {"type": "string"}},
                        "verification": {"type": "string"},
                    },
                    "required": ["id", "title", "depends_on", "verification"],
                },
            },
            "acceptance_criteria": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "description": {"type": "string"},
                        "kind": {"type": "string"},
                        "verifier": {"type": "object"},
                        "required_evidence": {"type": "array", "items": {"type": "string"}},
                        "required": {"type": "boolean"},
                    },
                    "required": ["id", "description", "kind", "verifier", "required_evidence"],
                },
            },
            "ambiguities": {"type": "array", "items": {"type": "string"}},
            "needs_human": {"type": "boolean"},
            "repair_reason": {"type": "string", "description": "Required when replacing a committed plan; state the concrete verifier/contract defect being repaired."},
        },
        "required": [
            "task_id", "objective", "non_goals", "todo", "acceptance_criteria",
            "ambiguities", "needs_human",
        ],
    },
}
EVENT = {
    "description": "Record a Project lifecycle event. Use todo_started with todo_id before project writes. If a committed verifier/plan defect is found, pause writes with event_type=verifier_defect_reported, state=VERIFIER_DEFECT, and details.reason; then repair the bound plan with adaptive_task_plan and a repair_reason.",
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"}, "event_type": {"type": "string"}, "todo_id": {"type": "string"},
            "state": {"type": "string"}, "workspace_revision": {"type": "string"}, "details": {"type": "object"}
        },
        "required": ["task_id", "event_type"]
    }
}
EVIDENCE = {
    "description": "Submit revision-bound evidence for an acceptance criterion.",
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"}, "evidence_id": {"type": "string"}, "criterion_id": {"type": "string"},
            "type": {"type": "string"}, "command": {"type": "string"}, "exit_code": {"type": "integer"},
            "workspace_revision": {"type": "string"}, "artifact_path": {"type": "string"},
            "artifact_sha256": {"type": "string"}, "produced_at": {"type": "string"}, "metadata": {"type": "object"}
        },
        "required": ["task_id", "evidence_id", "criterion_id", "type", "workspace_revision", "produced_at"]
    }
}
EVALUATE = {
    "description": "Run deterministic Gate evaluation; optionally run a Judge subagent for semantic/visual criteria.",
    "parameters": {
        "type": "object",
        "properties": {"task_id": {"type": "string"}, "judge_input": {"type": "object"}, "judge_result": {"type": "object"}},
        "required": ["task_id"]
    }
}
CLOSE = {
    "description": "Attempt to close a Project task. Runtime re-evaluates all evidence and refuses incomplete closure.",
    "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}, "judge_result": {"type": "object"}}, "required": ["task_id"]}
}
SNAPSHOT = {
    "description": "Read the current Adaptive Runtime task snapshot.",
    "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}
}
