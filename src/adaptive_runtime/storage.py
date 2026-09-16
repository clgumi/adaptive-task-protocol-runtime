"""SQLite truth store plus append-only JSONL audit logs."""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterator

from .errors import ConflictError, NotFoundError, StorageError
from .models import Evidence, GateResult, TaskContract, TaskState, utc_now


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    objective TEXT NOT NULL,
    workspace_path TEXT NOT NULL,
    workspace_revision TEXT NOT NULL,
    state TEXT NOT NULL,
    contract_version INTEGER NOT NULL,
    protocol_version TEXT NOT NULL,
    plan_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_contracts (
    task_id TEXT NOT NULL,
    contract_version INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (task_id, contract_version),
    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
);
CREATE TABLE IF NOT EXISTS todo_items (
    task_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (task_id, item_id),
    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
);
CREATE TABLE IF NOT EXISTS acceptance_criteria (
    task_id TEXT NOT NULL,
    criterion_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (task_id, criterion_id),
    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
);
CREATE TABLE IF NOT EXISTS evidence_index (
    task_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    criterion_id TEXT NOT NULL,
    status TEXT NOT NULL,
    workspace_revision TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (task_id, evidence_id),
    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
);
CREATE TABLE IF NOT EXISTS gate_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    passed INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
);
CREATE TABLE IF NOT EXISTS subagent_calls (
    call_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    result_hash TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
);
CREATE TABLE IF NOT EXISTS events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
);
CREATE TABLE IF NOT EXISTS current_state (
    task_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
);
"""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


_TODO_EVENT_STATUS = {
    "todo_started": "IN_PROGRESS",
    "todo_completed": "COMPLETED",
    "todo_blocked": "BLOCKED",
    "todo_failed": "BLOCKED",
    "todo_cancelled": "CANCELLED",
    "todo_reopened": "PENDING",
}
_TODO_STATUSES = frozenset({"PENDING", "IN_PROGRESS", "COMPLETED", "BLOCKED", "CANCELLED"})


def _todo_status_update(event_type: str, payload: dict[str, Any]) -> tuple[str, str] | None:
    todo_id = payload.get("todo_id") or payload.get("active_todo_id")
    if not isinstance(todo_id, str) or not todo_id.strip():
        return None
    status = _TODO_EVENT_STATUS.get(event_type)
    if event_type == "todo_status_changed":
        explicit = payload.get("todo_status")
        if explicit not in _TODO_STATUSES:
            raise ValueError("todo_status_changed requires a valid todo_status")
        status = explicit
    if status is None:
        return None
    return todo_id.strip(), status


class Storage:
    def __init__(self, database_path: str | Path, data_dir: str | Path):
        self.database_path = Path(database_path).resolve()
        self.data_dir = Path(data_dir).resolve()
        self._lock = threading.RLock()

    def initialize(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "tasks").mkdir(exist_ok=True)
        try:
            with self.connect() as connection:
                connection.executescript(SCHEMA_SQL)
                connection.commit()
        except sqlite3.Error as exc:
            raise StorageError("Unable to initialize Runtime database.", {"reason": str(exc)}) from exc

    @contextlib.contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
        finally:
            connection.close()

    def task_dir(self, task_id: str) -> Path:
        directory = (self.data_dir / "tasks" / task_id).resolve()
        if directory.parent != (self.data_dir / "tasks").resolve():
            raise StorageError("Invalid task directory.")
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _append_jsonl(self, task_id: str, name: str, payload: dict[str, Any]) -> None:
        path = self.task_dir(task_id) / f"{name}.jsonl"
        try:
            with path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(_json(payload) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            raise StorageError("Unable to append audit log.", {"path": str(path), "reason": str(exc)}) from exc

    def create_task(self, contract: TaskContract) -> None:
        payload = contract.to_dict()
        event = {"event_type": "task_created", "task_id": contract.task_id, "created_at": utc_now(), "payload": payload}
        with self._lock:
            self._append_jsonl(contract.task_id, "events", event)
            try:
                with self.connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    now = utc_now()
                    connection.execute(
                        "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (contract.task_id, contract.mode.value, contract.objective, contract.workspace["path"],
                         contract.workspace["revision"], contract.state.value, contract.contract_version,
                         contract.protocol_version, contract.plan_id, contract.created_at, now),
                    )
                    connection.execute(
                        "INSERT INTO task_contracts VALUES (?, ?, ?, ?)",
                        (contract.task_id, contract.contract_version, _json(payload), now),
                    )
                    connection.execute(
                        "INSERT INTO current_state VALUES (?, ?, ?)",
                        (contract.task_id, contract.state.value, now),
                    )
                    connection.execute(
                        "INSERT INTO events(task_id,event_type,payload_json,created_at) VALUES(?,?,?,?)",
                        (contract.task_id, "task_created", _json(event), now),
                    )
                    connection.commit()
            except sqlite3.IntegrityError as exc:
                raise ConflictError("TASK_EXISTS", f"Task {contract.task_id!r} already exists.") from exc
            except sqlite3.Error as exc:
                raise StorageError("Unable to create task.", {"reason": str(exc)}) from exc

    def task_row(self, task_id: str) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise NotFoundError("task", task_id)
        return row

    def snapshot(self, task_id: str) -> dict[str, Any]:
        row = self.task_row(task_id)
        with self.connect() as connection:
            contract_row = connection.execute(
                "SELECT payload_json FROM task_contracts WHERE task_id=? ORDER BY contract_version DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            todo = [json.loads(item["payload_json"]) for item in connection.execute(
                "SELECT payload_json FROM todo_items WHERE task_id=? ORDER BY item_id", (task_id,)
            )]
            criteria = [json.loads(item["payload_json"]) for item in connection.execute(
                "SELECT payload_json FROM acceptance_criteria WHERE task_id=? ORDER BY criterion_id", (task_id,)
            )]
            evidence = [json.loads(item["payload_json"]) for item in connection.execute(
                "SELECT payload_json FROM evidence_index WHERE task_id=? ORDER BY created_at,evidence_id", (task_id,)
            )]
            gate = connection.execute(
                "SELECT payload_json FROM gate_results WHERE task_id=? ORDER BY id DESC LIMIT 1", (task_id,)
            ).fetchone()
        contract = json.loads(contract_row["payload_json"]) if contract_row else {}
        contract.update({
            "state": row["state"],
            "workspace": {"path": row["workspace_path"], "revision": row["workspace_revision"]},
            "contract_version": row["contract_version"],
            "plan_id": row["plan_id"],
            "todo": todo,
            "acceptance_criteria": criteria,
        })
        return {
            "task": contract,
            "evidence": evidence,
            "latest_gate": json.loads(gate["payload_json"]) if gate else None,
        }

    def replace_plan(self, contract: TaskContract, event_payload: dict[str, Any]) -> None:
        payload = contract.to_dict()
        event = {"event_type": "plan_committed", "task_id": contract.task_id, "created_at": utc_now(), "payload": event_payload}
        with self._lock:
            self._append_jsonl(contract.task_id, "events", event)
            try:
                with self.connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    now = utc_now()
                    connection.execute(
                        "UPDATE tasks SET objective=?,state=?,contract_version=?,plan_id=?,updated_at=? WHERE task_id=?",
                        (contract.objective, contract.state.value, contract.contract_version, contract.plan_id, now, contract.task_id),
                    )
                    connection.execute("DELETE FROM todo_items WHERE task_id=?", (contract.task_id,))
                    connection.execute("DELETE FROM acceptance_criteria WHERE task_id=?", (contract.task_id,))
                    connection.executemany(
                        "INSERT INTO todo_items VALUES(?,?,?)",
                        [(contract.task_id, item.id, _json(item.to_dict())) for item in contract.todo],
                    )
                    connection.executemany(
                        "INSERT INTO acceptance_criteria VALUES(?,?,?)",
                        [(contract.task_id, item.id, _json(item.to_dict())) for item in contract.acceptance_criteria],
                    )
                    connection.execute(
                        "INSERT INTO task_contracts VALUES(?,?,?,?)",
                        (contract.task_id, contract.contract_version, _json(payload), now),
                    )
                    connection.execute(
                        "UPDATE current_state SET state=?,updated_at=? WHERE task_id=?",
                        (contract.state.value, now, contract.task_id),
                    )
                    connection.execute(
                        "INSERT INTO events(task_id,event_type,payload_json,created_at) VALUES(?,?,?,?)",
                        (contract.task_id, "plan_committed", _json(event), now),
                    )
                    connection.commit()
            except sqlite3.Error as exc:
                raise StorageError("Unable to commit plan.", {"reason": str(exc)}) from exc

    def append_event(self, task_id: str, event_type: str, payload: dict[str, Any], *, new_state: TaskState | None = None,
                     workspace_revision: str | None = None) -> None:
        self.task_row(task_id)
        todo_update = _todo_status_update(event_type, payload)
        event = {"event_type": event_type, "task_id": task_id, "created_at": utc_now(), "payload": payload}
        with self._lock:
            self._append_jsonl(task_id, "events", event)
            try:
                with self.connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    now = utc_now()
                    connection.execute(
                        "INSERT INTO events(task_id,event_type,payload_json,created_at) VALUES(?,?,?,?)",
                        (task_id, event_type, _json(event), now),
                    )
                    if todo_update is not None:
                        todo_id, todo_status = todo_update
                        row = connection.execute(
                            "SELECT payload_json FROM todo_items WHERE task_id=? AND item_id=?",
                            (task_id, todo_id),
                        ).fetchone()
                        if row is None:
                            raise NotFoundError("todo", todo_id)
                        todo_payload = json.loads(row["payload_json"])
                        todo_payload["status"] = todo_status
                        connection.execute(
                            "UPDATE todo_items SET payload_json=? WHERE task_id=? AND item_id=?",
                            (_json(todo_payload), task_id, todo_id),
                        )
                    if new_state is not None:
                        connection.execute("UPDATE tasks SET state=?,updated_at=? WHERE task_id=?", (new_state.value, now, task_id))
                        connection.execute("UPDATE current_state SET state=?,updated_at=? WHERE task_id=?", (new_state.value, now, task_id))
                    if workspace_revision is not None:
                        connection.execute(
                            "UPDATE tasks SET workspace_revision=?,updated_at=? WHERE task_id=?",
                            (workspace_revision, now, task_id),
                        )
                        rows = connection.execute(
                            "SELECT evidence_id,payload_json FROM evidence_index WHERE task_id=? AND workspace_revision<>? AND status='verified'",
                            (task_id, workspace_revision),
                        ).fetchall()
                        for old in rows:
                            item = json.loads(old["payload_json"])
                            item["status"] = "stale"
                            item["validation_message"] = "Workspace revision changed after verification."
                            connection.execute(
                                "UPDATE evidence_index SET status='stale',payload_json=? WHERE task_id=? AND evidence_id=?",
                                (_json(item), task_id, old["evidence_id"]),
                            )
                        criteria = connection.execute(
                            "SELECT criterion_id,payload_json FROM acceptance_criteria WHERE task_id=?",
                            (task_id,),
                        ).fetchall()
                        for row in criteria:
                            item = json.loads(row["payload_json"])
                            if item.get("status") == "PASS" and item.get("verified_revision") != workspace_revision:
                                item.update({
                                    "status": "NOT_RUN",
                                    "evidence_status": "pending",
                                    "verified_revision": None,
                                    "verified_at": None,
                                    "reason": "Workspace revision changed after verification.",
                                })
                                connection.execute(
                                    "UPDATE acceptance_criteria SET payload_json=? WHERE task_id=? AND criterion_id=?",
                                    (_json(item), task_id, row["criterion_id"]),
                                )
                    connection.commit()
            except sqlite3.Error as exc:
                raise StorageError("Unable to append task event.", {"reason": str(exc)}) from exc

    def insert_evidence(self, evidence: Evidence) -> None:
        payload = evidence.to_dict()
        envelope = {"event_type": "evidence_submitted", "created_at": utc_now(), "payload": payload}
        with self._lock:
            self._append_jsonl(evidence.task_id, "evidence", envelope)
            try:
                with self.connect() as connection:
                    connection.execute(
                        "INSERT INTO evidence_index VALUES(?,?,?,?,?,?,?)",
                        (evidence.task_id, evidence.evidence_id, evidence.criterion_id, evidence.status.value,
                         evidence.workspace_revision, _json(payload), evidence.produced_at),
                    )
                    connection.commit()
            except sqlite3.IntegrityError as exc:
                raise ConflictError("EVIDENCE_EXISTS", f"Evidence {evidence.evidence_id!r} already exists.") from exc
            except sqlite3.Error as exc:
                raise StorageError("Unable to save evidence.", {"reason": str(exc)}) from exc

    def update_evidence(self, evidence: Evidence) -> None:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE evidence_index SET status=?,workspace_revision=?,payload_json=? WHERE task_id=? AND evidence_id=?",
                (evidence.status.value, evidence.workspace_revision, _json(evidence.to_dict()), evidence.task_id, evidence.evidence_id),
            )
            connection.commit()
        if cursor.rowcount != 1:
            raise NotFoundError("evidence", evidence.evidence_id)

    def update_criteria_and_gate(self, task_id: str, criteria: list[dict[str, Any]], result: GateResult) -> None:
        event = {"event_type": "gate_evaluated", "task_id": task_id, "created_at": utc_now(), "payload": result.to_dict()}
        with self._lock:
            self._append_jsonl(task_id, "events", event)
            try:
                with self.connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    for item in criteria:
                        connection.execute(
                            "UPDATE acceptance_criteria SET payload_json=? WHERE task_id=? AND criterion_id=?",
                            (_json(item), task_id, item["id"]),
                        )
                    now = utc_now()
                    connection.execute(
                        "INSERT INTO gate_results(task_id,passed,payload_json,created_at) VALUES(?,?,?,?)",
                        (task_id, int(result.passed), _json(result.to_dict()), now),
                    )
                    connection.execute("UPDATE tasks SET state=?,updated_at=? WHERE task_id=?", (result.state.value, now, task_id))
                    connection.execute("UPDATE current_state SET state=?,updated_at=? WHERE task_id=?", (result.state.value, now, task_id))
                    connection.execute(
                        "INSERT INTO events(task_id,event_type,payload_json,created_at) VALUES(?,?,?,?)",
                        (task_id, "gate_evaluated", _json(event), now),
                    )
                    connection.commit()
            except sqlite3.Error as exc:
                raise StorageError("Unable to save Gate result.", {"reason": str(exc)}) from exc

    def record_subagent_call(self, task_id: str, call_id: str, kind: str, status: str,
                             payload: dict[str, Any], result_hash: str | None = None) -> None:
        envelope = {"call_id": call_id, "task_id": task_id, "kind": kind, "status": status,
                    "result_hash": result_hash, "created_at": utc_now(), "payload": payload}
        with self._lock:
            self._append_jsonl(task_id, "subagent", envelope)
            try:
                with self.connect() as connection:
                    connection.execute(
                        "INSERT OR REPLACE INTO subagent_calls VALUES(?,?,?,?,?,?,?)",
                        (call_id, task_id, kind, status, result_hash, _json(payload), envelope["created_at"]),
                    )
                    connection.commit()
            except sqlite3.Error as exc:
                raise StorageError("Unable to save subagent call.", {"reason": str(exc)}) from exc
