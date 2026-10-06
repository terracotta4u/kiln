import os
import sqlite3
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from kiln.agent import DEFAULT_TIMEOUT_SECONDS, AgentResult, run_agent
from kiln.config import Config
from kiln.db import record_event
from kiln.errors import KilnError
from kiln.git import branch_name, commit_if_dirty, diffstat, ensure_worktree, revision
from kiln.jobs import (
    claim_job,
    complete_job,
    fail_job,
    ready_jobs,
    release_claim,
    require_goal,
    require_job,
    start_attempt,
)
from kiln.models import Job, JobRole, JobStatus, Run, RunStatus
from kiln.prompts import render_prompt
from kiln.result import envelope
from kiln.publish import ensure_goal_branch
from kiln.runs import finish_run, start_run

VERIFY_TIMEOUT_SECONDS = 300
_VERIFY_OUTPUT_LIMIT = 4000


@dataclass(frozen=True)
class WorkerOutcome:
    job: Job
    run: Run
    failure: str | None
    diffstat: str
    summary: str


def run_worker(
    conn: sqlite3.Connection,
    config: Config,
    *,
    task_id: int | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    agent_bin: str | None = None,
    reporter: Callable[[str], None] | None = None,
) -> WorkerOutcome:
    worker = f"kiln-{os.getpid()}"
    task = _claim(conn, task_id, worker)
    branch = task.branch or branch_name(task.id, task.title)
    worktree = Path(task.worktree_path) if task.worktree_path else config.worktrees_dir / str(task.id)
    try:
        integration, _ = ensure_goal_branch(conn, config, require_goal(conn, task.goal_id))
        ensure_worktree(config.repo_root, worktree, branch, integration)
    except KilnError:
        release_claim(conn, task.id)
        raise

    task = start_attempt(conn, task.id, branch=branch, worktree_path=str(worktree.resolve()))
    before = revision(worktree)
    if reporter:
        reporter(f"working task #{task.id} on {branch}")
    goal = require_goal(conn, task.goal_id)
    run = start_run(conn, role="worker", model=config.models.worker, job_id=task.id)
    prompt = render_prompt(
        "worker.md",
        {
            "repo_root": str(config.repo_root),
            "branch": branch,
            "integration_branch": integration,
            "base_branch": config.base_branch,
            "goal_id": str(goal.id),
            "goal_title": goal.title,
            "task_id": str(task.id),
            "title": task.title,
            "description": task.description or "(none)",
            "acceptance": task.acceptance or "(none)",
            "feedback": task.feedback or "(none)",
        },
    )

    agent_result: AgentResult | None = None
    failure: str | None = None
    committed = False
    verify_code, verify_out = 0, ""
    changes = ""
    try:
        agent_result = run_agent(
            prompt=prompt,
            model=config.models.worker,
            workspace=worktree,
            log_path=config.runs_dir / f"{run.id}.log",
            force=True,
            timeout=timeout,
            agent_bin=agent_bin,
        )
    except KilnError as exc:
        failure = str(exc)

    try:
        committed = commit_if_dirty(worktree, f"kiln: task #{task.id} {task.title}")
    except KilnError as exc:
        failure = failure or str(exc)

    try:
        changes = diffstat(config.repo_root, integration, branch)
    except KilnError as exc:
        failure = failure or str(exc)

    agent_failure = _agent_failure(agent_result) if agent_result is not None else None
    if agent_failure:
        failure = failure or agent_failure
    produced = revision(worktree) != before or committed
    if produced and config.verify.strip():
        verify_code, verify_out = _run_verify(config.verify, worktree)
    if not produced:
        reason = failure or "no commit was produced"
        finished = finish_run(
            conn,
            run.id,
            status=RunStatus.failed,
            exit_code=None if agent_result is None else agent_result.exit_code,
            log_path=str(config.runs_dir / f"{run.id}.log"),
            report=None,
        )
        task = fail_job(conn, task.id, reason)
        record_event(conn, "worker.failed", reason, job_id=task.id, run_id=finished.id)
        return WorkerOutcome(job=task, run=finished, failure=reason, diffstat=changes, summary="")

    if verify_code != 0:
        verify_failure = _verify_failure(verify_code, verify_out)
        failure = f"{failure}; {verify_failure}" if failure else verify_failure

    summary, files = _worker_summary(agent_result.report if agent_result is not None else None)
    evidence: list[str] = []
    if failure:
        evidence.append(failure)
    stored = envelope(
        summary=summary or f"committed {branch}",
        evidence=evidence,
        artifacts=[{"type": "branch", "name": branch}],
        role_result={
            "summary": summary,
            "files": files,
            "committed": True,
            "verify": {
                "command": config.verify,
                "exit_code": verify_code,
                "output": verify_out[-_VERIFY_OUTPUT_LIMIT:],
            },
            "diffstat": changes,
        },
    )
    finished = finish_run(
        conn,
        run.id,
        status=RunStatus.failed if failure else RunStatus.succeeded,
        exit_code=None if agent_result is None else agent_result.exit_code,
        log_path=str(config.runs_dir / f"{run.id}.log"),
        report=stored,
    )
    task = complete_job(conn, task.id, stored)
    record_event(
        conn,
        "worker.failed" if failure else "worker.completed",
        failure or f"job #{task.id} completed",
        job_id=task.id,
        run_id=finished.id,
    )
    return WorkerOutcome(
        job=task,
        run=finished,
        failure=failure,
        diffstat=changes,
        summary=summary,
    )


def _claim(conn: sqlite3.Connection, task_id: int | None, worker: str) -> Job:
    if task_id is not None:
        task = require_job(conn, task_id)
        if task.role != JobRole.worker:
            raise KilnError(f"job #{task.id} is a {task.role.value}; only a worker job can be claimed here")
        if task.status == JobStatus.pending and task.attempts >= task.max_attempts:
            fail_job(conn, task.id, f"exhausted {task.max_attempts} attempts")
            raise KilnError(f"task #{task.id} exhausted {task.max_attempts} attempts")
        claimed = claim_job(conn, task.id, worker)
        if claimed is None:
            raise KilnError(f"task #{task.id} is not ready")
        return claimed

    while True:
        ready = [job for job in ready_jobs(conn) if job.role == JobRole.worker]
        if not ready:
            raise KilnError("no ready job")
        candidate = ready[0]
        if candidate.attempts >= candidate.max_attempts:
            fail_job(conn, candidate.id, f"exhausted {candidate.max_attempts} attempts")
            if len(ready) == 1:
                raise KilnError(f"task #{candidate.id} exhausted {candidate.max_attempts} attempts")
            continue
        claimed = claim_job(conn, candidate.id, worker)
        if claimed is not None:
            return claimed


def _worker_summary(report: dict | None) -> tuple[str, list[str]]:
    if not isinstance(report, dict):
        return "", []
    summary = report.get("summary")
    files = report.get("files")
    cleaned = summary.strip() if isinstance(summary, str) else ""
    listed = files if isinstance(files, list) and all(isinstance(item, str) for item in files) else []
    return cleaned, listed


def _agent_failure(result: AgentResult) -> str | None:
    if result.timed_out:
        return "timed out"
    if result.exit_code != 0:
        return f"agent exited {result.exit_code}"
    if result.parsed.is_error:
        return "agent reported an error"
    if result.report is None:
        return "response had no JSON report"
    return None


def _verify_failure(code: int, output: str) -> str:
    line = ""
    for item in reversed(output.splitlines()):
        if item.strip():
            line = item.strip()
            break
    if line:
        return f"verify exited {code}: {line}"
    return f"verify exited {code}"


def _run_verify(command: str, cwd: Path) -> tuple[int, str]:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=VERIFY_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", "replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        output = f"{stdout}{stderr}\nverify timed out".strip()
        return 124, output
    return result.returncode, (result.stdout + result.stderr).strip()
