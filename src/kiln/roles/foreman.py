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
from kiln.git import goal_branch_name
from kiln.jobs import (
    add_dependency,
    add_job,
    cancel_job,
    dependencies,
    dependency_satisfied,
    get_job,
    is_open,
    list_jobs,
    ready_jobs,
    reject_job,
    require_goal,
    require_job,
    set_goal_brief,
    set_goal_evidence,
    set_goal_status,
)
from kiln.models import Goal, GoalStatus, Integration, Job, JobRole, JobStatus, Run, RunStatus
from kiln.notes import add_note, list_notes
from kiln.prompts import render_prompt
from kiln.review import approve_task, send_back
from kiln.roles.reviewer import run_reviewer
from kiln.roles.scout import run_scout
from kiln.roles.worker import run_worker
from kiln.result import role_result
from kiln.runs import finish_run, start_run

_AGENT_TYPES = frozenset({"dispatch"})
_DECISIONS = frozenset({"approve", "reject"})
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
            "Jobs:",
        ]
    )
    tasks = list_jobs(conn, goal_id=goal.id)
    ready_ids = {task.id for task in ready_jobs(conn, goal_id=goal.id)}
    if not tasks:
        lines.append("(none)")
    for task in tasks:
        deps = dependencies(conn, task.id)
        dep_text = ", ".join(f"#{dep.id} {dep.title}" for dep in deps) or "-"
        availability = _availability(task, deps, ready_ids)
        integration = f"integration {task.integration.value} " if task.integration else ""
        target = f" target #{task.target_job_id}" if task.target_job_id else ""
        lines.append(
            f"- #{task.id} [{task.status.value}] {integration}{availability}{task.role.value} "
            f"p{task.priority} attempts {task.attempts}/{task.max_attempts} "
            f"deps {dep_text}{target}: {task.title}"
        )
        if task.question:
            lines.append(f"  question: {task.question}")
        if task.focus:
            lines.append(f"  focus: {task.focus}")
        if task.feedback:
            lines.append(f"  feedback: {task.feedback}")
        lines.extend(_result_lines(task.result))
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

    State changes run first. Dispatches then run together, and only when an action asks for them.
    """
    refs: dict[str, int] = {}
    for task in list_jobs(conn, goal_id=goal.id):
        refs.setdefault(task.title, task.id)
    limit = config.max_parallel_workers if worker_limit is None else worker_limit
    indexed = list(enumerate(actions, start=1))
    created = _jobs_created_in_list(actions)
    conflicts = _same_turn_conflicts(conn, actions, created, refs)
    messages: dict[int, str] = {}
    agents: list[tuple[int, dict]] = []
    for index, action in indexed:
        refusal = _same_turn_refusal(action, created, refs, conflicts, conn)
        if refusal:
            messages[index] = _record_action(conn, f"action {index} failed: {refusal}")
            continue
        if _is_agent(action):
            agents.append((index, action))
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
        agents,
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


def _same_turn_conflicts(
    conn: sqlite3.Connection,
    actions: list[dict],
    created: dict[str, tuple[str, object | None]],
    refs: dict[str, int],
) -> set[object]:
    """Worker jobs this list both sends a reviewer after and approves or rejects.

    Approve and reject run before dispatch, so that reviewer's verdict cannot inform them.
    Rework stays out of the guard: it keeps the worktree for another attempt.
    """
    targets: set[object] = set()
    decided: set[object] = set()
    for action in actions:
        if not isinstance(action, dict):
            continue
        kind = action.get("type")
        if kind == "dispatch":
            target = _dispatched_review_target(conn, action, created, refs)
            if target is not None:
                targets.add(target)
        elif kind in _DECISIONS:
            key = _decision_key(action, created, refs)
            if key is not None:
                decided.add(key)
    return targets & decided


def _jobs_created_in_list(actions: list[dict]) -> dict[str, tuple[str, object | None]]:
    """Map each create_job ref to its role and, for a reviewer, the target key."""
    named: list[tuple[str, dict]] = []
    for action in actions:
        if not isinstance(action, dict) or action.get("type") != "create_job":
            continue
        ref = action.get("ref")
        if isinstance(ref, str) and ref.strip():
            named.append((ref.strip(), action))
    created: dict[str, tuple[str, object | None]] = {}
    for name, action in named:
        role = action.get("role")
        role_name = role.strip() if isinstance(role, str) else ""
        target = _target_key(action.get("target_job_id")) if role_name == "reviewer" else None
        created[name] = (role_name, target)
    return created


def _target_key(value: object) -> object | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.startswith("#") and text[1:].isdigit():
        return int(text[1:])
    if text.isdigit():
        return int(text)
    return ("ref", text)


def _dispatched_review_target(
    conn: sqlite3.Connection,
    action: dict,
    created: dict[str, tuple[str, object | None]],
    refs: dict[str, int],
) -> object | None:
    if "job_id" in action and action.get("job_id") is not None:
        value = action.get("job_id")
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        job = get_job(conn, value)
        if job is not None and job.role == JobRole.reviewer and job.target_job_id is not None:
            return job.target_job_id
        return None
    ref = action.get("ref")
    if not isinstance(ref, str) or not ref.strip():
        return None
    name = ref.strip()
    if name in created:
        role, target = created[name]
        if role == "reviewer":
            return target
        return None
    if name in refs:
        job = get_job(conn, refs[name])
        if job is not None and job.role == JobRole.reviewer and job.target_job_id is not None:
            return job.target_job_id
    return None


def _decision_key(
    action: dict,
    created: dict[str, tuple[str, object | None]],
    refs: dict[str, int],
) -> object | None:
    if "job_id" in action and action.get("job_id") is not None:
        value = action.get("job_id")
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value
    ref = action.get("ref")
    if not isinstance(ref, str) or not ref.strip():
        return None
    name = ref.strip()
    if name in created:
        return ("ref", name)
    if name in refs:
        return refs[name]
    return ("ref", name)


def _same_turn_refusal(
    action: object,
    created: dict[str, tuple[str, object | None]],
    refs: dict[str, int],
    conflicts: set[object],
    conn: sqlite3.Connection,
) -> str | None:
    if not isinstance(action, dict):
        return None
    kind = action.get("type")
    key: object | None = None
    if kind == "dispatch":
        key = _dispatched_review_target(conn, action, created, refs)
    elif kind in _DECISIONS:
        key = _decision_key(action, created, refs)
    if key is None or key not in conflicts:
        return None
    label = f"#{key}" if isinstance(key, int) else key[1]
    return f"cannot dispatch a reviewer and approve or reject job {label} in one turn"


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
    if kind == "create_job":
        return _create_job(conn, config, goal, action, refs)
    if kind == "dispatch":
        return _dispatch(conn, config, goal, action, refs, agent_bin=agent_bin, reporter=reporter)
    if kind == "update_brief":
        updated = set_goal_brief(conn, goal.id, _text(action, "text"))
        return f"updated brief for goal #{updated.id}"
    if kind == "approve":
        return approve_task(conn, config, _in_goal(conn, goal, _resolve_job_ref(conn, goal, action, refs)))
    if kind == "reject":
        job_id = _in_goal(conn, goal, _resolve_job_ref(conn, goal, action, refs))
        reason = _optional_text(action, "reason") or "rejected"
        reject_job(conn, job_id, reason)
        return f"job #{job_id} rejected"
    if kind == "rework":
        return send_back(conn, _in_goal(conn, goal, _resolve_job_ref(conn, goal, action, refs)), _text(action, "feedback"))
    if kind == "cancel":
        task = cancel_job(conn, _in_goal(conn, goal, _resolve_job_ref(conn, goal, action, refs)))
        return f"cancelled #{task.id} {task.title}"
    if kind == "note":
        note = add_note(conn, goal.id, _text(action, "text"))
        return f"note #{note.id}"
    if kind == "goal_done":
        return _finish_goal(conn, goal, action)
    raise KilnError(f"unknown action type {kind!r}")


def _create_job(
    conn: sqlite3.Connection,
    config: Config,
    goal: Goal,
    action: dict,
    refs: dict[str, int],
) -> str:
    role = _role(action)
    title = _text(action, "title")
    question = ""
    focus = ""
    acceptance = ""
    target_id: int | None = None
    depends_on = action.get("depends_on") or []
    if not isinstance(depends_on, list):
        raise KilnError("depends_on must be a list")
    if role == JobRole.scout:
        question = _text(action, "question")
    elif role == JobRole.worker:
        acceptance = _optional_text(action, "acceptance")
    else:
        focus = _optional_text(action, "focus")
        if "target_job_id" not in action or action.get("target_job_id") is None:
            raise KilnError("reviewer job requires a target_job_id")
        target_id = _resolve_dep(conn, goal.id, action.get("target_job_id"), refs)
    description = _optional_text(action, "description")
    if role == JobRole.reviewer:
        linked = [_resolve_dep(conn, goal.id, dep, refs) for dep in depends_on]
        if target_id not in linked:
            raise KilnError("reviewer target must be listed in depends_on")
        job = add_job(
            conn,
            goal.id,
            title,
            role=role,
            description=description,
            focus=focus,
            target_job_id=target_id,
            depends_on=linked,
            priority=_priority(action),
            max_attempts=config.max_attempts,
        )
        _remember(refs, action, job.id, job.title)
        deps = ", ".join(f"#{dep_id}" for dep_id in linked)
        return f"created job #{job.id} {job.title} depending on {deps}"
    job = add_job(
        conn,
        goal.id,
        title,
        role=role,
        description=description,
        acceptance=acceptance,
        question=question,
        priority=_priority(action),
        max_attempts=config.max_attempts,
    )
    _remember(refs, action, job.id, job.title)
    linked: list[int] = []
    for dep in depends_on:
        try:
            dep_id = _resolve_dep(conn, goal.id, dep, refs)
            if dep_id == job.id:
                raise KilnError(f"job #{job.id} cannot depend on itself")
            add_dependency(conn, job.id, dep_id)
        except KilnError as exc:
            return f"created job #{job.id} {job.title}; dependency failed: {exc}"
        linked.append(dep_id)
    if linked:
        deps = ", ".join(f"#{dep_id}" for dep_id in linked)
        return f"created job #{job.id} {job.title} depending on {deps}"
    return f"created job #{job.id} {job.title}"


def _remember(refs: dict[str, int], action: dict, job_id: int, title: str) -> None:
    ref = action.get("ref")
    if isinstance(ref, str) and ref.strip():
        refs[ref.strip()] = job_id
    refs.setdefault(title, job_id)


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
    job_id = _resolve_job_ref(conn, goal, action, refs)
    job = require_job(conn, job_id)
    if job.status != JobStatus.pending:
        raise KilnError(f"job #{job_id} is {job.status.value}; only a pending job can be dispatched")
    if job.id not in {ready.id for ready in ready_jobs(conn, goal_id=goal.id)}:
        raise KilnError(f"job #{job_id} is blocked")
    if job.role == JobRole.worker:
        outcome = run_worker(conn, config, task_id=job_id, agent_bin=agent_bin, reporter=reporter)
        branch = outcome.job.branch or ""
        line = f"job #{outcome.job.id}  {outcome.job.status.value}  {branch}"
        if outcome.failure:
            return f"{line}: {outcome.failure}"
        return line
    if job.role == JobRole.scout:
        outcome = run_scout(conn, config, job_id=job.id, agent_bin=agent_bin, reporter=reporter)
        if outcome.failure or outcome.job.status != JobStatus.completed:
            return f"scout job #{outcome.job.id} failed: {outcome.failure}"
        return f"scout job #{outcome.job.id} completed"
    target = require_job(conn, job.target_job_id) if job.target_job_id is not None else None
    if not (
        target is not None
        and target.role == JobRole.worker
        and target.status == JobStatus.completed
        and target.integration == Integration.pending
        and target.branch
    ):
        state = "missing" if target is None else target.status.value
        integration = "none" if target is None or target.integration is None else target.integration.value
        raise KilnError(
            f"job #{job.target_job_id} is {state} integration {integration}; "
            "only a completed worker with pending integration can be reviewed"
        )
    outcome = run_reviewer(conn, config, target.id, job_id=job.id, agent_bin=agent_bin, reporter=reporter)
    if outcome.failure:
        return f"review failed: {outcome.failure}"
    return f"reviewed #{target.id}: {outcome.verdict}"


def _finish_goal(conn: sqlite3.Connection, goal: Goal, action: dict) -> str:
    open_jobs = [task for task in list_jobs(conn, goal_id=goal.id) if is_open(task)]
    if open_jobs:
        ids = ", ".join(f"#{task.id}" for task in open_jobs)
        raise KilnError(f"goal #{goal.id} still has open jobs: {ids}")
    evidence = action.get("evidence")
    if not isinstance(evidence, list):
        raise KilnError("goal_done requires evidence")
    set_goal_evidence(conn, goal.id, evidence)
    set_goal_status(conn, goal.id, GoalStatus.done)
    return f"goal #{goal.id} done"


def _resolve_job_ref(conn: sqlite3.Connection, goal: Goal, action: dict, refs: dict[str, int]) -> int:
    if "job_id" in action and action.get("job_id") is not None:
        return _in_goal(conn, goal, _job_id(action))
    ref = action.get("ref")
    if isinstance(ref, str) and ref.strip() in refs:
        return _in_goal(conn, goal, refs[ref.strip()])
    raise KilnError("action needs a job_id or ref")


def _availability(task: Job, deps: list[Job], ready_ids: set[int]) -> str:
    if task.status != JobStatus.pending:
        return ""
    if task.id in ready_ids:
        return "ready "
    waiting = [dep.id for dep in deps if not dependency_satisfied(dep, for_role=task.role)]
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
    matches = [task for task in list_jobs(conn, goal_id=goal_id) if task.title == text]
    if len(matches) == 1:
        return matches[0].id
    if len(matches) > 1:
        raise KilnError(f"title {text!r} matches more than one job")
    raise KilnError(f"unknown dependency {dep!r}")


def _in_goal(conn: sqlite3.Connection, goal: Goal, task_id: int) -> int:
    return _task_in_goal(conn, goal.id, task_id)


def _task_in_goal(conn: sqlite3.Connection, goal_id: int, task_id: int) -> int:
    task = require_job(conn, task_id)
    if task.goal_id != goal_id:
        raise KilnError(f"job #{task_id} is not in goal #{goal_id}")
    return task.id


def _result_lines(raw: str | None) -> list[str]:
    report = _object(raw)
    if report is None:
        return []
    lines: list[str] = []
    summary = report.get("summary")
    if isinstance(summary, str) and summary.strip():
        lines.append(f"  result: {summary.strip()}")
    evidence = report.get("evidence")
    if isinstance(evidence, list):
        for item in evidence:
            if isinstance(item, str) and item.strip():
                lines.append(f"  evidence: {item.strip()}")
    verdict = role_result(report).get("verdict")
    if isinstance(verdict, str) and verdict.strip():
        lines.append(f"  verdict: {verdict.strip()}")
    return lines


def _object(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        report = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(report, dict):
        return None
    return report


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


def _role(action: dict) -> JobRole:
    text = _text(action, "role")
    try:
        return JobRole(text)
    except ValueError:
        raise KilnError("role must be scout, worker, or reviewer") from None


def _job_id(action: dict) -> int:
    value = action.get("job_id")
    if isinstance(value, bool) or not isinstance(value, int):
        raise KilnError("job_id must be an integer")
    return value
