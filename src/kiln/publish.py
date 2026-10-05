"""Goal branch, and the pull request opened when a goal is finished."""

import os
import sqlite3
import subprocess

from kiln.config import Config
from kiln.errors import KilnError
from kiln.git import (
    branch_exists,
    commits_ahead,
    ensure_branch,
    goal_branch_name,
    push_branch,
    remote_url,
)
from kiln.models import Goal, GoalStatus, TaskStatus
from kiln.tasks import list_tasks, require_goal, set_goal_branch, set_goal_pr, set_goal_status

_OPEN = (TaskStatus.pending, TaskStatus.claimed, TaskStatus.running, TaskStatus.review)


def ensure_goal_branch(conn: sqlite3.Connection, config: Config, goal: Goal) -> tuple[str, bool]:
    """Return the goal's integration branch, creating it from the base branch when needed."""
    name = goal.branch or goal_branch_name(goal.id, goal.title)
    if goal.branch and not branch_exists(config.repo_root, name):
        raise KilnError(f"goal #{goal.id} branch {name} does not exist")
    created = not branch_exists(config.repo_root, name)
    if created:
        ensure_branch(config.repo_root, name, config.base_branch)
    if goal.branch != name:
        set_goal_branch(conn, goal.id, name)
    return name, created


def publish_ready_goals(
    conn: sqlite3.Connection,
    config: Config,
    *,
    gh_bin: str = "gh",
    remote: str = "origin",
) -> list[str]:
    """Open one pull request for each goal whose tasks are all finished."""
    lines: list[str] = []
    for goal in _goals_ready_to_publish(conn):
        lines.append(_publish_goal(conn, config, goal, gh_bin=gh_bin, remote=remote))
    return lines


def _goals_ready_to_publish(conn: sqlite3.Connection) -> list[Goal]:
    ready: list[Goal] = []
    for goal in list_goals_all(conn):
        if goal.pr_url is not None:
            continue
        tasks = list_tasks(conn, goal_id=goal.id)
        if any(task.status in _OPEN for task in tasks):
            continue
        if not tasks and goal.status != GoalStatus.done:
            continue
        ready.append(goal)
    return ready


def list_goals_all(conn: sqlite3.Connection):
    from kiln.tasks import list_goals

    return list_goals(conn)


def _publish_goal(
    conn: sqlite3.Connection,
    config: Config,
    goal: Goal,
    *,
    gh_bin: str,
    remote: str,
) -> str:
    current = require_goal(conn, goal.id)
    branch = current.branch
    if branch is None or commits_ahead(config.repo_root, config.base_branch, branch) == 0:
        _mark_published(conn, current, "")
        name = branch or "(none)"
        return f"goal #{current.id} finished with no changes on {name}"
    if remote_url(config.repo_root, remote) is None:
        raise KilnError(
            f"no {remote} remote; kiln pushes {branch} there to open a pull request"
        )
    push_branch(config.repo_root, branch, remote)
    url = create_pull_request(
        config.repo_root,
        base=config.base_branch,
        head=branch,
        title=current.title,
        body=_pull_request_body(conn, current),
        gh_bin=gh_bin,
    )
    _mark_published(conn, current, url)
    return f"opened {url}"


def create_pull_request(
    repo,
    *,
    base: str,
    head: str,
    title: str,
    body: str,
    gh_bin: str = "gh",
) -> str:
    created = _gh(
        repo,
        gh_bin,
        "pr",
        "create",
        "--base",
        base,
        "--head",
        head,
        "--title",
        title,
        "--body",
        body,
    )
    if created.returncode == 0:
        url = _last_line(created.stdout)
        if url:
            return url
        raise KilnError("gh pr create returned no url")
    existing = _gh(repo, gh_bin, "pr", "view", head, "--json", "url", "--jq", ".url")
    if existing.returncode == 0:
        url = _last_line(existing.stdout)
        if url:
            return url
    detail = (created.stderr or created.stdout).strip()
    raise KilnError(detail or "gh pr create failed")


def _mark_published(conn: sqlite3.Connection, goal: Goal, url: str) -> None:
    if goal.status != GoalStatus.done:
        set_goal_status(conn, goal.id, GoalStatus.done)
    set_goal_pr(conn, goal.id, url)


def _pull_request_body(conn: sqlite3.Connection, goal: Goal) -> str:
    lines = [
        goal.description.strip() or goal.title,
        "",
        "Brief:",
        goal.brief.strip() if goal.brief else "(none)",
        "",
        "Evidence:",
    ]
    if goal.evidence:
        lines.extend(f"- {item}" for item in goal.evidence)
    else:
        lines.append("(none)")
    lines.extend(["", "Tasks:"])
    for task in list_tasks(conn, goal_id=goal.id):
        lines.append(f"- #{task.id} [{task.status.value}] {task.title}")
    return "\n".join(lines).strip() + "\n"


def _gh(repo, gh_bin: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        return subprocess.run(
            [gh_bin, *args],
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            check=False,
        )
    except FileNotFoundError as exc:
        raise KilnError(f"could not run {gh_bin}") from exc


def _last_line(text: str) -> str:
    for line in reversed(text.splitlines()):
        if line.strip():
            return line.strip()
    return ""
