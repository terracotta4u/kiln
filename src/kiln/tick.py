import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field

from kiln.config import Config
from kiln.errors import KilnError
from kiln.gc import cleanup
from kiln.git import branch_exists, goal_branch_name
from kiln.jobs import is_open, list_goals, list_jobs, require_goal
from kiln.models import Goal, GoalStatus
from kiln.publish import ensure_goal_branch, publish_ready_goals
from kiln.roles.foreman import apply_actions, goal_brief, run_foreman


@dataclass
class TickResult:
    lines: list[str] = field(default_factory=list)
    foreman_failed: bool = False
    agents_ran: int = 0


def run_turn(
    conn: sqlite3.Connection,
    config: Config,
    goal: Goal,
    *,
    workers: int | None = None,
    turn: int = 1,
    turn_cap: int | None = None,
    agent_bin: str | None = None,
    reporter: Callable[[str], None] | None = None,
) -> TickResult:
    """One foreman turn for one goal: cleanup, branch, decide, then run what it asked."""
    result = TickResult()
    limit = config.max_parallel_workers if workers is None else workers
    if limit < 1:
        raise KilnError("workers must be >= 1")
    cap = config.max_foreman_turns if turn_cap is None else turn_cap
    result.lines.extend(cleanup(conn, config))
    _branch, created = ensure_goal_branch(conn, config, goal)
    if created:
        result.lines.append(f"goal #{goal.id} branch {_branch}")
    goal = require_goal(conn, goal.id)
    outcome = run_foreman(
        conn,
        config,
        goal,
        agent_bin=agent_bin,
        reporter=reporter,
        worker_limit=limit,
        turn=turn,
        turn_cap=cap,
    )
    if outcome.failure:
        result.lines.append(f"foreman failed for goal #{goal.id}: {outcome.failure}")
        result.foreman_failed = True
        return result
    if not outcome.actions:
        result.lines.append(f"goal #{goal.id}: no actions")
        return result
    applied = apply_actions(
        conn,
        config,
        goal,
        outcome.actions,
        agent_bin=agent_bin,
        reporter=reporter,
        worker_limit=limit,
    )
    result.lines.extend(applied)
    result.agents_ran = applied.agents_ran
    return result


def run_tick(
    conn: sqlite3.Connection,
    config: Config,
    *,
    workers: int | None = None,
    dry_run: bool = False,
    turn_cap: int | None = None,
    agent_bin: str | None = None,
    reporter: Callable[[str], None] | None = None,
) -> TickResult:
    """One pass over the active goals. The foreman decides what runs."""
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
            result.lines.append(
                goal_brief(
                    conn,
                    config,
                    goal,
                    worker_limit=limit,
                    turn=1,
                    turn_cap=config.max_foreman_turns if turn_cap is None else turn_cap,
                )
            )
        return result

    for goal in goals:
        turn = run_turn(
            conn,
            config,
            goal,
            workers=limit,
            turn_cap=turn_cap,
            agent_bin=agent_bin,
            reporter=reporter,
        )
        result.lines.extend(turn.lines)
        result.agents_ran += turn.agents_ran
        result.foreman_failed = result.foreman_failed or turn.foreman_failed
    return result


def run_until_done(
    conn: sqlite3.Connection,
    config: Config,
    *,
    workers: int | None = None,
    turns: int | None = None,
    agent_bin: str | None = None,
    gh_bin: str = "gh",
    reporter: Callable[[str], None] | None = None,
) -> TickResult:
    """Turn until every active goal is finished, then open a pull request for each."""
    cap = config.max_foreman_turns if turns is None else turns
    if cap < 1:
        raise KilnError("turns must be >= 1")
    result = TickResult()
    used = 0
    streak = 0
    while True:
        goals = list_goals(conn, status=GoalStatus.active)
        if not goals:
            if not result.lines:
                _record(result, ["no active goals"], reporter)
            break
        if used >= cap:
            _record(result, [f"stopped: reached {cap} foreman turns"], reporter)
            break
        before = _snapshot(conn)
        agents = 0
        failed = False
        for goal in goals:
            if used >= cap:
                _record(result, [f"stopped: reached {cap} foreman turns"], reporter)
                return result
            turn = run_turn(
                conn,
                config,
                goal,
                workers=workers,
                turn=used + 1,
                turn_cap=cap,
                agent_bin=agent_bin,
                reporter=reporter,
            )
            used += 1
            _record(result, turn.lines, reporter)
            agents += turn.agents_ran
            if turn.foreman_failed:
                streak += 1
                failed = True
            else:
                streak = 0
            _record(result, publish_ready_goals(conn, config, gh_bin=gh_bin), reporter)
            if streak >= 2:
                _record(result, ["stopped: foreman failed twice in a row"], reporter)
                return result
            if not list_goals(conn, status=GoalStatus.active):
                return result
        if agents == 0 and not failed and _snapshot(conn) == before:
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
    goals = tuple(
        (goal.id, goal.status.value, goal.branch, goal.pr_url, goal.brief, goal.evidence)
        for goal in list_goals(conn)
    )
    jobs = tuple(
        (job.id, job.status.value, job.integration.value if job.integration else "", job.attempts)
        for job in list_jobs(conn)
    )
    notes = conn.execute("SELECT COUNT(*) AS n FROM notes").fetchone()["n"]
    return (goals, jobs, notes)


def _stuck_message(conn: sqlite3.Connection) -> str:
    open_jobs = [job for job in list_jobs(conn) if is_open(job)]
    if not open_jobs:
        return "stopped: a turn made no progress"
    detail = ", ".join(f"#{job.id} {job.status.value}" for job in open_jobs)
    return f"stopped: a turn made no progress ({detail})"


def _dry_branch_line(config: Config, goal: Goal) -> str:
    name = goal.branch or goal_branch_name(goal.id, goal.title)
    if goal.branch and branch_exists(config.repo_root, name):
        return f"goal #{goal.id} on {name}"
    return f"would create branch {name}"
