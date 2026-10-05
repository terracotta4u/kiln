import json
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

import typer

from kiln import __version__
from kiln.config import Config, init_factory, load_config
from kiln.db import connect, list_events, migrate
from kiln.errors import KilnError
from kiln.gc import cleanup
from kiln.models import Event, Goal, Run, Task, TaskStatus
from kiln.review import approve_task, reject_task, send_back
from kiln.roles.scout import run_scout
from kiln.roles.worker import run_worker
from kiln.runs import require_run
from kiln.tick import run_tick, run_until_done
from kiln.tasks import (
    add_dependency,
    add_goal,
    add_task,
    cancel_task,
    dependencies,
    dependents,
    list_goals,
    list_tasks,
    ready_tasks,
    require_goal,
    require_task,
)

app = typer.Typer(no_args_is_help=True, help="Kiln: an automated software factory for one repo.")
goal_app = typer.Typer(no_args_is_help=True, help="Manage goals.")
task_app = typer.Typer(no_args_is_help=True, help="Manage tasks.")
runs_app = typer.Typer(no_args_is_help=True, help="Inspect agent runs.")
app.add_typer(goal_app, name="goal")
app.add_typer(task_app, name="task")
app.add_typer(runs_app, name="runs")


def _version(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def callback(
    version: bool = typer.Option(
        False,
        "--version",
        help="Show the kiln version.",
        callback=_version,
        is_eager=True,
    ),
) -> None:
    """Kiln: an automated software factory for one repo."""


@app.command()
def init() -> None:
    """Create kiln.toml and the local .kiln database for this repository."""
    try:
        config, created = init_factory()
    except KilnError as exc:
        _fail(exc)
    if created:
        typer.echo(f"wrote {config.toml_path}")
    else:
        typer.echo(f"{config.toml_path} already exists; left unchanged")
    typer.echo(f"database {config.db_path}")


@app.command()
def scout(
    question: str = typer.Argument(help="What the scout should find out."),
    goal: int | None = typer.Option(None, "--goal", "-g", help="Goal to attach the report to."),
) -> None:
    """Send a read-only scout and store its report as a note."""

    def render(config: Config, conn) -> None:
        outcome = run_scout(conn, config, question, goal_id=goal, reporter=typer.echo)
        if outcome.note is None:
            raise KilnError(
                f"scout failed: {outcome.failure} (run #{outcome.run.id}, log {outcome.run.log_path})"
            )
        typer.echo(f"\nnote #{outcome.note.id} on goal #{outcome.goal.id}")
        typer.echo(outcome.note.text)

    _with_db(render)


@app.command()
def work(
    task: int | None = typer.Option(
        None,
        "--task",
        "-t",
        help="Task to claim. Omit to take the next ready task.",
    ),
) -> None:
    """Claim one ready task and run a worker in its own worktree."""

    def render(config: Config, conn) -> None:
        outcome = run_worker(conn, config, task_id=task, reporter=typer.echo)
        typer.echo(
            f"task #{outcome.task.id}  {outcome.task.status.value}  {outcome.task.branch}"
        )
        typer.echo(f"attempts  {outcome.task.attempts}/{outcome.task.max_attempts}")
        if outcome.run.log_path:
            typer.echo(f"log  {outcome.run.log_path}")
        if outcome.summary:
            typer.echo(f"\n{outcome.summary}")
        if outcome.diffstat:
            typer.echo(f"\n{outcome.diffstat}")
        if outcome.failure:
            raise KilnError(f"task #{outcome.task.id} is in review: {outcome.failure}")

    _with_db(render)


@app.command()
def run(
    workers: int | None = typer.Option(
        None,
        "--workers",
        "-w",
        help="How many scouts, workers, and reviewers may run at once. Defaults to max_parallel_workers.",
    ),
    turns: int | None = typer.Option(
        None,
        "--turns",
        help="Stop after this many foreman turns. Defaults to max_foreman_turns.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print the state the foreman would see and change nothing.",
    ),
) -> None:
    """Run until every active goal is finished, then open a pull request."""

    def render(config: Config, conn) -> None:
        if turns is not None and turns < 1:
            raise KilnError("turns must be >= 1")
        if dry_run:
            outcome = run_tick(
                conn,
                config,
                workers=workers,
                dry_run=True,
                turn_cap=turns,
                reporter=typer.echo,
            )
            for line in outcome.lines:
                typer.echo(line)
            return
        run_until_done(conn, config, workers=workers, turns=turns, reporter=typer.echo)

    _with_db(render)


@app.command()
def review(
    task_id: int = typer.Argument(help="Task in review."),
    approve: bool = typer.Option(False, "--approve", help="Merge the task branch into the base branch."),
    rework: str | None = typer.Option(None, "--rework", help="Send the task back with this feedback."),
    fail: bool = typer.Option(False, "--fail", help="Fail the task."),
    reason: str = typer.Option("", "--reason", help="Why the task failed."),
) -> None:
    """Approve, rework, or fail a task that is in review."""
    chosen = sum((approve, rework is not None, fail))
    if chosen != 1:
        _fail(KilnError("pass exactly one of --approve, --rework, or --fail"))

    def render(config: Config, conn) -> None:
        if approve:
            typer.echo(approve_task(conn, config, task_id))
        elif rework is not None:
            typer.echo(send_back(conn, task_id, rework))
        else:
            typer.echo(reject_task(conn, task_id, reason))

    _with_db(render)


@app.command()
def log(
    limit: int = typer.Option(20, "--limit", "-n", help="How many events to show."),
) -> None:
    """Show recent factory events, oldest first."""
    if limit < 1:
        _fail(KilnError("log limit must be >= 1"))

    def render(_config: Config, conn) -> None:
        events = list_events(conn, limit=limit)
        if not events:
            typer.echo("no events")
            return
        for event in events:
            typer.echo(_format_event(event))

    _with_db(render)


@app.command()
def gc() -> None:
    """Remove worktrees and merged branches left behind by finished tasks."""

    def render(config: Config, conn) -> None:
        lines = cleanup(conn, config)
        if not lines:
            typer.echo("nothing to clean")
            return
        for line in lines:
            typer.echo(line)

    _with_db(render)


@runs_app.command("show")
def runs_show(run_id: int = typer.Argument(help="Run id.")) -> None:
    """Show one agent run, its report, and the tail of its log."""

    def render(_config: Config, conn) -> None:
        run = require_run(conn, run_id)
        typer.echo(f"run #{run.id}")
        typer.echo(f"role        {run.role}")
        typer.echo(f"model       {run.model}")
        typer.echo(f"status      {run.status.value}")
        typer.echo(f"task        {_format_task_ref(run)}")
        typer.echo(f"exit        {_format_exit(run.exit_code)}")
        typer.echo(f"started     {run.started_at}")
        typer.echo(f"finished    {run.finished_at or '(still running)'}")
        typer.echo(f"log         {run.log_path or '(none)'}")
        typer.echo("\nreport")
        typer.echo(_format_report(run))
        typer.echo("\nlog")
        typer.echo(_read_log_tail(run.log_path))

    _with_db(render)


@app.command()
def status() -> None:
    """Show goals, ready work, and tasks in progress."""

    def render(config: Config, conn) -> None:
        typer.echo(f"repo  {config.repo_root}")
        goals = list_goals(conn)
        if not goals:
            typer.echo("no goals")
            return
        tasks = list_tasks(conn)
        by_goal: dict[int, list[Task]] = {goal.id: [] for goal in goals}
        for task in tasks:
            by_goal.setdefault(task.goal_id, []).append(task)
        for goal in goals:
            counts = Counter(task.status for task in by_goal.get(goal.id, []))
            summary = ", ".join(
                f"{counts[state]} {state.value}" for state in TaskStatus if counts[state]
            )
            typer.echo(f"\n#{goal.id}  {goal.status.value}  {goal.title}")
            typer.echo(f"    {summary or 'no tasks'}")

        ready = ready_tasks(conn)
        ready_ids = {task.id for task in ready}
        typer.echo("\nready")
        _echo_task_lines(ready)

        blocked = [
            task for task in tasks if task.status == TaskStatus.pending and task.id not in ready_ids
        ]
        if blocked:
            typer.echo("\nblocked")
            for task in blocked:
                deps = ", ".join(f"#{dep.id}" for dep in dependencies(conn, task.id))
                typer.echo(f"  #{task.id}  p{task.priority}  {task.title}  deps: {deps}")

        inflight = [
            task
            for task in tasks
            if task.status in (TaskStatus.claimed, TaskStatus.running, TaskStatus.review)
        ]
        if inflight:
            typer.echo("\nin progress")
            _echo_task_lines(inflight)

    _with_db(render)


@goal_app.command("add")
def goal_add(
    title: str = typer.Argument(help="Short name for the goal."),
    description: str = typer.Option("", "--description", "-d", help="What done looks like."),
) -> None:
    """Add a goal."""

    def render(_config: Config, conn) -> None:
        goal = add_goal(conn, title, description)
        typer.echo(f"goal #{goal.id}  {goal.title}")

    _with_db(render)


@goal_app.command("list")
def goal_list() -> None:
    """List goals."""

    def render(_config: Config, conn) -> None:
        goals = list_goals(conn)
        if not goals:
            typer.echo("no goals")
            return
        for goal in goals:
            typer.echo(f"#{goal.id}  {goal.status.value}  {goal.title}")

    _with_db(render)


@goal_app.command("show")
def goal_show(goal_id: int = typer.Argument(help="Goal id.")) -> None:
    """Show a goal and its tasks."""

    def render(_config: Config, conn) -> None:
        goal = require_goal(conn, goal_id)
        _echo_goal(goal)
        tasks = list_tasks(conn, goal_id=goal.id)
        typer.echo("\ntasks")
        _echo_task_lines(tasks)

    _with_db(render)


@task_app.command("add")
def task_add(
    goal_id: int = typer.Argument(help="Goal this task belongs to."),
    title: str = typer.Argument(help="Short name for the task."),
    description: str = typer.Option("", "--description", "-d"),
    acceptance: str = typer.Option("", "--acceptance", "-a", help="How to tell the task is done."),
    priority: int = typer.Option(0, "--priority", "-p", help="Higher values are scheduled first."),
    depends_on: list[int] | None = typer.Option(
        None,
        "--depends-on",
        help="Task that must be done first. Repeatable.",
    ),
) -> None:
    """Add a task to a goal."""

    def render(config: Config, conn) -> None:
        task = add_task(
            conn,
            goal_id,
            title,
            description=description,
            acceptance=acceptance,
            priority=priority,
            max_attempts=config.max_attempts,
        )
        for dep_id in depends_on or []:
            add_dependency(conn, task.id, dep_id)
        typer.echo(f"task #{task.id}  {task.title}")

    _with_db(render)


@task_app.command("list")
def task_list(
    status: TaskStatus | None = typer.Option(None, "--status", "-s"),
    goal_id: int | None = typer.Option(None, "--goal", "-g"),
) -> None:
    """List tasks."""

    def render(_config: Config, conn) -> None:
        tasks = list_tasks(conn, goal_id=goal_id, status=status)
        if not tasks:
            typer.echo("no tasks")
            return
        for task in tasks:
            deps = dependencies(conn, task.id)
            suffix = ""
            if deps:
                suffix = "  deps: " + ", ".join(f"#{dep.id}" for dep in deps)
            typer.echo(f"#{task.id}  {task.status.value:<9}  p{task.priority}  {task.title}{suffix}")

    _with_db(render)


@task_app.command("show")
def task_show(task_id: int = typer.Argument(help="Task id.")) -> None:
    """Show a task, its dependencies, and anything it blocks."""

    def render(_config: Config, conn) -> None:
        task = require_task(conn, task_id)
        goal = require_goal(conn, task.goal_id)
        typer.echo(f"task #{task.id}")
        typer.echo(f"status      {task.status.value}")
        typer.echo(f"goal        #{goal.id}  {goal.title}")
        typer.echo(f"title       {task.title}")
        typer.echo(f"priority    {task.priority}")
        typer.echo(f"attempts    {task.attempts}/{task.max_attempts}")
        if task.claimed_by:
            typer.echo(f"claimed by  {task.claimed_by}")
        if task.branch:
            typer.echo(f"branch      {task.branch}")
        if task.worktree_path:
            typer.echo(f"worktree    {task.worktree_path}")
        if task.feedback:
            typer.echo(f"feedback    {task.feedback}")
        typer.echo("depends on  " + _format_related(dependencies(conn, task.id)))
        typer.echo("blocks      " + _format_related(dependents(conn, task.id)))
        typer.echo("\ndescription")
        typer.echo(task.description or "(none)")
        typer.echo("\nacceptance")
        typer.echo(task.acceptance or "(none)")

    _with_db(render)


@task_app.command("dep")
def task_dep(
    task_id: int = typer.Argument(help="Task that waits."),
    depends_on: int = typer.Argument(help="Task that must be done first."),
) -> None:
    """Record that TASK_ID waits until DEPENDS_ON is done."""

    def render(_config: Config, conn) -> None:
        add_dependency(conn, task_id, depends_on)
        typer.echo(f"task #{task_id} depends on #{depends_on}")

    _with_db(render)


@task_app.command("cancel")
def task_cancel(task_id: int = typer.Argument(help="Task id.")) -> None:
    """Cancel a task that is not already done."""

    def render(_config: Config, conn) -> None:
        task = cancel_task(conn, task_id)
        typer.echo(f"cancelled #{task.id}  {task.title}")

    _with_db(render)


def _with_db(fn: Callable) -> None:
    try:
        config = load_config()
    except KilnError as exc:
        _fail(exc)
    conn = connect(config.db_path)
    try:
        migrate(conn)
        fn(config, conn)
    except KilnError as exc:
        _fail(exc)
    finally:
        conn.close()


def _fail(exc: KilnError) -> NoReturn:
    typer.echo(f"error: {exc}", err=True)
    raise typer.Exit(1) from exc


def _echo_goal(goal: Goal) -> None:
    typer.echo(f"goal #{goal.id}")
    typer.echo(f"status       {goal.status.value}")
    typer.echo(f"title        {goal.title}")
    typer.echo(f"created      {goal.created_at}")
    if goal.branch:
        typer.echo(f"branch       {goal.branch}")
    if goal.pr_url:
        typer.echo(f"pull request {goal.pr_url}")
    typer.echo("\ndescription")
    typer.echo(goal.description or "(none)")
    typer.echo("\nbrief")
    typer.echo(goal.brief or "(none)")
    typer.echo("\nevidence")
    if not goal.evidence:
        typer.echo("(none)")
    else:
        for item in goal.evidence:
            typer.echo(f"- {item}")


def _echo_task_lines(tasks: list[Task]) -> None:
    if not tasks:
        typer.echo("  (none)")
        return
    for task in tasks:
        typer.echo(f"  #{task.id}  {task.status.value:<9}  p{task.priority}  {task.title}")


def _format_event(event: Event) -> str:
    refs = []
    if event.task_id is not None:
        refs.append(f"task #{event.task_id}")
    if event.run_id is not None:
        refs.append(f"run #{event.run_id}")
    prefix = f"{event.ts}  {event.kind}"
    if refs:
        prefix += "  " + "  ".join(refs)
    message = " ".join(event.message.split())
    return f"{prefix}  {message}"


def _format_task_ref(run: Run) -> str:
    if run.task_id is None:
        return "(none)"
    return f"#{run.task_id}"


def _format_exit(exit_code: int | None) -> str:
    if exit_code is None:
        return "(none)"
    return str(exit_code)


def _format_report(run: Run) -> str:
    if not run.report_json:
        return "(none)"
    try:
        parsed = json.loads(run.report_json)
    except json.JSONDecodeError:
        return run.report_json
    return json.dumps(parsed, indent=2)


def _read_log_tail(log_path: str | None, *, lines: int = 40) -> str:
    if not log_path:
        return "(none)"
    path = Path(log_path)
    if not path.is_file():
        return f"(missing) {path}"
    text = path.read_text(encoding="utf-8", errors="replace")
    parts = text.splitlines()
    if len(parts) <= lines:
        return text.rstrip("\n") or "(empty)"
    omitted = len(parts) - lines
    return f"... {omitted} earlier lines\n" + "\n".join(parts[-lines:])


def _format_related(tasks: list[Task]) -> str:
    if not tasks:
        return "(none)"
    return ", ".join(f"#{task.id} {task.title} [{task.status.value}]" for task in tasks)


def main() -> None:
    app()
