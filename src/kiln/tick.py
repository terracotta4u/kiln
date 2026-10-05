import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field

from kiln.config import Config
from kiln.errors import KilnError
from kiln.gc import cleanup
from kiln.git import branch_exists, goal_branch_name
from kiln.models import Goal, GoalStatus, TaskStatus
from kiln.publish import ensure_goal_branch, publish_ready_goals
from kiln.roles.foreman import apply_actions, goal_brief, run_foreman
from kiln.tasks import list_goals, list_tasks


@dataclass
class TickResult:
    lines: list[str] = field(default_factory=list)


def run_tick(
    conn: sqlite3.Connection,
    config: Config,
    *,
    workers: int | None = None,
    dispatch: bool = True,
    dry_run: bool = False,
    agent_bin: str | None = None,
    reporter: Callable[[str], None] | None = None,
) -> TickResult:
    """One factory cycle. The foreman decides what runs."""
    result = TickResult()
    goals = list_goals(conn, status=GoalStatus.active)
    if not goals:
        result.lines.append("no active goals")
        return result
    limit = config.max_parallel_workers if workers is None else workers
    if limit < 1:
        raise KilnError("workers must be >= 1")

    if dry_run:
        result.lines.extend(cleanup(conn, config, dry_run=True))
        for goal in goals:
            result.lines.append(_dry_branch_line(config, goal))
        for goal in goals:
            result.lines.append(goal_brief(conn, config, goal))
        return result

    result.lines.extend(cleanup(conn, config))
    for goal in goals:
        _branch, created = ensure_goal_branch(conn, config, goal)
        if created:
            result.lines.append(f"goal #{goal.id} branch {_branch}")
    for goal in list_goals(conn, status=GoalStatus.active):
        _foreman_goal(
            conn,
            config,
            goal,
            result,
            worker_limit=limit,
            allow_dispatch=dispatch,
            agent_bin=agent_bin,
            reporter=reporter,
        )
    return result


def run_until_done(
    conn: sqlite3.Connection,
    config: Config,
    *,
    workers: int | None = None,
    agent_bin: str | None = None,
    gh_bin: str = "gh",
    reporter: Callable[[str], None] | None = None,
) -> TickResult:
    """Tick until every active goal is finished, then open a pull request for each."""
    result = TickResult()
    while True:
        if not list_goals(conn, status=GoalStatus.active):
            if not result.lines:
                _record(result, ["no active goals"], reporter)
            break
        before = _snapshot(conn)
        tick = run_tick(
            conn,
            config,
            workers=workers,
            agent_bin=agent_bin,
            reporter=reporter,
        )
        _record(result, tick.lines, reporter)
        _record(result, publish_ready_goals(conn, config, gh_bin=gh_bin), reporter)
        if not list_goals(conn, status=GoalStatus.active):
            break
        if _snapshot(conn) == before:
            _record(result, [_stuck_message(conn)], reporter)
            break
    return result


def _record(result: TickResult, lines: list[str], reporter: Callable[[str], None] | None) -> None:
    result.lines.extend(lines)
    if reporter is None:
        return
    for line in lines:
        reporter(line)


def _snapshot(conn: sqlite3.Connection) -> tuple:
    goals = tuple((goal.id, goal.status.value, goal.branch, goal.pr_url) for goal in list_goals(conn))
    tasks = tuple((task.id, task.status.value, task.attempts) for task in list_tasks(conn))
    notes = conn.execute("SELECT COUNT(*) AS n FROM notes").fetchone()["n"]
    return (goals, tasks, notes)


def _stuck_message(conn: sqlite3.Connection) -> str:
    open_tasks = [
        task
        for task in list_tasks(conn)
        if task.status in (TaskStatus.pending, TaskStatus.claimed, TaskStatus.running, TaskStatus.review)
    ]
    if not open_tasks:
        return "stopped: a tick made no progress"
    detail = ", ".join(f"#{task.id} {task.status.value}" for task in open_tasks)
    return f"stopped: a tick made no progress ({detail})"


def _dry_branch_line(config: Config, goal: Goal) -> str:
    name = goal.branch or goal_branch_name(goal.id, goal.title)
    if goal.branch and branch_exists(config.repo_root, name):
        return f"goal #{goal.id} on {name}"
    return f"would create branch {name}"


def _foreman_goal(
    conn: sqlite3.Connection,
    config: Config,
    goal: Goal,
    result: TickResult,
    *,
    worker_limit: int,
    allow_dispatch: bool,
    agent_bin: str | None,
    reporter: Callable[[str], None] | None,
) -> None:
    outcome = run_foreman(
        conn,
        config,
        goal,
        agent_bin=agent_bin,
        reporter=reporter,
        worker_limit=worker_limit,
    )
    if outcome.failure:
        result.lines.append(f"foreman failed for goal #{goal.id}: {outcome.failure}")
        return
    if not outcome.actions:
        result.lines.append(f"goal #{goal.id}: no actions")
        return
    result.lines.extend(
        apply_actions(
            conn,
            config,
            goal,
            outcome.actions,
            agent_bin=agent_bin,
            reporter=reporter,
            worker_limit=worker_limit,
            allow_dispatch=allow_dispatch,
        )
    )
