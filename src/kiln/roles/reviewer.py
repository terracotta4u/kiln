"""Independent read-only review of a task branch."""

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from kiln.agent import DEFAULT_TIMEOUT_SECONDS, AgentResult, get_harness, run_agent
from kiln.config import Config
from kiln.db import record_event
from kiln.errors import KilnError
from kiln.git import branch_diff, ensure_worktree
from kiln.models import Run, RunStatus, Task, TaskStatus
from kiln.prompts import render_prompt
from kiln.publish import ensure_goal_branch
from kiln.runs import finish_run, latest_run, start_run
from kiln.tasks import require_goal, require_task

DIFF_CHAR_LIMIT = 12_000
_VERDICTS = {"approve", "needs_changes", "reject"}


@dataclass(frozen=True)
class ReviewOutcome:
    task: Task
    run: Run
    failure: str | None
    verdict: str | None
    summary: str


def run_reviewer(
    conn: sqlite3.Connection,
    config: Config,
    task_id: int,
    *,
    focus: str = "",
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    agent_bin: str | None = None,
    reporter: Callable[[str], None] | None = None,
) -> ReviewOutcome:
    """Review a task that is waiting in review. The task status stays put."""
    task = require_task(conn, task_id)
    if task.status != TaskStatus.review:
        raise KilnError(f"task #{task_id} is {task.status.value}; only a task in review can be reviewed")
    if not task.branch:
        raise KilnError(f"task #{task_id} has no branch to review")
    goal = require_goal(conn, task.goal_id)
    integration, _ = ensure_goal_branch(conn, config, goal)
    worktree = Path(task.worktree_path) if task.worktree_path else config.worktrees_dir / str(task.id)
    ensure_worktree(config.repo_root, worktree, task.branch, integration)

    run = start_run(conn, role="reviewer", model=config.models.reviewer, task_id=task.id)
    if reporter:
        reporter(f"reviewing task #{task.id} with {config.models.reviewer}")
    log_path = config.runs_dir / f"{run.id}.log"
    prompt = render_prompt(
        "reviewer.md",
        {
            "repo_root": str(config.repo_root),
            "goal_id": str(goal.id),
            "goal_title": goal.title,
            "goal_description": goal.description or "(none)",
            "brief": goal.brief or "(none)",
            "task_id": str(task.id),
            "title": task.title,
            "description": task.description or "(none)",
            "acceptance": task.acceptance or "(none)",
            "focus": focus.strip() or "(none)",
            "verify": _verify_text(conn, task.id),
            "integration_branch": integration,
            "diff": _capped_diff(config.repo_root, integration, task.branch),
        },
    )
    try:
        result = run_agent(
            prompt=prompt,
            model=config.models.reviewer,
            workspace=worktree,
            log_path=log_path,
            timeout=timeout,
            agent_bin=agent_bin,
            harness=get_harness(config.harness),
            readonly=True,
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
        record_event(conn, "review.failed", str(exc), task_id=task.id, run_id=finished.id)
        raise

    failure = _failure(result, timeout)
    report = _validated(result.report) if failure is None else None
    if failure is None and isinstance(report, str):
        failure = report
        report = None
    finished = finish_run(
        conn,
        run.id,
        status=RunStatus.failed if failure else RunStatus.succeeded,
        exit_code=result.exit_code,
        log_path=str(result.log_path),
        report=report if isinstance(report, dict) else None,
    )
    if failure:
        record_event(conn, "review.failed", failure, task_id=task.id, run_id=finished.id)
        return ReviewOutcome(task=require_task(conn, task.id), run=finished, failure=failure, verdict=None, summary="")
    assert isinstance(report, dict)
    record_event(
        conn,
        "review.completed",
        f"{report['verdict']}: {report['summary']}",
        task_id=task.id,
        run_id=finished.id,
    )
    return ReviewOutcome(
        task=require_task(conn, task.id),
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


def _verify_text(conn: sqlite3.Connection, task_id: int) -> str:
    run = latest_run(conn, task_id, "worker")
    if run is None or not run.report_json:
        return "(none)"
    try:
        report = json.loads(run.report_json)
    except json.JSONDecodeError:
        return "(none)"
    if not isinstance(report, dict):
        return "(none)"
    verify = report.get("verify")
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
