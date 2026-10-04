from collections import Counter
from collections.abc import Callable
from typing import NoReturn

import typer

from kiln import __version__
from kiln.config import Config, init_factory, load_config
from kiln.db import connect, migrate
from kiln.errors import KilnError
from kiln.models import Goal, Task, TaskStatus
from kiln.roles.scout import run_scout
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
app.add_typer(goal_app, name="goal")
app.add_typer(task_app, name="task")


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
    typer.echo("\ndescription")
    typer.echo(goal.description or "(none)")


def _echo_task_lines(tasks: list[Task]) -> None:
    if not tasks:
        typer.echo("  (none)")
        return
    for task in tasks:
        typer.echo(f"  #{task.id}  {task.status.value:<9}  p{task.priority}  {task.title}")


def _format_related(tasks: list[Task]) -> str:
    if not tasks:
        return "(none)"
    return ", ".join(f"#{task.id} {task.title} [{task.status.value}]" for task in tasks)


def main() -> None:
    app()
