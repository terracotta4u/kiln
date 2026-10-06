import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from kiln.agent import DEFAULT_TIMEOUT_SECONDS, AgentResult, run_agent
from kiln.config import Config
from kiln.db import record_event
from kiln.errors import KilnError
from kiln.models import Goal, GoalStatus, Note, Run, RunStatus
from kiln.notes import add_note
from kiln.prompts import render_prompt
from kiln.runs import finish_run, start_run
from kiln.jobs import list_goals, require_goal


@dataclass(frozen=True)
class ScoutOutcome:
    goal: Goal
    run: Run
    note: Note | None
    failure: str | None


def run_scout(
    conn: sqlite3.Connection,
    config: Config,
    question: str,
    *,
    goal_id: int | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    agent_bin: str | None = None,
    reporter: Callable[[str], None] | None = None,
) -> ScoutOutcome:
    question = question.strip()
    if not question:
        raise KilnError("scout question cannot be empty")
    goal = _resolve_goal(conn, goal_id)
    run = start_run(conn, role="scout", model=config.models.scout)
    if reporter:
        reporter(f"scouting goal #{goal.id} with {config.models.scout}")
    log_path = config.runs_dir / f"{run.id}.log"
    prompt = render_prompt(
        "scout.md",
        {
            "repo_root": str(config.repo_root),
            "goal_id": str(goal.id),
            "goal_title": goal.title,
            "goal_description": goal.description or "(none)",
            "question": question,
        },
    )
    try:
        result = run_agent(
            prompt=prompt,
            model=config.models.scout,
            workspace=config.repo_root,
            log_path=log_path,
            mode="ask",
            timeout=timeout,
            agent_bin=agent_bin,
        )
    except KilnError:
        finish_run(
            conn,
            run.id,
            status=RunStatus.failed,
            exit_code=None,
            log_path=None,
            report=None,
        )
        record_event(conn, "scout.failed", "agent executable not found", run_id=run.id)
        raise

    failure = _failure(result, timeout)
    finished = finish_run(
        conn,
        run.id,
        status=RunStatus.failed if failure else RunStatus.succeeded,
        exit_code=result.exit_code,
        log_path=str(result.log_path),
        report=result.report,
    )
    if failure:
        record_event(conn, "scout.failed", failure, run_id=finished.id)
        return ScoutOutcome(goal=goal, run=finished, note=None, failure=failure)

    assert result.report is not None
    note = add_note(conn, goal.id, _note_text(question, result.report))
    record_event(
        conn,
        "scout.completed",
        f"note #{note.id} on goal #{goal.id}",
        run_id=finished.id,
    )
    return ScoutOutcome(goal=goal, run=finished, note=note, failure=None)


def _resolve_goal(conn: sqlite3.Connection, goal_id: int | None) -> Goal:
    if goal_id is not None:
        return require_goal(conn, goal_id)
    active = list_goals(conn, status=GoalStatus.active)
    if len(active) == 1:
        return active[0]
    if not active:
        raise KilnError("no active goal; add one with `kiln goal add`")
    raise KilnError("more than one active goal; pass --goal")


def _failure(result: AgentResult, timeout: float) -> str | None:
    if result.timed_out:
        return f"timed out after {timeout:g}s"
    if result.exit_code != 0:
        return f"agent exited {result.exit_code}"
    if result.parsed.is_error:
        return "agent reported an error"
    if result.report is None:
        return "response had no JSON report"
    return None


def _note_text(question: str, report: dict) -> str:
    summary = report.get("summary")
    lines = [f"Question: {question}", ""]
    if isinstance(summary, str) and summary.strip():
        lines.append(summary.strip())
    else:
        lines.append("Scout returned a report without a summary.")
    findings = report.get("findings")
    if isinstance(findings, list) and findings:
        lines.append("")
        lines.append("Findings:")
        for item in findings:
            lines.append(f"- {item}")
    return "\n".join(lines)
