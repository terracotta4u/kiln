import sqlite3
from pathlib import Path

from kiln.config import Config
from kiln.errors import KilnError
from kiln.git import delete_branch, merge_branch, remove_worktree
from kiln.jobs import fail_job, require_goal, require_job, rework_job, set_integration
from kiln.models import Integration, JobRole, JobStatus
from kiln.publish import ensure_goal_branch


def approve_task(conn: sqlite3.Connection, config: Config, task_id: int) -> str:
    """Merge a reviewed task into the goal branch. A conflict sends it back for rework."""
    task = _require_review(conn, task_id)
    if not task.branch:
        raise KilnError(f"task #{task_id} has no branch to merge")
    integration, _ = ensure_goal_branch(conn, config, require_goal(conn, task.goal_id))
    _remove_worktree(config, task.worktree_path)
    result = merge_branch(config.repo_root, task.branch, integration)
    if not result.merged:
        rework_job(
            conn,
            task_id,
            f"Merge into {integration} conflicted.\n{result.message}",
        )
        return f"task #{task_id} conflicted and was sent back for rework"
    set_integration(conn, task_id, Integration.merged)
    if config.delete_merged_branches:
        try:
            delete_branch(config.repo_root, task.branch)
        except KilnError as exc:
            return f"task #{task_id} merged into {integration}; branch not deleted: {exc}"
    return f"task #{task_id} merged into {integration}"


def send_back(conn: sqlite3.Connection, task_id: int, feedback: str) -> str:
    rework_job(conn, task_id, feedback)
    return f"task #{task_id} sent back for rework"


def reject_task(conn: sqlite3.Connection, task_id: int, reason: str) -> str:
    """Foreman fail still marks execution failed. Integration reject arrives with the action rename."""
    _require_review(conn, task_id)
    fail_job(conn, task_id, reason.strip() or "failed")
    return f"task #{task_id} failed"


def _require_review(conn: sqlite3.Connection, task_id: int):
    task = require_job(conn, task_id)
    if not (
        task.role == JobRole.worker
        and task.status == JobStatus.completed
        and task.integration == Integration.pending
    ):
        raise KilnError(
            f"task #{task_id} is {task.status.value}; only a completed worker with pending integration can be reviewed"
        )
    return task


def _remove_worktree(config: Config, worktree_path: str | None) -> None:
    if not worktree_path:
        return
    path = Path(worktree_path)
    if not path.exists():
        return
    remove_worktree(config.repo_root, path)
