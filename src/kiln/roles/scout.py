import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from kiln.agent import DEFAULT_TIMEOUT_SECONDS, AgentResult, run_agent
from kiln.config import Config
from kiln.db import record_event
from kiln.errors import KilnError
from kiln.jobs import (
    add_job,
    claim_job,
    complete_job,
    fail_job,
    list_goals,
    require_goal,
    require_job,
    start_execution,
)
from kiln.models import Goal, GoalStatus, Job, JobRole, JobStatus, Run, RunStatus
from kiln.prompts import render_prompt
from kiln.result import envelope
from kiln.runs import finish_run, start_run


@dataclass(frozen=True)
class ScoutOutcome:
    goal: Goal
    job: Job
    run: Run
    failure: str | None
    summary: str


def run_scout(
    conn: sqlite3.Connection,
    config: Config,
    question: str = "",
    *,
    job_id: int | None = None,
    goal_id: int | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    agent_bin: str | None = None,
    reporter: Callable[[str], None] | None = None,
) -> ScoutOutcome:
    """Run a scout job read-only and store the report as its result.

    Without job_id, this creates the job. Dispatch passes job_id so the existing job runs.
    """
    if job_id is None:
        question = question.strip()
        if not question:
            raise KilnError("scout question cannot be empty")
        goal = _resolve_goal(conn, goal_id)
        job = add_job(
            conn,
            goal.id,
            question,
            role=JobRole.scout,
            question=question,
            max_attempts=config.max_attempts,
        )
    else:
        job = require_job(conn, job_id)
        if job.role != JobRole.scout:
            raise KilnError(f"job #{job.id} is a {job.role.value}; only a scout job can run here")
        if job.status != JobStatus.pending:
            raise KilnError(f"job #{job.id} is {job.status.value}; only a pending job can be dispatched")
        question = job.question.strip()
        if not question:
            raise KilnError("scout question cannot be empty")
        goal = require_goal(conn, job.goal_id)
    claimed = claim_job(conn, job.id, f"kiln-scout-{os.getpid()}")
    if claimed is None:
        raise KilnError(f"job #{job.id} is not ready")
    job = start_execution(conn, claimed.id)
    run = start_run(conn, role="scout", model=config.models.scout, job_id=job.id)
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
    except KilnError as exc:
        finished = finish_run(
            conn,
            run.id,
            status=RunStatus.failed,
            exit_code=None,
            log_path=None,
            report=None,
        )
        fail_job(conn, job.id, str(exc))
        record_event(conn, "scout.failed", str(exc), job_id=job.id, run_id=finished.id)
        raise

    failure = _failure(result, timeout)
    report = _validated(result.report) if failure is None else None
    if failure is None and isinstance(report, str):
        failure = report
        report = None
    if failure or not isinstance(report, dict):
        finished = finish_run(
            conn,
            run.id,
            status=RunStatus.failed,
            exit_code=result.exit_code,
            log_path=str(result.log_path),
            report=None,
        )
        reason = failure or "response had no JSON report"
        failed = fail_job(conn, job.id, reason)
        record_event(conn, "scout.failed", reason, job_id=job.id, run_id=finished.id)
        return ScoutOutcome(goal=goal, job=failed, run=finished, failure=reason, summary="")

    stored = envelope(
        summary=report["summary"],
        evidence=list(report["findings"]),
        role_result=report,
    )
    finished = finish_run(
        conn,
        run.id,
        status=RunStatus.succeeded,
        exit_code=result.exit_code,
        log_path=str(result.log_path),
        report=stored,
    )
    completed = complete_job(conn, job.id, stored)
    record_event(
        conn,
        "scout.completed",
        f"job #{completed.id} completed",
        job_id=completed.id,
        run_id=finished.id,
    )
    return ScoutOutcome(goal=goal, job=completed, run=finished, failure=None, summary=report["summary"])


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


def _validated(report: dict | None) -> dict | str:
    if not isinstance(report, dict):
        return "response had no JSON report"
    summary = report.get("summary")
    findings = report.get("findings", [])
    if not isinstance(summary, str) or not summary.strip():
        return "scout summary is required"
    if not isinstance(findings, list) or any(not isinstance(item, str) for item in findings):
        return "scout findings must be a list of strings"
    return {"summary": summary.strip(), "findings": findings}
