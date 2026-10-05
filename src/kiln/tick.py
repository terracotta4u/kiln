import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from kiln.config import Config
from kiln.db import connect, migrate, record_event
from kiln.errors import KilnError
from kiln.gc import cleanup
from kiln.git import branch_exists, goal_branch_name
from kiln.models import Goal, GoalStatus, TaskStatus
from kiln.notes import list_notes
from kiln.publish import ensure_goal_branch, publish_ready_goals
from kiln.queue import drop_scout, enqueue_scout, pending_scouts
from kiln.roles.foreman import apply_actions, goal_brief, run_foreman
from kiln.roles.scout import run_scout
from kiln.roles.worker import WorkerOutcome, run_worker
from kiln.tasks import list_goals, list_tasks, ready_tasks


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
    """One factory cycle: scouts, foreman, merges, then workers."""
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
            if _needs_auto_scout(conn, goal):
                result.lines.append(f"would scout goal #{goal.id}")
            queued = pending_scouts(conn, goal.id)
            for _, question in queued:
                result.lines.append(f"would run scout: {question}")
        ready = [task for task in ready_tasks(conn) if task.attempts < task.max_attempts]
        if dispatch:
            result.lines.append(f"would dispatch {min(limit, len(ready))} worker(s)")
        else:
            result.lines.append("dispatch skipped")
        return result

    result.lines.extend(cleanup(conn, config))
    for goal in goals:
        _branch, created = ensure_goal_branch(conn, config, goal)
        if created:
            result.lines.append(f"goal #{goal.id} branch {_branch}")
    for goal in goals:
        _scout_goal(conn, config, goal, result, agent_bin=agent_bin, reporter=reporter)
    for goal in list_goals(conn, status=GoalStatus.active):
        _foreman_goal(conn, config, goal, result, agent_bin=agent_bin, reporter=reporter)
    if not dispatch:
        result.lines.append("dispatch skipped")
        return result
    _dispatch(config, limit, result, agent_bin=agent_bin, reporter=reporter)
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
    scouts = conn.execute("SELECT COUNT(*) AS n FROM scout_requests").fetchone()["n"]
    return (goals, tasks, scouts)


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


def _needs_auto_scout(conn: sqlite3.Connection, goal: Goal) -> bool:
    if list_tasks(conn, goal_id=goal.id) or list_notes(conn, goal.id):
        return False
    row = conn.execute(
        "SELECT 1 FROM events WHERE kind = 'scout.auto' AND message = ? LIMIT 1",
        (f"goal #{goal.id}",),
    ).fetchone()
    return row is None


def _scout_goal(
    conn: sqlite3.Connection,
    config: Config,
    goal: Goal,
    result: TickResult,
    *,
    agent_bin: str | None,
    reporter: Callable[[str], None] | None,
) -> None:
    if _needs_auto_scout(conn, goal):
        question = f"Explore the repository and report what matters for this goal: {goal.title}"
        if goal.description:
            question = f"{question}\n{goal.description}"
        record_event(conn, "scout.auto", f"goal #{goal.id}")
        enqueue_scout(conn, goal.id, question)
    for request_id, question in pending_scouts(conn, goal.id):
        outcome = run_scout(
            conn,
            config,
            question,
            goal_id=goal.id,
            agent_bin=agent_bin,
            reporter=reporter,
        )
        drop_scout(conn, request_id)
        if outcome.note is None:
            result.lines.append(f"scout failed for goal #{goal.id}: {outcome.failure}")
        else:
            result.lines.append(f"scout note #{outcome.note.id} on goal #{goal.id}")


def _foreman_goal(
    conn: sqlite3.Connection,
    config: Config,
    goal: Goal,
    result: TickResult,
    *,
    agent_bin: str | None,
    reporter: Callable[[str], None] | None,
) -> None:
    outcome = run_foreman(conn, config, goal, agent_bin=agent_bin, reporter=reporter)
    if outcome.failure:
        result.lines.append(f"foreman failed for goal #{goal.id}: {outcome.failure}")
        return
    if not outcome.actions:
        result.lines.append(f"goal #{goal.id}: no actions")
        return
    result.lines.extend(apply_actions(conn, config, goal, outcome.actions))


def _dispatch(
    config: Config,
    limit: int,
    result: TickResult,
    *,
    agent_bin: str | None,
    reporter: Callable[[str], None] | None,
) -> None:
    peek = connect(config.db_path)
    try:
        migrate(peek)
        eligible = [task for task in ready_tasks(peek) if task.attempts < task.max_attempts]
    finally:
        peek.close()
    count = min(limit, len(eligible))
    if count == 0:
        result.lines.append("no ready tasks")
        return
    outcomes: list[WorkerOutcome] = []
    errors: list[str] = []
    lock = threading.Lock()

    def announce(message: str) -> None:
        if reporter is None:
            return
        with lock:
            reporter(message)

    def work() -> None:
        worker_conn = connect(config.db_path)
        try:
            migrate(worker_conn)
            outcome = run_worker(worker_conn, config, agent_bin=agent_bin, reporter=announce)
            with lock:
                outcomes.append(outcome)
        except Exception as exc:
            with lock:
                errors.append(str(exc))
        finally:
            worker_conn.close()

    if count == 1:
        work()
    else:
        threads = [threading.Thread(target=work) for _ in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    for outcome in outcomes:
        branch = outcome.task.branch or ""
        line = f"task #{outcome.task.id}  {outcome.task.status.value}  {branch}"
        if outcome.failure:
            line = f"{line}: {outcome.failure}"
        result.lines.append(line)
    result.lines.extend(errors)
