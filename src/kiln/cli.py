import json
import os
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

import typer

from kiln import __version__
from kiln.config import Config, init_factory, load_config
from kiln.db import connect, events_after, list_events, migrate
from kiln.errors import KilnError
from kiln.server.client import Client, ensure_running, is_running, start_detached
from kiln.server.server import serve
from kiln.gc import cleanup
from kiln.jobs import (
    add_dependency,
    add_goal,
    add_job,
    cancel_job,
    dependencies,
    dependents,
    is_open,
    list_goals,
    list_jobs,
    ready_jobs,
    require_goal,
    require_job,
)
from kiln.models import Event, Goal, Job, JobStatus, Run
from kiln.review import approve_job, reject_job, send_back
from kiln.roles.scout import run_scout
from kiln.roles.worker import run_worker
from kiln.runs import require_run
from kiln.tick import run_tick, run_until_done

app = typer.Typer(no_args_is_help=True, help="Kiln: an automated software factory for one repo.")
goal_app = typer.Typer(no_args_is_help=True, help="Manage goals.")
job_app = typer.Typer(no_args_is_help=True, help="Manage jobs.")
runs_app = typer.Typer(no_args_is_help=True, help="Inspect agent runs.")
server_app = typer.Typer(no_args_is_help=True, help="The server that owns running factories.")
app.add_typer(goal_app, name="goal")
app.add_typer(job_app, name="job")
app.add_typer(runs_app, name="runs")
app.add_typer(server_app, name="server")


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


@server_app.command("start")
def server_start() -> None:
    """Start the Kiln server in the background. Harmless if it is already running."""
    try:
        if is_running():
            info = Client().ping()
            typer.echo(f"kiln server already running (pid {info['pid']})")
            return
        info = start_detached()
    except KilnError as exc:
        _fail(exc)
    typer.echo(f"kiln server started (pid {info['pid']})")


@server_app.command("status")
def server_status() -> None:
    """Report whether the Kiln server is running."""
    if not is_running():
        typer.echo("not running")
        return
    try:
        info = Client().server_status()
    except KilnError as exc:
        _fail(exc)
    count = sum(1 for factory in info["factories"] if factory["state"] == "running")
    noun = "factory" if count == 1 else "factories"
    typer.echo(f"running (pid {info['pid']}, {count} {noun})")


@server_app.command("stop")
def server_stop(
    force: bool = typer.Option(
        False,
        "--force",
        help="Stop running factories and kill their agents, then exit.",
    ),
) -> None:
    """Stop the Kiln server. Refuses while a factory is running unless --force."""
    if not is_running():
        typer.echo("not running")
        return
    try:
        Client().stop_server(force=force)
    except KilnError as exc:
        _fail(exc)
    deadline = time.monotonic() + 20
    while is_running() and time.monotonic() < deadline:
        time.sleep(0.05)
    if is_running():
        _fail(KilnError("kiln server did not exit"))
    typer.echo("kiln server stopped")


@server_app.command("serve", hidden=True)
def server_serve() -> None:
    """Run the server in the foreground. `kiln server start` launches this."""
    try:
        serve()
    except KilnError as exc:
        _fail(exc)


@app.command()
def scout(
    question: str = typer.Argument(help="What the scout should find out."),
    goal: int | None = typer.Option(None, "--goal", "-g", help="Goal to attach the report to."),
) -> None:
    """Send a read-only scout and store its report on a job."""

    def render(config: Config, conn) -> None:
        outcome = run_scout(conn, config, question, goal_id=goal, reporter=typer.echo)
        if outcome.failure:
            raise KilnError(
                f"scout failed: {outcome.failure} (run #{outcome.run.id}, log {outcome.run.log_path})"
            )
        typer.echo(f"\njob #{outcome.job.id}  {outcome.job.status.value}")
        typer.echo(outcome.summary)

    _with_db(render, mutates_lifecycle=True)


@app.command()
def work(
    job: int | None = typer.Option(
        None,
        "--job",
        "-j",
        help="Worker job to claim. Omit to take the next ready worker.",
    ),
) -> None:
    """Claim one ready worker and run it in its own worktree."""

    def render(config: Config, conn) -> None:
        outcome = run_worker(conn, config, job_id=job, reporter=typer.echo)
        typer.echo(
            f"job #{outcome.job.id}  {outcome.job.status.value}  {outcome.job.branch}"
        )
        typer.echo(f"attempts  {outcome.job.attempts}/{outcome.job.max_attempts}")
        if outcome.run.log_path:
            typer.echo(f"log  {outcome.run.log_path}")
        if outcome.summary:
            typer.echo(f"\n{outcome.summary}")
        if outcome.diffstat:
            typer.echo(f"\n{outcome.diffstat}")
        if outcome.job.status == JobStatus.failed:
            raise KilnError(f"job #{outcome.job.id} failed: {outcome.failure}")
        if outcome.failure:
            raise KilnError(f"job #{outcome.job.id} completed with a problem: {outcome.failure}")

    _with_db(render, mutates_lifecycle=True)


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
    """Ask the Kiln server to run this repository's factory, and follow its events.

    Closing this command detaches. The factory keeps running. Reconnect with `kiln attach`.
    """
    if dry_run:
        def render(config: Config, conn) -> None:
            if turns is not None and turns < 1:
                raise KilnError("turns must be >= 1")
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

        _with_db(render)
        return
    if turns is not None and turns < 1:
        _fail(KilnError("turns must be >= 1"))
    try:
        config = load_config()
        client = ensure_running()
        _warn_version(client.ping())
        started = client.start_factory(
            config.repo_root,
            workers=workers,
            turns=turns,
            agent_bin=os.environ.get("KILN_AGENT_BIN"),
        )
        if not started["started"]:
            typer.echo(
                f"factory already running for {config.repo_root} "
                f"since {started['factory']['started_at']}; attaching"
            )
        _follow(client, config.repo_root, int(started["cursor"]))
    except KeyboardInterrupt:
        typer.echo("detached; the factory keeps running. Reconnect with `kiln attach`.")
    except KilnError as exc:
        _fail(exc)


@app.command()
def attach(
    since: int | None = typer.Option(
        None,
        "--since",
        help="Print events after this id, then follow new ones.",
    ),
) -> None:
    """Show this repository's factory and follow its events. Detach leaves the factory running."""
    try:
        config = load_config()
    except KilnError as exc:
        _fail(exc)
    if not is_running():
        _fail(KilnError("kiln server is not running; start it with `kiln server start`"))
    client = Client()
    try:
        _warn_version(client.ping())
        info = client.factory_status(config.repo_root)
        cursor = _print_factory_view(config, info, since)
    except KilnError as exc:
        _fail(exc)
    if info["factory"]["state"] != "running":
        _echo_outcome(info["factory"])
        typer.echo("no factory running for this repository; use `kiln run`")
        return
    try:
        _follow(client, config.repo_root, cursor)
    except KeyboardInterrupt:
        typer.echo("detached; the factory keeps running. Reconnect with `kiln attach`.")
    except KilnError as exc:
        _fail(exc)


@app.command()
def review(
    job_id: int = typer.Argument(help="Completed worker whose integration is still pending."),
    approve: bool = typer.Option(False, "--approve", help="Merge the job branch into the goal branch."),
    rework: str | None = typer.Option(None, "--rework", help="Send the job back with this feedback."),
    reject: bool = typer.Option(
        False,
        "--reject",
        help="Reject the branch. Execution stays completed and the result stays.",
    ),
    reason: str = typer.Option("", "--reason", help="Why the job is rejected."),
) -> None:
    """Approve, rework, or reject a completed worker. This does not run the reviewer."""
    chosen = sum((approve, rework is not None, reject))
    if chosen != 1:
        _fail(KilnError("pass exactly one of --approve, --rework, or --reject"))

    def render(config: Config, conn) -> None:
        if approve:
            typer.echo(approve_job(conn, config, job_id))
        elif rework is not None:
            typer.echo(send_back(conn, job_id, rework))
        else:
            typer.echo(reject_job(conn, job_id, reason))

    _with_db(render, mutates_lifecycle=True)


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
    """Remove worktrees and merged branches left behind by finished jobs."""

    def render(config: Config, conn) -> None:
        lines = cleanup(conn, config)
        if not lines:
            typer.echo("nothing to clean")
            return
        for line in lines:
            typer.echo(line)

    _with_db(render, mutates_lifecycle=True)


@runs_app.command("show")
def runs_show(run_id: int = typer.Argument(help="Run id.")) -> None:
    """Show one agent run, its report, and the tail of its log."""

    def render(_config: Config, conn) -> None:
        run = require_run(conn, run_id)
        typer.echo(f"run #{run.id}")
        typer.echo(f"role        {run.role}")
        typer.echo(f"model       {run.model}")
        typer.echo(f"status      {run.status.value}")
        typer.echo(f"job         {_format_job_ref(run)}")
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
    """Show goals, ready work, and jobs in progress."""

    def render(config: Config, conn) -> None:
        typer.echo(_server_line(config.repo_root))
        typer.echo(f"repo  {config.repo_root}")
        goals = list_goals(conn)
        if not goals:
            typer.echo("no goals")
            return
        jobs = list_jobs(conn)
        by_goal: dict[int, list[Job]] = {goal.id: [] for goal in goals}
        for job in jobs:
            by_goal.setdefault(job.goal_id, []).append(job)
        for goal in goals:
            counts = Counter(job.status for job in by_goal.get(goal.id, []))
            summary = ", ".join(
                f"{counts[state]} {state.value}" for state in JobStatus if counts[state]
            )
            typer.echo(f"\n#{goal.id}  {goal.status.value}  {goal.title}")
            typer.echo(f"    {summary or 'no jobs'}")

        ready = ready_jobs(conn)
        ready_ids = {job.id for job in ready}
        typer.echo("\nready")
        _echo_job_lines(ready)

        blocked = [
            job for job in jobs if job.status == JobStatus.pending and job.id not in ready_ids
        ]
        if blocked:
            typer.echo("\nblocked")
            for job in blocked:
                deps = ", ".join(f"#{dep.id}" for dep in dependencies(conn, job.id))
                typer.echo(f"  #{job.id}  p{job.priority}  {job.title}  deps: {deps}")

        inflight = [
            job
            for job in jobs
            if is_open(job) and job.status != JobStatus.pending
        ]
        if inflight:
            typer.echo("\nin progress")
            _echo_job_lines(inflight)

    _with_db(render)


@goal_app.command("add")
def goal_add(
    title: str = typer.Argument(help="Short name for the goal."),
    description: str = typer.Option("", "--description", "-d", help="What done looks like."),
) -> None:
    """Add a goal."""

    def render(config: Config, conn) -> None:
        goal = add_goal(conn, title, description)
        typer.echo(f"goal #{goal.id}  {goal.title}")
        _hand_goal_to_running_server(config)

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
    """Show a goal and its jobs."""

    def render(_config: Config, conn) -> None:
        goal = require_goal(conn, goal_id)
        _echo_goal(goal)
        jobs = list_jobs(conn, goal_id=goal.id)
        typer.echo("\njobs")
        _echo_job_lines(jobs)

    _with_db(render)


@job_app.command("add")
def job_add(
    goal_id: int = typer.Argument(help="Goal this job belongs to."),
    title: str = typer.Argument(help="Short name for the job."),
    description: str = typer.Option("", "--description", "-d"),
    acceptance: str = typer.Option("", "--acceptance", "-a", help="How to tell the job is done."),
    priority: int = typer.Option(0, "--priority", "-p", help="Higher values are scheduled first."),
    depends_on: list[int] | None = typer.Option(
        None,
        "--depends-on",
        help="Job that must be done first. Repeatable.",
    ),
) -> None:
    """Add a worker job to a goal."""

    def render(config: Config, conn) -> None:
        job = add_job(
            conn,
            goal_id,
            title,
            description=description,
            acceptance=acceptance,
            priority=priority,
            max_attempts=config.max_attempts,
            depends_on=depends_on,
        )
        typer.echo(f"job #{job.id}  {job.title}")

    _with_db(render, mutates_lifecycle=True)


@job_app.command("list")
def job_list(
    status: JobStatus | None = typer.Option(None, "--status", "-s"),
    goal_id: int | None = typer.Option(None, "--goal", "-g"),
) -> None:
    """List jobs."""

    def render(_config: Config, conn) -> None:
        jobs = list_jobs(conn, goal_id=goal_id, status=status)
        if not jobs:
            typer.echo("no jobs")
            return
        for job in jobs:
            deps = dependencies(conn, job.id)
            suffix = ""
            if deps:
                suffix = "  deps: " + ", ".join(f"#{dep.id}" for dep in deps)
            typer.echo(
                f"#{job.id}  {job.status.value:<9}  {job.role.value:<8}  p{job.priority}  {job.title}{suffix}"
            )

    _with_db(render)


@job_app.command("show")
def job_show(job_id: int = typer.Argument(help="Job id.")) -> None:
    """Show a job, its dependencies, and anything it blocks."""

    def render(_config: Config, conn) -> None:
        job = require_job(conn, job_id)
        goal = require_goal(conn, job.goal_id)
        typer.echo(f"job #{job.id}")
        typer.echo(f"role        {job.role.value}")
        typer.echo(f"status      {job.status.value}")
        if job.integration:
            typer.echo(f"integration {job.integration.value}")
        if job.target_job_id is not None:
            typer.echo(f"target      #{job.target_job_id}")
        typer.echo(f"goal        #{goal.id}  {goal.title}")
        typer.echo(f"title       {job.title}")
        typer.echo(f"priority    {job.priority}")
        typer.echo(f"attempts    {job.attempts}/{job.max_attempts}")
        if job.claimed_by:
            typer.echo(f"claimed by  {job.claimed_by}")
        if job.branch:
            typer.echo(f"branch      {job.branch}")
        if job.worktree_path:
            typer.echo(f"worktree    {job.worktree_path}")
        if job.feedback:
            typer.echo(f"feedback    {job.feedback}")
        typer.echo("depends on  " + _format_related(dependencies(conn, job.id)))
        typer.echo("blocks      " + _format_related(dependents(conn, job.id)))
        typer.echo("\ndescription")
        typer.echo(job.description or "(none)")
        typer.echo("\nacceptance")
        typer.echo(job.acceptance or "(none)")

    _with_db(render)


@job_app.command("dep")
def job_dep(
    job_id: int = typer.Argument(help="Job that waits."),
    depends_on: int = typer.Argument(help="Job that must be done first."),
) -> None:
    """Record that JOB_ID waits until DEPENDS_ON is done."""

    def render(_config: Config, conn) -> None:
        add_dependency(conn, job_id, depends_on)
        typer.echo(f"job #{job_id} depends on #{depends_on}")

    _with_db(render, mutates_lifecycle=True)


@job_app.command("cancel")
def job_cancel(job_id: int = typer.Argument(help="Job id.")) -> None:
    """Cancel a job that is not already finished."""

    def render(_config: Config, conn) -> None:
        job = cancel_job(conn, job_id)
        typer.echo(f"cancelled #{job.id}  {job.title}")

    _with_db(render, mutates_lifecycle=True)


def _with_db(fn: Callable, *, mutates_lifecycle: bool = False) -> None:
    try:
        config = load_config()
    except KilnError as exc:
        _fail(exc)
    if mutates_lifecycle:
        try:
            _refuse_if_factory_running(config)
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


def _hand_goal_to_running_server(config: Config) -> None:
    """If the server is up, ask it to include this goal in the factory.

    A running factory takes another pass before it marks itself completed. If the
    previous run has already finished, this starts the next one. A server that is
    not running is left alone; `kiln run` picks the goal up later.
    """
    if not is_running():
        return
    try:
        Client().start_factory(config.repo_root, agent_bin=os.environ.get("KILN_AGENT_BIN"))
    except KilnError:
        return


def _refuse_if_factory_running(config: Config) -> None:
    """The server owns job lifecycle while it is running a factory for this repo."""
    if not is_running():
        return
    try:
        info = Client().factory_status(config.repo_root)
    except KilnError:
        return
    factory = info["factory"]
    if factory["state"] != "running":
        return
    raise KilnError(
        "a factory is running for this repository "
        f"(since {factory['started_at']}); the server owns job lifecycle while it runs. "
        "Watch it with `kiln attach`, wait for it to finish, or `kiln server stop --force`."
    )


def _warn_version(info: dict) -> None:
    remote = info.get("version")
    if remote and remote != __version__:
        typer.echo(
            f"warning: kiln server is {remote}; this client is {__version__}. "
            "Restart it with `kiln server stop` and `kiln server start`.",
            err=True,
        )


def _server_line(repo: Path) -> str:
    if not is_running():
        return "server  not running"
    try:
        info = Client().factory_status(repo)
    except KilnError:
        return "server  not running"
    factory = info["factory"]
    if factory["state"] == "running":
        return f"server  running, factory running since {factory['started_at']}"
    return "server  running, idle"


_FOLLOW_PAGE = 100


def _follow(client: Client, repo: Path, cursor: int) -> None:
    while True:
        page = client.events(repo, after=cursor, limit=_FOLLOW_PAGE)
        events = page["events"]
        for raw in events:
            event = _event_from_payload(raw)
            typer.echo(_format_event(event))
            cursor = event.id
        factory = page["factory"]
        if factory["state"] == "running":
            time.sleep(0.5)
            continue
        # A finished factory can still have more events than one page. Keep
        # reading until a short page, which means the cursor has caught the tail.
        if len(events) >= _FOLLOW_PAGE:
            continue
        _echo_outcome(factory)
        if factory["state"] == "failed":
            raise typer.Exit(code=1)
        return


def _print_factory_view(config: Config, info: dict, since: int | None) -> int:
    factory = info["factory"]
    if factory["state"] == "running":
        typer.echo(f"factory  running since {factory['started_at']}")
    elif factory["state"] == "idle":
        typer.echo("factory  idle")
    else:
        detail = factory["state"]
        if factory.get("stop_reason"):
            detail += f" ({factory['stop_reason']})"
        typer.echo(f"factory  {detail}")
    typer.echo("\ngoals")
    goals = info.get("goals") or []
    if not goals:
        typer.echo("  (none)")
    for goal in goals:
        typer.echo(f"  #{goal['id']}  {goal['status']}  {goal['title']}")
    typer.echo("\nopen jobs")
    jobs = info.get("open_jobs") or []
    if not jobs:
        typer.echo("  (none)")
    for job in jobs:
        typer.echo(f"  #{job['id']}  {job['status']:<9}  {job['role']}  {job['title']}")
    typer.echo("\nevents")
    return _print_backlog(config, since, following=factory["state"] == "running")


def _print_backlog(config: Config, since: int | None, *, following: bool) -> int:
    conn = connect(config.db_path)
    try:
        migrate(conn)
        if since is None:
            events = list_events(conn, limit=20)
        else:
            events = events_after(conn, since, limit=500)
    finally:
        conn.close()
    for event in events:
        typer.echo(_format_event(event))
    if not events and not following:
        typer.echo("(none)")
    if events:
        return events[-1].id
    return 0 if since is None else since


def _echo_outcome(factory: dict) -> None:
    state = factory["state"]
    if state == "completed":
        typer.echo("factory completed")
    elif state == "stopped":
        typer.echo(f"factory stopped ({factory['stop_reason']})")
    elif state == "failed":
        typer.echo(f"factory failed: {factory['error']}")


def _event_from_payload(raw: dict) -> Event:
    return Event(
        id=raw["id"],
        ts=raw["ts"],
        kind=raw["kind"],
        job_id=raw["job_id"],
        run_id=raw["run_id"],
        message=raw["message"],
    )


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


def _echo_job_lines(jobs: list[Job]) -> None:
    if not jobs:
        typer.echo("  (none)")
        return
    for job in jobs:
        typer.echo(f"  #{job.id}  {job.status.value:<9}  p{job.priority}  {job.title}")


def _format_event(event: Event) -> str:
    refs = []
    if event.job_id is not None:
        refs.append(f"job #{event.job_id}")
    if event.run_id is not None:
        refs.append(f"run #{event.run_id}")
    prefix = f"{event.ts}  {event.kind}"
    if refs:
        prefix += "  " + "  ".join(refs)
    message = " ".join(event.message.split())
    return f"{prefix}  {message}"


def _format_job_ref(run: Run) -> str:
    if run.job_id is None:
        return "(none)"
    return f"#{run.job_id}"


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


def _format_related(jobs: list[Job]) -> str:
    if not jobs:
        return "(none)"
    return ", ".join(f"#{job.id} {job.title} [{job.status.value}]" for job in jobs)


def main() -> None:
    app()
