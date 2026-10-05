import json
import sqlite3
from dataclasses import dataclass
from enum import StrEnum

from kiln.errors import KilnError


class GoalStatus(StrEnum):
    active = "active"
    done = "done"


class TaskStatus(StrEnum):
    pending = "pending"
    claimed = "claimed"
    running = "running"
    review = "review"
    done = "done"
    failed = "failed"
    cancelled = "cancelled"


class RunStatus(StrEnum):
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


@dataclass(frozen=True)
class Goal:
    id: int
    title: str
    description: str
    status: GoalStatus
    created_at: str
    branch: str | None = None
    pr_url: str | None = None
    brief: str | None = None
    evidence: tuple[str, ...] | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Goal":
        return cls(
            id=row["id"],
            title=row["title"],
            description=row["description"],
            status=GoalStatus(row["status"]),
            created_at=row["created_at"],
            branch=row["branch"],
            pr_url=row["pr_url"],
            brief=row["brief"],
            evidence=_evidence(row["evidence"]),
        )


@dataclass(frozen=True)
class Task:
    id: int
    goal_id: int
    title: str
    description: str
    acceptance: str
    status: TaskStatus
    priority: int
    attempts: int
    max_attempts: int
    branch: str | None
    worktree_path: str | None
    claimed_by: str | None
    feedback: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Task":
        return cls(
            id=row["id"],
            goal_id=row["goal_id"],
            title=row["title"],
            description=row["description"],
            acceptance=row["acceptance"],
            status=TaskStatus(row["status"]),
            priority=row["priority"],
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            branch=row["branch"],
            worktree_path=row["worktree_path"],
            claimed_by=row["claimed_by"],
            feedback=row["feedback"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


def _evidence(value: str | None) -> tuple[str, ...] | None:
    if value is None or value == "":
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise KilnError("goal evidence is not a list of strings") from exc
    if not isinstance(parsed, list) or any(not isinstance(item, str) for item in parsed):
        raise KilnError("goal evidence is not a list of strings")
    return tuple(parsed)


@dataclass(frozen=True)
class Event:
    id: int
    ts: str
    kind: str
    task_id: int | None
    run_id: int | None
    message: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Event":
        return cls(
            id=row["id"],
            ts=row["ts"],
            kind=row["kind"],
            task_id=row["task_id"],
            run_id=row["run_id"],
            message=row["message"],
        )


@dataclass(frozen=True)
class Note:
    id: int
    goal_id: int
    text: str
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Note":
        return cls(
            id=row["id"],
            goal_id=row["goal_id"],
            text=row["text"],
            created_at=row["created_at"],
        )


@dataclass(frozen=True)
class Run:
    id: int
    task_id: int | None
    role: str
    model: str
    status: RunStatus
    exit_code: int | None
    started_at: str
    finished_at: str | None
    log_path: str | None
    report_json: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Run":
        return cls(
            id=row["id"],
            task_id=row["task_id"],
            role=row["role"],
            model=row["model"],
            status=RunStatus(row["status"]),
            exit_code=row["exit_code"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            log_path=row["log_path"],
            report_json=row["report_json"],
        )
