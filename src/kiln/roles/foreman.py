import json
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from kiln.agent import DEFAULT_TIMEOUT_SECONDS, run_agent
from kiln.config import Config
from kiln.db import connect, migrate, record_event
from kiln.errors import KilnError
from kiln.git import diffstat, goal_branch_name
from kiln.models import Goal, GoalStatus, Run, RunStatus, Task, TaskStatus
from kiln.notes import add_note, list_notes
from kiln.prompts import render_prompt
from kiln.review import approve_task, reject_task, send_back
from kiln.roles.reviewer import run_reviewer
from kiln.roles.scout import run_scout
from kiln.roles.worker import run_worker
from kiln.runs import finish_run, latest_run, start_run
from kiln.tasks import (
    add_dependency,
    add_task,
    cancel_task,
    dependencies,
    fail_task,
    list_tasks,
    ready_tasks,
    require_goal,
    require_task,
    set_goal_brief,
    set_goal_evidence,
    set_goal_status,
)

_OPEN = {TaskStatus.pending, TaskStatus.claimed, TaskStatus.running, TaskStatus.review}
_AGENT_TYPES = frozenset({"scout", "dispatch", "review"})
_NOTE_LIMIT = 2000


@dataclass(frozen=True)
class ForemanOutcome:
    run: Run
    actions: list[dict]
    failure: str | None


def goal_brief(
    conn: sqlite3.Connection,
    config: Config,
    goal: Goal,
    *,
    worker_limit: int | None = None,
    turn: int | None = None,
    turn_cap: int | None = None,
) -> str:
    """Text the foreman sees. Summaries only, not file contents."""
    limit = config.max_parallel_workers if worker_limit is None else worker_limit
    lines = [
        f"Goal #{goal.id}: {goal.title}",
        goal.description or "(no description)",
        "",
        "Brief:",
        goal.brief or "(none yet)",
        "",
    ]
    if turn is not None and turn_cap is not None:
        lines.append(f"Turn {turn} of {turn_cap}")
    lines.extend(
        [
            f"Workers this turn: {limit}",
            "",
            "Tasks:",
        ]
    )
    tasks = list_tasks(conn, goal_id=goal.id)
    ready_ids = {task.id for task in ready_tasks(conn, goal_id=goal.id)}
    if not tasks:
        lines.append("(none)")
    for task in tasks:
        deps = dependencies(conn, task.id)
        dep_text = ", ".join(f"#{dep.id} {dep.title}" for dep in deps) or "-"
        availability = _availability(task, deps, ready_ids)
        lines.append(
            f"- #{task.id} [{task.status.value}] {availability}p{task.priority} "
            f"attempts {task.attempts}/{task.max_attempts} deps {dep_text}: {task.title}"
        )
        if task.feedback:
            lines.append(f"  feedback: {task.feedback}")
        if task.status == TaskStatus.review:
            lines.extend(_review_lines(conn, config, goal, task))
    lines.append("")
    lines.append("Notes:")
    notes = list_notes(conn, goal.id)
    if not notes:
        lines.append("(none)")
    for note in notes[-10:]:
        text = note.text if len(note.text) <= _NOTE_LIMIT else note.text[:_NOTE_LIMIT] + "..."
        lines.append(f"- note #{note.id}")
        lines.append(text)
    return "\n".join(lines)


def run_foreman(
    conn: sqlite3.Connection,
    config: Config,
    goal: Goal,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    agent_bin: str | None = None,
    reporter: Callable[[str], None] | None = None,
    worker_limit: int | None = None,
    turn: int | None = None,
    turn_cap: int | None = None,
) -> ForemanOutcome:
    run = start_run(conn, role="foreman", model=config.models.foreman)
    if reporter:
        reporter(f"foreman for goal #{goal.id} with {config.models.foreman}")
    prompt = render_prompt(
        "foreman.md",
        {
            "repo_root": str(config.repo_root),
            "base_branch": config.base_branch,
            "integration_branch": goal.branch or goal_branch_name(goal.id, goal.title),
            "state": goal_brief(
                conn, config, goal, worker_limit=worker_limit, turn=turn, turn_cap=turn_cap
            ),
        },
    )
    log_path = config.runs_dir / f"{run.id}.log"
    try:
        result = run_agent(
            prompt=prompt,
            model=config.models.foreman,
            workspace=config.repo_root,
            log_path=log_path,
            mode="ask",
            timeout=timeout,
            agent_bin=agent_bin,
        )
    except KilnError as exc:
        finished = finish_run(
            conn,
            run.id,
            status=RunStatus.failed,
            exit_code=None,
            log_path=None,
            report=None,
        )
        return ForemanOutcome(run=finished, actions=[], failure=str(exc))

    failure = _failure(result)
    actions = result.report.get("actions") if result.report else None
    if failure is None and not isinstance(actions, list):
        failure = "response had no actions list"
    finished = finish_run(
        conn,
        run.id,
        status=RunStatus.failed if failure else RunStatus.succeeded,
        exit_code=result.exit_code,
        log_path=str(result.log_path),
        report=result.report,
    )
    if failure:
        return ForemanOutcome(run=finished, actions=[], failure=failure)
    assert isinstance(actions, list)
    return ForemanOutcome(run=finished, actions=actions, failure=None)


class AppliedActions(list[str]):
    """Action messages in the order the foreman asked, plus how many agents started."""

    agents_ran: int

    def __init__(self, messages: list[str], agents_ran: int) -> None:
        super().__init__(messages)
        self.agents_ran = agents_ran


def apply_actions(
    conn: sqlite3.Connection,
    config: Config,
    goal: Goal,
    actions: list[dict],
    *,
    agent_bin: str | None = None,
    reporter: Callable[[str], None] | None = None,
    worker_limit: int | None = None,
) -> AppliedActions:
    """Apply foreman decisions. One bad action does not discard the rest.

    State changes run first. Scout, dispatch, and review then run together,
    and only when an action asks for them.
    """
    refs: dict[str, int] = {}
    for task in list_tasks(conn, goal_id=goal.id):
        refs.setdefault(task.title, task.id)
    limit = config.max_parallel_workers if worker_limit is None else worker_limit
    indexed = list(enumerate(actions, start=1))
    messages: dict[int, str] = {}
    for index, action in indexed:
        if _is_agent(action):
            continue
        messages[index] = _record_action(
            conn,
            _attempt(
                conn,
                config,
                goal,
                action,
                refs,
                index=index,
                agent_bin=agent_bin,
                reporter=reporter,
            ),
        )
    agents_ran = _run_agents(
        config,
        goal,
        [(index, action) for index, action in indexed if _is_agent(action)],
        messages,
        refs,
        db_path=_database_path(conn),
        limit=limit,
        agent_bin=agent_bin,
        reporter=reporter,
    )
    return AppliedActions([messages[index] for index, _action in indexed], agents_ran)


def _is_agent(action: object) -> bool:
    return isinstance(action, dict) and action.get("type") in _AGENT_TYPES


def _attempt(
    conn: sqlite3.Connection,
    config: Config,
    goal: Goal,
    action: dict,
    refs: dict[str, int],
    *,
    index: int,
    agent_bin: str | None,
    reporter: Callable[[str], None] | None,
) -> str:
    try:
        return _apply_one(conn, config, goal, action, refs, agent_bin=agent_bin, reporter=reporter)
    except KilnError as exc:
        return f"action {index} failed: {exc}"


def _record_action(conn: sqlite3.Connection, message: str) -> str:
    record_event(conn, "foreman.action", message)
    return message


def _database_path(conn: sqlite3.Connection) -> Path:
    row = conn.execute("PRAGMA database_list").fetchone()
    file = "" if row is None else row["file"]
    if not file:
        raise KilnError("database connection has no file")
    return Path(file)


def _run_agents(
    config: Config,
    goal: Goal,
    actions: list[tuple[int, dict]],
    messages: dict[int, str],
    refs: dict[str, int],
    *,
    db_path: Path,
    limit: int,
    agent_bin: str | None,
    reporter: Callable[[str], None] | None,
) -> int:
    pending: list[tuple[int, dict]] = []
    dispatched = 0
    holder = connect(db_path)
    try:
        migrate(holder)
        for index, action in actions:
            if action.get("type") != "dispatch":
                pending.append((index, action))
                continue
            if dispatched >= limit:
                messages[index] = _record_action(holder, "limit reached, dispatch next turn")
                continue
            dispatched += 1
            pending.append((index, action))
    finally:
        holder.close()
    if not pending:
        return 0
    reporter_lock = threading.Lock()

    def announce(message: str) -> None:
        if reporter is None:
            return
        with reporter_lock:
            reporter(message)

    def run_one(index: int, action: dict) -> str:
        local = connect(db_path)
        try:
            migrate(local)
            current = require_goal(local, goal.id)
            message = _attempt(
                local,
                config,
                current,
                action,
                refs,
                index=index,
                agent_bin=agent_bin,
                reporter=announce,
            )
            return _record_action(local, message)
        finally:
            local.close()

    if len(pending) == 1:
        index, action = pending[0]
        messages[index] = run_one(index, action)
        return 1

    slots = threading.Semaphore(max(limit, 1))
    finished = threading.Lock()

    def work(index: int, action: dict) -> None:
        try:
            with slots:
                message = run_one(index, action)
        except Exception as exc:
            message = f"action {index} failed: {exc}"
        with finished:
            messages[index] = message

    threads = [threading.Thread(target=work, args=item) for item in pending]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return len(pending)


def _apply_one(
    conn: sqlite3.Connection,
    config: Config,
    goal: Goal,
    action: dict,
    refs: dict[str, int],
    *,
    agent_bin: str | None = None,
    reporter: Callable[[str], None] | None = None,
) -> str:
    if not isinstance(action, dict):
        raise KilnError("action is not an object")
    kind = action.get("type")
    if kind == "create_task":
        return _create_task(conn, config, goal, action, refs)
    if kind == "scout":
        return _scout(conn, config, goal, action, agent_bin=agent_bin, reporter=reporter)
    if kind == "dispatch":
        return _dispatch(conn, config, goal, action, refs, agent_bin=agent_bin, reporter=reporter)
    if kind == "review":
        return _review(conn, config, goal, action, refs, agent_bin=agent_bin, reporter=reporter)
    if kind == "update_brief":
        updated = set_goal_brief(conn, goal.id, _text(action, "text"))
        return f"updated brief for goal #{updated.id}"
    if kind == "approve":
        return approve_task(conn, config, _in_goal(conn, goal, _task_id(action)))
    if kind == "rework":
        return send_back(conn, _in_goal(conn, goal, _task_id(action)), _text(action, "feedback"))
    if kind == "fail":
        task_id = _in_goal(conn, goal, _task_id(action))
        reason = _optional_text(action, "reason") or "failed by foreman"
        fail_task(conn, task_id, reason)
        return f"task #{task_id} failed"
    if kind == "cancel":
        task = cancel_task(conn, _in_goal(conn, goal, _task_id(action)))
        return f"cancelled #{task.id} {task.title}"
    if kind == "note":
        note = add_note(conn, goal.id, _text(action, "text"))
        return f"note #{note.id}"
    if kind == "goal_done":
        return _finish_goal(conn, goal, action)
    raise KilnError(f"unknown action type {kind!r}")


def _create_task(
    conn: sqlite3.Connection,
    config: Config,
    goal: Goal,
    action: dict,
    refs: dict[str, int],
) -> str:
    title = _text(action, "title")
    task = add_task(
        conn,
        goal.id,
        title,
        description=_optional_text(action, "description"),
        acceptance=_optional_text(action, "acceptance"),
        priority=_priority(action),
        max_attempts=config.max_attempts,
    )
    ref = action.get("ref")
    if isinstance(ref, str) and ref.strip():
        refs[ref.strip()] = task.id
    refs.setdefault(task.title, task.id)
    depends_on = action.get("depends_on") or []
    if not isinstance(depends_on, list):
        raise KilnError("depends_on must be a list")
    linked: list[int] = []
    for dep in depends_on:
        try:
            dep_id = _resolve_dep(conn, goal.id, dep, refs)
            if dep_id == task.id:
                raise KilnError(f"task #{task.id} cannot depend on itself")
            add_dependency(conn, task.id, dep_id)
        except KilnError as exc:
            return f"created task #{task.id} {task.title}; dependency failed: {exc}"
        linked.append(dep_id)
    if linked:
        deps = ", ".join(f"#{dep_id}" for dep_id in linked)
        return f"created task #{task.id} {task.title} depending on {deps}"
    return f"created task #{task.id} {task.title}"


def _scout(
    conn: sqlite3.Connection,
    config: Config,
    goal: Goal,
    action: dict,
    *,
    agent_bin: str | None,
    reporter: Callable[[str], None] | None,
) -> str:
    outcome = run_scout(
        conn,
        config,
        _text(action, "question"),
        goal_id=goal.id,
        agent_bin=agent_bin,
        reporter=reporter,
    )
    if outcome.note is None:
        return f"scout failed: {outcome.failure}"
    return f"scout note #{outcome.note.id} on goal #{goal.id}"


def _dispatch(
    conn: sqlite3.Connection,
    config: Config,
    goal: Goal,
    action: dict,
    refs: dict[str, int],
    *,
    agent_bin: str | None,
    reporter: Callable[[str], None] | None,
) -> str:
    task_id = _resolve_task_ref(conn, goal, action, refs)
    task = require_task(conn, task_id)
    if task.status != TaskStatus.pending:
        raise KilnError(f"task #{task_id} is {task.status.value}; only a pending task can be dispatched")
    if task.id not in {ready.id for ready in ready_tasks(conn, goal_id=goal.id)}:
        raise KilnError(f"task #{task_id} is blocked")
    outcome = run_worker(conn, config, task_id=task_id, agent_bin=agent_bin, reporter=reporter)
    branch = outcome.task.branch or ""
    line = f"task #{outcome.task.id}  {outcome.task.status.value}  {branch}"
    if outcome.failure:
        return f"{line}: {outcome.failure}"
    return line


def _review(
    conn: sqlite3.Connection,
    config: Config,
    goal: Goal,
    action: dict,
    refs: dict[str, int],
    *,
    agent_bin: str | None,
    reporter: Callable[[str], None] | None,
) -> str:
    task_id = _resolve_task_ref(conn, goal, action, refs)
    outcome = run_reviewer(
        conn,
        config,
        task_id,
        focus=_optional_text(action, "focus"),
        agent_bin=agent_bin,
        reporter=reporter,
    )
    if outcome.failure:
        return f"review failed: {outcome.failure}"
    return f"reviewed #{task_id}: {outcome.verdict}"


def _finish_goal(conn: sqlite3.Connection, goal: Goal, action: dict) -> str:
    open_tasks = [task for task in list_tasks(conn, goal_id=goal.id) if task.status in _OPEN]
    if open_tasks:
        ids = ", ".join(f"#{task.id}" for task in open_tasks)
        raise KilnError(f"goal #{goal.id} still has open tasks: {ids}")
    evidence = action.get("evidence")
    if not isinstance(evidence, list):
        raise KilnError("goal_done requires evidence")
    set_goal_evidence(conn, goal.id, evidence)
    set_goal_status(conn, goal.id, GoalStatus.done)
    return f"goal #{goal.id} done"


def _resolve_task_ref(conn: sqlite3.Connection, goal: Goal, action: dict, refs: dict[str, int]) -> int:
    if "task_id" in action and action.get("task_id") is not None:
        return _in_goal(conn, goal, _task_id(action))
    ref = action.get("ref")
    if isinstance(ref, str) and ref.strip() in refs:
        return _in_goal(conn, goal, refs[ref.strip()])
    raise KilnError("action needs a task_id or ref")


def _availability(task: Task, deps: list[Task], ready_ids: set[int]) -> str:
    if task.status != TaskStatus.pending:
        return ""
    if task.id in ready_ids:
        return "ready "
    waiting = [dep.id for dep in deps if dep.status != TaskStatus.done]
    if not waiting:
        return "blocked "
    return "blocked by " + ", ".join(f"#{dep_id}" for dep_id in waiting) + " "


def _resolve_dep(conn: sqlite3.Connection, goal_id: int, dep: object, refs: dict[str, int]) -> int:
    if isinstance(dep, bool) or dep is None:
        raise KilnError(f"bad dependency {dep!r}")
    if isinstance(dep, int):
        return _task_in_goal(conn, goal_id, dep)
    if not isinstance(dep, str) or not dep.strip():
        raise KilnError(f"bad dependency {dep!r}")
    text = dep.strip()
    if text.startswith("#") and text[1:].isdigit():
        text = text[1:]
    if text.isdigit():
        return _task_in_goal(conn, goal_id, int(text))
    if text in refs:
        return _task_in_goal(conn, goal_id, refs[text])
    matches = [task for task in list_tasks(conn, goal_id=goal_id) if task.title == text]
    if len(matches) == 1:
        return matches[0].id
    if len(matches) > 1:
        raise KilnError(f"title {text!r} matches more than one task")
    raise KilnError(f"unknown dependency {dep!r}")


def _in_goal(conn: sqlite3.Connection, goal: Goal, task_id: int) -> int:
    return _task_in_goal(conn, goal.id, task_id)


def _task_in_goal(conn: sqlite3.Connection, goal_id: int, task_id: int) -> int:
    task = require_task(conn, task_id)
    if task.goal_id != goal_id:
        raise KilnError(f"task #{task_id} is not in goal #{goal_id}")
    return task.id


def _review_lines(conn: sqlite3.Connection, config: Config, goal: Goal, task: Task) -> list[str]:
    lines: list[str] = []
    if task.branch:
        try:
            base = goal.branch or config.base_branch
            changes = diffstat(config.repo_root, base, task.branch)
        except KilnError as exc:
            changes = str(exc)
        lines.append("  diffstat:")
        lines.append(changes or "  (no changes)")
    worker = latest_run(conn, task.id, "worker")
    if worker is not None and worker.report_json:
        lines.extend(_worker_report_lines(worker.report_json))
    review = latest_run(conn, task.id, "reviewer")
    if review is not None and review.report_json and (worker is None or review.id > worker.id):
        lines.extend(_reviewer_report_lines(review.report_json))
    return lines


def _worker_report_lines(raw: str) -> list[str]:
    try:
        report = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(report, dict):
        return []
    lines: list[str] = []
    worker = report.get("worker")
    if isinstance(worker, dict) and isinstance(worker.get("summary"), str):
        lines.append(f"  summary: {worker['summary']}")
    verify = report.get("verify")
    if isinstance(verify, dict) and verify.get("command"):
        code = verify.get("exit_code")
        output = verify.get("output") or ""
        if not isinstance(output, str):
            output = str(output)
        tail = "\n".join(output.splitlines()[-20:])
        lines.append(f"  verify exit {code}")
        if tail:
            lines.append(tail)
    return lines


def _reviewer_report_lines(raw: str) -> list[str]:
    try:
        report = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(report, dict):
        return []
    verdict = report.get("verdict")
    summary = report.get("summary")
    if not isinstance(verdict, str) or not isinstance(summary, str):
        return []
    lines = [f"  review: {verdict} — {summary}"]
    findings = report.get("findings")
    if isinstance(findings, list):
        for item in findings:
            if isinstance(item, str) and item.strip():
                lines.append(f"  - {item.strip()}")
    return lines


def _failure(result) -> str | None:
    if result.timed_out:
        return "timed out"
    if result.exit_code != 0:
        return f"agent exited {result.exit_code}"
    if result.parsed.is_error:
        return "agent reported an error"
    if result.report is None:
        return "response had no JSON report"
    return None


def _text(action: dict, key: str) -> str:
    value = _optional_text(action, key)
    if not value:
        raise KilnError(f"{key} is required")
    return value


def _optional_text(action: dict, key: str) -> str:
    value = action.get(key, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise KilnError(f"{key} must be a string")
    return value.strip()


def _priority(action: dict) -> int:
    value = action.get("priority", 0)
    if isinstance(value, bool) or not isinstance(value, int):
        raise KilnError("priority must be an integer")
    return value


def _task_id(action: dict) -> int:
    value = action.get("task_id")
    if isinstance(value, bool) or not isinstance(value, int):
        raise KilnError("task_id must be an integer")
    return value
