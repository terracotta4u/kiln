"""Independent read-only review of a worker branch."""

import json
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from kiln.agent import DEFAULT_TIMEOUT_SECONDS, AgentResult, run_agent
from kiln.config import Config
from kiln.db import record_event
from kiln.errors import KilnError
from kiln.git import branch_diff, ensure_worktree
from kiln.jobs import (
    add_job,
    claim_job,
    complete_job,
    fail_job,
    require_goal,
    require_job,
    start_execution,
)
from kiln.models import Integration, Job, JobRole, JobStatus, Run, RunStatus
from kiln.prompts import render_prompt
from kiln.publish import ensure_goal_branch
from kiln.result import envelope, role_result
from kiln.runs import finish_run, latest_run, start_run

DIFF_CHAR_LIMIT = 12_000
_VERDICTS = {"approve", "needs_changes", "reject"}


@dataclass(frozen=True)
class ReviewOutcome:
    job: Job
    run: Run
    failure: str | None
    verdict: str | None
    summary: str


def run_reviewer(
    conn: sqlite3.Connection,
    config: Config,
    worker_id: int | None = None,
    *,
    job_id: int | None = None,
    focus: str = "",
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    agent_bin: str | None = None,
    reporter: Callable[[str], None] | None = None,
) -> ReviewOutcome:
    """Review a completed worker. The verdict is stored on the reviewer job and leaves the worker alone.

    Without job_id, this creates the reviewer job. Dispatch passes job_id so the existing job runs.
    """
    review: Job | None = None
    if job_id is not None:
        review = require_job(conn, job_id)
        if review.role != JobRole.reviewer or review.target_job_id is None:
            raise KilnError(f"job #{job_id} is not a reviewer job")
        if review.status != JobStatus.pending:
            raise KilnError(
                f"job #{review.id} is {review.status.value}; only a pending job can be dispatched"
            )
        worker = require_job(conn, review.target_job_id)
        focus = review.focus or focus
    else:
        if worker_id is None:
            raise KilnError("a reviewer needs a worker to review")
        worker = require_job(conn, worker_id)
    if not (
        worker.role == JobRole.worker
        and worker.status == JobStatus.completed
        and worker.integration == Integration.pending
    ):
        raise KilnError(
            f"job #{worker.id} is {worker.status.value}; only a completed worker with pending integration can be reviewed"
        )
    if not worker.branch:
        raise KilnError(f"job #{worker.id} has no branch to review")
    if review is None:
        review = add_job(
            conn,
            worker.goal_id,
            f"Review {worker.title}",
            role=JobRole.reviewer,
            focus=focus.strip(),
            target_job_id=worker.id,
            depends_on=[worker.id],
            max_attempts=config.max_attempts,
        )
    claimed = claim_job(conn, review.id, f"kiln-reviewer-{os.getpid()}")
    if claimed is None:
        raise KilnError(f"job #{review.id} is not ready")
    review = start_execution(conn, claimed.id)
    goal = require_goal(conn, worker.goal_id)
    integration, _ = ensure_goal_branch(conn, config, goal)
    worktree = Path(worker.worktree_path) if worker.worktree_path else config.worktrees_dir / str(worker.id)
    ensure_worktree(config.repo_root, worktree, worker.branch, integration)

    run = start_run(conn, role="reviewer", model=config.models.reviewer, job_id=review.id)
    if reporter:
        reporter(f"reviewing job #{review.id} target #{worker.id} with {config.models.reviewer}")
    log_path = config.runs_dir / f"{run.id}.log"
    prompt = render_prompt(
        "reviewer.md",
        {
            "repo_root": str(config.repo_root),
            "goal_id": str(goal.id),
            "goal_title": goal.title,
            "goal_description": goal.description or "(none)",
            "brief": goal.brief or "(none)",
            "job_id": str(worker.id),
            "title": worker.title,
            "description": worker.description or "(none)",
            "acceptance": worker.acceptance or "(none)",
            "focus": focus.strip() or "(none)",
            "verify": _verify_text(conn, worker.id),
            "integration_branch": integration,
            "diff": _capped_diff(config.repo_root, integration, worker.branch),
        },
    )
    try:
        result = run_agent(
            prompt=prompt,
            model=config.models.reviewer,
            workspace=worktree,
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
        fail_job(conn, review.id, str(exc))
        record_event(conn, "review.failed", str(exc), job_id=review.id, run_id=finished.id)
        raise

    failure = _failure(result, timeout)
    report = _validated(result.report) if failure is None else None
    if failure is None and isinstance(report, str):
        failure = report
        report = None
    if failure or not isinstance(report, dict):
        reason = failure or "response had no review"
        finished = finish_run(
            conn,
            run.id,
            status=RunStatus.failed,
            exit_code=result.exit_code,
            log_path=str(result.log_path),
            report=None,
        )
        failed = fail_job(conn, review.id, reason)
        record_event(conn, "review.failed", reason, job_id=review.id, run_id=finished.id)
        return ReviewOutcome(job=failed, run=finished, failure=reason, verdict=None, summary="")
    stored = envelope(
        summary=report["summary"],
        evidence=list(report["findings"]),
        artifacts=[{"type": "branch", "name": worker.branch}],
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
    completed = complete_job(conn, review.id, stored)
    record_event(
        conn,
        "review.completed",
        f"{report['verdict']}: {report['summary']}",
        job_id=completed.id,
        run_id=finished.id,
    )
    return ReviewOutcome(
        job=completed,
        run=finished,
        failure=None,
        verdict=str(report["verdict"]),
        summary=str(report["summary"]),
    )


def _capped_diff(repo: Path, base: str, branch: str) -> str:
    text = branch_diff(repo, base, branch)
    if not text.strip():
        return "(no changes)"
    if len(text) <= DIFF_CHAR_LIMIT:
        return text
    omitted = len(text) - DIFF_CHAR_LIMIT
    return text[:DIFF_CHAR_LIMIT] + f"\n\n[diff truncated, {omitted} characters omitted]"


def _verify_text(conn: sqlite3.Connection, job_id: int) -> str:
    run = latest_run(conn, job_id, "worker")
    if run is None or not run.report_json:
        return "(none)"
    try:
        report = json.loads(run.report_json)
    except json.JSONDecodeError:
        return "(none)"
    if not isinstance(report, dict):
        return "(none)"
    verify = role_result(report).get("verify")
    if not isinstance(verify, dict) or not verify.get("command"):
        return "(none)"
    output = verify.get("output") or ""
    if not isinstance(output, str):
        output = str(output)
    return f"command: {verify['command']}\nexit: {verify.get('exit_code')}\n{output}".rstrip()


def _validated(report: dict | None) -> dict | str:
    if not isinstance(report, dict):
        return "response had no review"
    verdict = report.get("verdict")
    summary = report.get("summary")
    findings = report.get("findings", [])
    confidence = report.get("confidence")
    if verdict not in _VERDICTS:
        return "review verdict must be approve, needs_changes, or reject"
    if not isinstance(summary, str) or not summary.strip():
        return "review summary is required"
    if not isinstance(findings, list) or any(not isinstance(item, str) for item in findings):
        return "review findings must be a list of strings"
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= float(confidence) <= 1:
        return "review confidence must be a number from 0 to 1"
    return {
        "verdict": verdict,
        "summary": summary.strip(),
        "findings": findings,
        "confidence": float(confidence),
    }


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
