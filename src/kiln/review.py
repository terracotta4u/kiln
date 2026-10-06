import sqlite3
from pathlib import Path

from kiln.config import Config
from kiln.errors import KilnError
from kiln.git import delete_branch, merge_branch, remove_worktree
from kiln.jobs import (
    reject_job as record_rejection,
    require_goal,
    require_job,
    rework_job,
    set_integration,
)
from kiln.models import Integration, JobRole, JobStatus
from kiln.publish import ensure_goal_branch


def approve_job(conn: sqlite3.Connection, config: Config, job_id: int) -> str:
    """Merge a completed worker into the goal branch. A conflict sends it back for rework."""
    job = _require_review(conn, job_id)
    if not job.branch:
        raise KilnError(f"job #{job_id} has no branch to merge")
    integration, _ = ensure_goal_branch(conn, config, require_goal(conn, job.goal_id))
    _remove_worktree(config, job.worktree_path)
    result = merge_branch(config.repo_root, job.branch, integration)
    if not result.merged:
        rework_job(
            conn,
            job_id,
            f"Merge into {integration} conflicted.\n{result.message}",
        )
        return f"job #{job_id} conflicted and was sent back for rework"
    set_integration(conn, job_id, Integration.merged)
    if config.delete_merged_branches:
        try:
            delete_branch(config.repo_root, job.branch)
        except KilnError as exc:
            return f"job #{job_id} merged into {integration}; branch not deleted: {exc}"
    return f"job #{job_id} merged into {integration}"


def send_back(conn: sqlite3.Connection, job_id: int, feedback: str) -> str:
    rework_job(conn, job_id, feedback)
    return f"job #{job_id} sent back for rework"


def reject_job(conn: sqlite3.Connection, job_id: int, reason: str) -> str:
    """Reject a completed worker. Execution stays completed and the result stays."""
    record_rejection(conn, job_id, reason.strip() or "rejected")
    return f"job #{job_id} rejected"


def _require_review(conn: sqlite3.Connection, job_id: int):
    job = require_job(conn, job_id)
    if not (
        job.role == JobRole.worker
        and job.status == JobStatus.completed
        and job.integration == Integration.pending
    ):
        raise KilnError(
            f"job #{job_id} is {job.status.value}; only a completed worker with pending integration can be reviewed"
        )
    return job


def _remove_worktree(config: Config, worktree_path: str | None) -> None:
    if not worktree_path:
        return
    path = Path(worktree_path)
    if not path.exists():
        return
    remove_worktree(config.repo_root, path)
