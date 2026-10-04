import sqlite3
from pathlib import Path

from kiln.config import Config
from kiln.errors import KilnError
from kiln.git import delete_branch, merge_branch, remove_worktree
from kiln.models import TaskStatus
from kiln.tasks import fail_task, require_task, rework_task, set_task_status


def approve_task(conn: sqlite3.Connection, config: Config, task_id: int) -> str:
    """Merge a reviewed task into the base branch. A conflict sends it back for rework."""
    task = _require_review(conn, task_id)
    if not task.branch:
        raise KilnError(f"task #{task_id} has no branch to merge")
    _remove_worktree(config, task.worktree_path)
    result = merge_branch(config.repo_root, task.branch, config.base_branch)
    if not result.merged:
        rework_task(
            conn,
            task_id,
            f"Merge into {config.base_branch} conflicted.\n{result.message}",
        )
        return f"task #{task_id} conflicted and was sent back for rework"
    set_task_status(conn, task_id, TaskStatus.done)
    if config.delete_merged_branches:
        try:
            delete_branch(config.repo_root, task.branch)
        except KilnError as exc:
            return f"task #{task_id} merged into {config.base_branch}; branch not deleted: {exc}"
    return f"task #{task_id} merged into {config.base_branch}"


def send_back(conn: sqlite3.Connection, task_id: int, feedback: str) -> str:
    task = rework_task(conn, task_id, feedback)
    if task.status == TaskStatus.failed:
        return f"task #{task_id} exhausted its attempts and failed"
    return f"task #{task_id} sent back for rework"


def reject_task(conn: sqlite3.Connection, task_id: int, reason: str) -> str:
    _require_review(conn, task_id)
    fail_task(conn, task_id, reason.strip() or "failed")
    return f"task #{task_id} failed"


def _require_review(conn: sqlite3.Connection, task_id: int):
    task = require_task(conn, task_id)
    if task.status != TaskStatus.review:
        raise KilnError(f"task #{task_id} is {task.status.value}; only a task in review can be reviewed")
    return task


def _remove_worktree(config: Config, worktree_path: str | None) -> None:
    if not worktree_path:
        return
    path = Path(worktree_path)
    if not path.exists():
        return
    remove_worktree(config.repo_root, path)
