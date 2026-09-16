"""Schemas exposed to the Hermes model for Adaptive Bridge tools."""

START = {
    "description": "Create and plan an Adaptive Runtime Project task before any project write.",
    "parameters": {
        "type": "object",
        "properties": {
            "objective": {"type": "string"},
            "workspace": {"type": "string"},
            "workspace_revision": {"type": "string"},
            "non_goals": {"type": "array", "items": {"type": "string"}},
            "project_context": {"type": "object"}
        },
        "required": ["objective", "workspace"]
    }
}
BIND = {
    "description": "Bind this Hermes session to an existing Adaptive Runtime task after a process restart.",
    "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}
}
EVENT = {
    "description": "Record a Project lifecycle event. Use todo_started with todo_id before project writes.",
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
