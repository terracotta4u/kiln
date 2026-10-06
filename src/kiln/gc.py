"""Remove worktrees and merged branches that finished jobs no longer need."""

import shutil
import sqlite3
from pathlib import Path

from kiln.config import Config
from kiln.db import record_event
from kiln.errors import KilnError
from kiln.git import branch_exists, delete_branch, prune_worktrees, remove_worktree
from kiln.jobs import forget_checkout, is_open, list_jobs
from kiln.models import Integration, Job


def cleanup(conn: sqlite3.Connection, config: Config, *, dry_run: bool = False) -> list[str]:
    """Drop finished checkouts. Live jobs keep their worktree and branch.

    A merged job's branch is deleted only when delete_merged_branches is set.
    Rejected, failed, and cancelled jobs keep their branch so the work can still be inspected.
    """
    jobs = list_jobs(conn)
    lines: list[str] = []
    for job in jobs:
        if is_open(job):
            continue
        lines.extend(_release_job(conn, config, job, dry_run=dry_run))
    lines.extend(_sweep_directories(config, jobs, dry_run=dry_run))
    if not dry_run:
        pruned = prune_worktrees(config.repo_root)
        if pruned:
            lines.extend(pruned.splitlines())
        if lines:
            record_event(conn, "gc", "; ".join(lines))
    return lines


def _release_job(conn: sqlite3.Connection, config: Config, job: Job, *, dry_run: bool) -> list[str]:
    lines: list[str] = []
    if job.worktree_path:
        lines.extend(_release_worktree(conn, config, job, dry_run=dry_run))
    if job.integration == Integration.merged and config.delete_merged_branches and job.branch:
        lines.extend(_release_branch(conn, config, job, dry_run=dry_run))
    return lines


def _release_worktree(conn: sqlite3.Connection, config: Config, job: Job, *, dry_run: bool) -> list[str]:
    path = Path(job.worktree_path or "")
    if not _inside(config.worktrees_dir, path):
        return [f"left worktree for job #{job.id} in place ({path})"]
    if not path.exists():
        if not dry_run:
            forget_checkout(conn, job.id, worktree=True)
        return [f"{'would clear' if dry_run else 'cleared'} worktree path for job #{job.id}"]
    if dry_run:
        return [f"would remove worktree for job #{job.id}"]
    try:
        _drop_path(config.repo_root, path)
    except OSError as exc:
        return [f"failed to remove worktree for job #{job.id}: {exc}"]
    forget_checkout(conn, job.id, worktree=True)
    return [f"removed worktree for job #{job.id}"]


def _release_branch(conn: sqlite3.Connection, config: Config, job: Job, *, dry_run: bool) -> list[str]:
    branch = job.branch or ""
    if not branch.startswith("kiln/") or branch == config.base_branch:
        return [f"left branch {branch} for job #{job.id} in place"]
    if not branch_exists(config.repo_root, branch):
        if not dry_run:
            forget_checkout(conn, job.id, branch=True)
        return [f"{'would clear' if dry_run else 'cleared'} branch {branch} for job #{job.id}"]
    if dry_run:
        return [f"would delete branch {branch} for job #{job.id}"]
    try:
        delete_branch(config.repo_root, branch)
    except KilnError as exc:
        return [f"failed to delete branch {branch} for job #{job.id}: {exc}"]
    forget_checkout(conn, job.id, branch=True)
    return [f"deleted branch {branch} for job #{job.id}"]


def _sweep_directories(config: Config, jobs: list[Job], *, dry_run: bool) -> list[str]:
    root = config.worktrees_dir
    if not root.is_dir():
        return []
    live_ids = {job.id for job in jobs if is_open(job)}
    live_paths = {
        Path(job.worktree_path).resolve()
        for job in jobs
        if is_open(job) and job.worktree_path
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

