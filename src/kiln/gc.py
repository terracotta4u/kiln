"""Remove worktrees and merged branches that finished tasks no longer need."""

import shutil
import sqlite3
from pathlib import Path

from kiln.config import Config
from kiln.db import record_event
from kiln.errors import KilnError
from kiln.git import branch_exists, delete_branch, prune_worktrees, remove_worktree
from kiln.models import Task, TaskStatus
from kiln.tasks import forget_checkout, list_tasks

_FINISHED = (TaskStatus.done, TaskStatus.failed, TaskStatus.cancelled)
_LIVE = (TaskStatus.pending, TaskStatus.claimed, TaskStatus.running, TaskStatus.review)


def cleanup(conn: sqlite3.Connection, config: Config, *, dry_run: bool = False) -> list[str]:
    """Drop finished checkouts. Live tasks keep their worktree and branch.

    A done task's branch is deleted only when delete_merged_branches is set.
    Failed and cancelled tasks keep their branch so the work can still be inspected.
    """
    tasks = list_tasks(conn)
    lines: list[str] = []
    for task in tasks:
        if task.status not in _FINISHED:
            continue
        lines.extend(_release_task(conn, config, task, dry_run=dry_run))
    lines.extend(_sweep_directories(config, tasks, dry_run=dry_run))
    if not dry_run:
        pruned = prune_worktrees(config.repo_root)
        if pruned:
            lines.extend(pruned.splitlines())
        if lines:
            record_event(conn, "gc", "; ".join(lines))
    return lines


def _release_task(conn: sqlite3.Connection, config: Config, task: Task, *, dry_run: bool) -> list[str]:
    lines: list[str] = []
    if task.worktree_path:
        lines.extend(_release_worktree(conn, config, task, dry_run=dry_run))
    if task.status == TaskStatus.done and config.delete_merged_branches and task.branch:
        lines.extend(_release_branch(conn, config, task, dry_run=dry_run))
    return lines


def _release_worktree(conn: sqlite3.Connection, config: Config, task: Task, *, dry_run: bool) -> list[str]:
    path = Path(task.worktree_path or "")
    if not _inside(config.worktrees_dir, path):
        return [f"left worktree for task #{task.id} in place ({path})"]
    if not path.exists():
        if not dry_run:
            forget_checkout(conn, task.id, worktree=True)
        return [f"{'would clear' if dry_run else 'cleared'} worktree path for task #{task.id}"]
    if dry_run:
        return [f"would remove worktree for task #{task.id}"]
    try:
        _drop_path(config.repo_root, path)
    except OSError as exc:
        return [f"failed to remove worktree for task #{task.id}: {exc}"]
    forget_checkout(conn, task.id, worktree=True)
    return [f"removed worktree for task #{task.id}"]


def _release_branch(conn: sqlite3.Connection, config: Config, task: Task, *, dry_run: bool) -> list[str]:
    branch = task.branch or ""
    if not branch.startswith("kiln/") or branch == config.base_branch:
        return [f"left branch {branch} for task #{task.id} in place"]
    if not branch_exists(config.repo_root, branch):
        if not dry_run:
            forget_checkout(conn, task.id, branch=True)
        return [f"{'would clear' if dry_run else 'cleared'} branch {branch} for task #{task.id}"]
    if dry_run:
        return [f"would delete branch {branch} for task #{task.id}"]
    try:
        delete_branch(config.repo_root, branch)
    except KilnError as exc:
        return [f"failed to delete branch {branch} for task #{task.id}: {exc}"]
    forget_checkout(conn, task.id, branch=True)
    return [f"deleted branch {branch} for task #{task.id}"]


def _sweep_directories(config: Config, tasks: list[Task], *, dry_run: bool) -> list[str]:
    root = config.worktrees_dir
    if not root.is_dir():
        return []
    live_ids = {task.id for task in tasks if task.status in _LIVE}
    live_paths = {
        Path(task.worktree_path).resolve()
        for task in tasks
        if task.status in _LIVE and task.worktree_path
    }
    lines: list[str] = []
    for path in sorted(root.iterdir()):
        if not path.is_dir():
            continue
        if path.resolve() in live_paths:
            continue
        if path.name.isdigit() and int(path.name) in live_ids:
            continue
        if dry_run:
            lines.append(f"would remove leftover {path.name}")
            continue
        try:
            _drop_path(config.repo_root, path)
        except OSError as exc:
            lines.append(f"failed to remove leftover {path.name}: {exc}")
            continue
        lines.append(f"removed leftover {path.name}")
    return lines


def _drop_path(repo: Path, path: Path) -> None:
    if (path / ".git").exists():
        try:
            remove_worktree(repo, path)
            return
        except KilnError:
            pass
    shutil.rmtree(path)


def _inside(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True

