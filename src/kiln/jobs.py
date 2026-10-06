import json
import sqlite3

from kiln.db import record_event, utc_now
from kiln.errors import KilnError
from kiln.models import Goal, GoalStatus, Integration, Job, JobRole, JobStatus


def _ready_sql(*, goal_id: int | None) -> tuple[str, tuple]:
    goal_clause = "j.goal_id = ? AND " if goal_id is not None else ""
    params = (goal_id,) if goal_id is not None else ()
    sql = f"""
        SELECT j.*
        FROM jobs j
        WHERE {goal_clause}j.status = ?
          AND NOT EXISTS (
              SELECT 1
              FROM job_deps
              JOIN jobs AS dep ON dep.id = job_deps.depends_on
              WHERE job_deps.job_id = j.id
                AND NOT (
                    dep.status = 'completed'
                    AND (
                        j.role != 'worker'
                        OR dep.role != 'worker'
                        OR dep.integration = 'merged'
                    )
                )
          )
        ORDER BY j.priority DESC, j.id ASC
    """
    return sql, (*params, JobStatus.pending.value)


def is_open(job: Job) -> bool:
    """True while the job still needs a decision or an executor."""
    if job.status in (JobStatus.pending, JobStatus.claimed, JobStatus.running):
        return True
    return (
        job.role == JobRole.worker
        and job.status == JobStatus.completed
        and job.integration == Integration.pending
    )


def dependency_satisfied(job: Job, *, for_role: JobRole) -> bool:
    """A dependency is satisfied once it has completed.

    A worker also waits until every worker it depends on has been merged.
    A scout or reviewer can run as soon as its dependencies have completed.
    """
    if job.status != JobStatus.completed:
        return False
    if for_role == JobRole.worker and job.role == JobRole.worker:
        return job.integration == Integration.merged
    return True


def add_goal(conn: sqlite3.Connection, title: str, description: str = "") -> Goal:
    title = _require_title(title, "goal")
    cur = conn.execute(
        "INSERT INTO goals (title, description, status, created_at) VALUES (?, ?, ?, ?)",
        (title, description, GoalStatus.active.value, utc_now()),
    )
    goal = require_goal(conn, cur.lastrowid)
    record_event(conn, "goal.created", f"created goal #{goal.id} {goal.title}")
    return goal


def get_goal(conn: sqlite3.Connection, goal_id: int) -> Goal | None:
    row = conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
    return Goal.from_row(row) if row else None


def set_goal_status(conn: sqlite3.Connection, goal_id: int, status: GoalStatus) -> Goal:
    goal = require_goal(conn, goal_id)
    conn.execute("UPDATE goals SET status = ? WHERE id = ?", (status.value, goal_id))
    record_event(conn, "goal.status", f"goal #{goal_id} {goal.status.value} -> {status.value}")
    return require_goal(conn, goal_id)


def set_goal_branch(conn: sqlite3.Connection, goal_id: int, branch: str) -> Goal:
    require_goal(conn, goal_id)
    conn.execute("UPDATE goals SET branch = ? WHERE id = ?", (branch, goal_id))
    record_event(conn, "goal.branch", f"goal #{goal_id} branch {branch}")
    return require_goal(conn, goal_id)


def set_goal_brief(conn: sqlite3.Connection, goal_id: int, text: str) -> Goal:
    """Replace the foreman's running brief for a goal."""
    require_goal(conn, goal_id)
    cleaned = text.strip()
    if not cleaned:
        raise KilnError("goal brief cannot be empty")
    conn.execute("UPDATE goals SET brief = ? WHERE id = ?", (cleaned, goal_id))
    record_event(conn, "goal.brief", f"updated brief for goal #{goal_id}")
    return require_goal(conn, goal_id)


def set_goal_evidence(conn: sqlite3.Connection, goal_id: int, evidence: list[str]) -> Goal:
    """Store why a goal is finished. Each item is one piece of evidence."""
    require_goal(conn, goal_id)
    if not evidence or any(not isinstance(item, str) or not item.strip() for item in evidence):
        raise KilnError("goal evidence must be a non-empty list of strings")
    stored = json.dumps([item.strip() for item in evidence])
    conn.execute("UPDATE goals SET evidence = ? WHERE id = ?", (stored, goal_id))
    record_event(conn, "goal.evidence", f"recorded evidence for goal #{goal_id}")
    return require_goal(conn, goal_id)


def set_goal_pr(conn: sqlite3.Connection, goal_id: int, url: str) -> Goal:
    require_goal(conn, goal_id)
    conn.execute("UPDATE goals SET pr_url = ? WHERE id = ?", (url, goal_id))
    record_event(conn, "goal.pr", url or f"goal #{goal_id} has no pull request")
    return require_goal(conn, goal_id)


def require_goal(conn: sqlite3.Connection, goal_id: int) -> Goal:
    goal = get_goal(conn, goal_id)
    if goal is None:
        raise KilnError(f"no goal with id {goal_id}")
    return goal


def list_goals(conn: sqlite3.Connection, status: GoalStatus | None = None) -> list[Goal]:
    if status is None:
        rows = conn.execute("SELECT * FROM goals ORDER BY id ASC").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM goals WHERE status = ? ORDER BY id ASC",
            (status.value,),
        ).fetchall()
    return [Goal.from_row(row) for row in rows]


def add_job(
    conn: sqlite3.Connection,
    goal_id: int,
    title: str,
    *,
    role: JobRole = JobRole.worker,
    description: str = "",
    acceptance: str = "",
    question: str = "",
    focus: str = "",
    target_job_id: int | None = None,
    depends_on: list[int] | None = None,
    priority: int = 0,
    max_attempts: int = 3,
) -> Job:
    require_goal(conn, goal_id)
    title = _require_title(title, "job")
    if max_attempts < 1:
        raise KilnError("max_attempts must be >= 1")
    deps = list(depends_on or [])
    _check_target(conn, goal_id, role, target_job_id, deps)
    now = utc_now()
    cur = conn.execute(
        """
        INSERT INTO jobs (
            goal_id, role, title, description, acceptance, question, focus, target_job_id,
            status, priority, attempts, max_attempts, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
        """,
        (
            goal_id,
            role.value,
            title,
            description,
            acceptance,
            question,
            focus,
            target_job_id,
            JobStatus.pending.value,
            priority,
            max_attempts,
            now,
            now,
        ),
    )
    job = require_job(conn, cur.lastrowid)
    for dep_id in deps:
        add_dependency(conn, job.id, dep_id)
    record_event(conn, "job.created", f"created job #{job.id} {job.role.value} {job.title}", job_id=job.id)
    return job


def get_job(conn: sqlite3.Connection, job_id: int) -> Job | None:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return Job.from_row(row) if row else None


def require_job(conn: sqlite3.Connection, job_id: int) -> Job:
    job = get_job(conn, job_id)
    if job is None:
        raise KilnError(f"no job with id {job_id}")
    return job


def list_jobs(
    conn: sqlite3.Connection,
    *,
    goal_id: int | None = None,
    status: JobStatus | None = None,
) -> list[Job]:
    clauses: list[str] = []
    params: list[object] = []
    if goal_id is not None:
        clauses.append("goal_id = ?")
        params.append(goal_id)
    if status is not None:
        clauses.append("status = ?")
        params.append(status.value)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(f"SELECT * FROM jobs {where} ORDER BY id ASC", params).fetchall()
    return [Job.from_row(row) for row in rows]


def ready_jobs(conn: sqlite3.Connection, *, goal_id: int | None = None) -> list[Job]:
    """Pending jobs whose dependencies are satisfied, highest priority first."""
    sql, params = _ready_sql(goal_id=goal_id)
    rows = conn.execute(sql, params).fetchall()
    return [Job.from_row(row) for row in rows]


def dependencies(conn: sqlite3.Connection, job_id: int) -> list[Job]:
    rows = conn.execute(
        """
        SELECT jobs.*
        FROM job_deps
        JOIN jobs ON jobs.id = job_deps.depends_on
        WHERE job_deps.job_id = ?
        ORDER BY jobs.id ASC
        """,
        (job_id,),
    ).fetchall()
    return [Job.from_row(row) for row in rows]


def dependents(conn: sqlite3.Connection, job_id: int) -> list[Job]:
    rows = conn.execute(
        """
        SELECT jobs.*
        FROM job_deps
        JOIN jobs ON jobs.id = job_deps.job_id
        WHERE job_deps.depends_on = ?
        ORDER BY jobs.id ASC
        """,
        (job_id,),
    ).fetchall()
    return [Job.from_row(row) for row in rows]


def add_dependency(conn: sqlite3.Connection, job_id: int, depends_on: int) -> None:
    """Record that job_id cannot start until depends_on is satisfied."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        job = require_job(conn, job_id)
        dep = require_job(conn, depends_on)
        if job.goal_id != dep.goal_id:
            raise KilnError(f"jobs #{job_id} and #{depends_on} are in different goals")
        if _reaches(conn, depends_on, job_id):
            raise KilnError(f"job #{job_id} depending on #{depends_on} would create a cycle")
        try:
            conn.execute(
                "INSERT INTO job_deps (job_id, depends_on) VALUES (?, ?)",
                (job_id, depends_on),
            )
        except sqlite3.IntegrityError as exc:
            raise KilnError(f"job #{job_id} already depends on #{depends_on}") from exc
        record_event(
            conn,
            "job.dependency",
            f"job #{job_id} depends on #{depends_on}",
            job_id=job_id,
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def claim_job(conn: sqlite3.Connection, job_id: int, worker: str) -> Job | None:
    """Claim a pending, unblocked job. Returns None if it is not claimable."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None or row["status"] != JobStatus.pending.value or not _deps_satisfied(conn, job_id):
            conn.execute("ROLLBACK")
            return None
        conn.execute(
            """
            UPDATE jobs
            SET status = ?, claimed_by = ?, updated_at = ?
            WHERE id = ? AND status = ?
            """,
            (JobStatus.claimed.value, worker, utc_now(), job_id, JobStatus.pending.value),
        )
        record_event(conn, "job.claimed", f"claimed by {worker}", job_id=job_id)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return require_job(conn, job_id)


def claim_next(conn: sqlite3.Connection, worker: str, *, goal_id: int | None = None) -> Job | None:
    """Claim the highest-priority ready job, if one exists."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        sql, params = _ready_sql(goal_id=goal_id)
        row = conn.execute(sql + " LIMIT 1", params).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return None
        job_id = row["id"]
        cur = conn.execute(
            """
            UPDATE jobs
            SET status = ?, claimed_by = ?, updated_at = ?
            WHERE id = ? AND status = ?
            """,
            (JobStatus.claimed.value, worker, utc_now(), job_id, JobStatus.pending.value),
        )
        if cur.rowcount != 1:
            conn.execute("ROLLBACK")
            return None
        record_event(conn, "job.claimed", f"claimed by {worker}", job_id=job_id)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return require_job(conn, job_id)


def set_job_status(conn: sqlite3.Connection, job_id: int, status: JobStatus) -> Job:
    job = require_job(conn, job_id)
    conn.execute(
        "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
        (status.value, utc_now(), job_id),
    )
    record_event(
        conn,
        "job.status",
        f"{job.status.value} -> {status.value}",
        job_id=job_id,
    )
    return require_job(conn, job_id)


def release_claim(conn: sqlite3.Connection, job_id: int) -> Job:
    """Return a claimed job to pending when setup fails before an attempt starts."""
    require_job(conn, job_id)
    conn.execute(
        "UPDATE jobs SET status = ?, claimed_by = NULL, updated_at = ? WHERE id = ?",
        (JobStatus.pending.value, utc_now(), job_id),
    )
    record_event(conn, "job.released", f"released claim on job #{job_id}", job_id=job_id)
    return require_job(conn, job_id)


def start_attempt(conn: sqlite3.Connection, job_id: int, *, branch: str, worktree_path: str) -> Job:
    require_job(conn, job_id)
    conn.execute(
        """
        UPDATE jobs
        SET status = ?, attempts = attempts + 1, branch = ?, worktree_path = ?, updated_at = ?
        WHERE id = ?
        """,
        (JobStatus.running.value, branch, worktree_path, utc_now(), job_id),
    )
    job = require_job(conn, job_id)
    record_event(
        conn,
        "job.attempt",
        f"attempt {job.attempts}/{job.max_attempts} on {branch}",
        job_id=job_id,
    )
    return job


def complete_job(conn: sqlite3.Connection, job_id: int, result: dict) -> Job:
    """Record a successful execution. A worker then waits for integration."""
    job = require_job(conn, job_id)
    payload = json.dumps(result)
    if job.role == JobRole.worker:
        conn.execute(
            """
            UPDATE jobs
            SET status = ?, integration = ?, result = ?, claimed_by = NULL, updated_at = ?
            WHERE id = ?
            """,
            (JobStatus.completed.value, Integration.pending.value, payload, utc_now(), job_id),
        )
    else:
        conn.execute(
            """
            UPDATE jobs
            SET status = ?, result = ?, claimed_by = NULL, updated_at = ?
            WHERE id = ?
            """,
            (JobStatus.completed.value, payload, utc_now(), job_id),
        )
    record_event(conn, "job.completed", f"job #{job_id} completed", job_id=job_id)
    return require_job(conn, job_id)


def set_integration(conn: sqlite3.Connection, job_id: int, integration: Integration) -> Job:
    job = require_job(conn, job_id)
    if job.role != JobRole.worker or job.status != JobStatus.completed:
        raise KilnError(f"job #{job_id} has no integration to set")
    conn.execute(
        "UPDATE jobs SET integration = ?, updated_at = ? WHERE id = ?",
        (integration.value, utc_now(), job_id),
    )
    record_event(
        conn,
        "job.integration",
        f"job #{job_id} integration {integration.value}",
        job_id=job_id,
    )
    return require_job(conn, job_id)


def rework_job(conn: sqlite3.Connection, job_id: int, feedback: str) -> Job:
    """Send a completed, unmerged worker back to pending. Exhausted jobs stay completed."""
    job = _require_waiting_worker(conn, job_id, "reworked")
    cleaned = feedback.strip()
    if not cleaned:
        raise KilnError("rework feedback cannot be empty")
    if job.attempts >= job.max_attempts:
        raise KilnError(f"job #{job_id} exhausted {job.max_attempts} attempts")
    conn.execute(
        """
        UPDATE jobs
        SET status = ?, integration = NULL, result = NULL, feedback = ?, claimed_by = NULL, updated_at = ?
        WHERE id = ?
        """,
        (JobStatus.pending.value, cleaned, utc_now(), job_id),
    )
    record_event(conn, "job.rework", cleaned, job_id=job_id)
    return require_job(conn, job_id)


def reject_job(conn: sqlite3.Connection, job_id: int, reason: str) -> Job:
    """Abandon a completed worker branch. Execution stays completed and the result stays."""
    _require_waiting_worker(conn, job_id, "rejected")
    cleaned = reason.strip() or "rejected"
    conn.execute(
        "UPDATE jobs SET integration = ?, updated_at = ? WHERE id = ?",
        (Integration.rejected.value, utc_now(), job_id),
    )
    record_event(conn, "job.integration", f"job #{job_id} rejected: {cleaned}", job_id=job_id)
    return require_job(conn, job_id)


def fail_job(conn: sqlite3.Connection, job_id: int, reason: str) -> Job:
    """The executor did not successfully perform the job."""
    job = require_job(conn, job_id)
    conn.execute(
        """
        UPDATE jobs
        SET status = ?, integration = NULL, feedback = ?, claimed_by = NULL, updated_at = ?
        WHERE id = ?
        """,
        (JobStatus.failed.value, reason, utc_now(), job_id),
    )
    record_event(conn, "job.status", f"{job.status.value} -> failed: {reason}", job_id=job_id)
    return require_job(conn, job_id)


def forget_checkout(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    worktree: bool = False,
    branch: bool = False,
) -> None:
    """Drop a stored worktree path or branch name after the checkout is gone."""
    assignments: list[str] = []
    if worktree:
        assignments.append("worktree_path = NULL")
    if branch:
        assignments.append("branch = NULL")
    if not assignments:
        return
    assignments.append("updated_at = ?")
    conn.execute(
        f"UPDATE jobs SET {', '.join(assignments)} WHERE id = ?",
        (utc_now(), job_id),
    )


def cancel_job(conn: sqlite3.Connection, job_id: int) -> Job:
    job = require_job(conn, job_id)
    if job.status in (JobStatus.completed, JobStatus.failed, JobStatus.cancelled):
        raise KilnError(f"job #{job_id} is {job.status.value} and cannot be cancelled")
    return set_job_status(conn, job_id, JobStatus.cancelled)


def _require_waiting_worker(conn: sqlite3.Connection, job_id: int, verb: str) -> Job:
    job = require_job(conn, job_id)
    if job.role == JobRole.worker and job.status == JobStatus.completed and job.integration == Integration.pending:
        return job
    raise KilnError(
        f"job #{job_id} is {job.status.value}; only a completed worker with pending integration can be {verb}"
    )


def _check_target(
    conn: sqlite3.Connection,
    goal_id: int,
    role: JobRole,
    target_job_id: int | None,
    depends_on: list[int],
) -> None:
    if role != JobRole.reviewer:
        if target_job_id is not None:
            raise KilnError("only a reviewer job has a target_job_id")
        return
    if target_job_id is None:
        raise KilnError("reviewer job requires a target_job_id")
    if target_job_id not in depends_on:
        raise KilnError("reviewer target must be listed in depends_on")
    target = require_job(conn, target_job_id)
    if target.goal_id != goal_id:
        raise KilnError(f"reviewer target #{target_job_id} is in a different goal")
    if target.role != JobRole.worker:
        raise KilnError("reviewer target must be a worker job")


def _deps_satisfied(conn: sqlite3.Connection, job_id: int) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM job_deps
        JOIN jobs AS dep ON dep.id = job_deps.depends_on
        WHERE job_deps.job_id = ?
          AND NOT (
              dep.status = 'completed'
              AND (
                  (SELECT role FROM jobs WHERE id = ?) != 'worker'
                  OR dep.role != 'worker'
                  OR dep.integration = 'merged'
              )
          )
        LIMIT 1
        """,
        (job_id, job_id),
    ).fetchone()
    return row is None


def _reaches(conn: sqlite3.Connection, start: int, target: int) -> bool:
    """True if start depends on target, directly or through a chain."""
    seen: set[int] = set()
    stack = [start]
    while stack:
        node = stack.pop()
        if node == target:
            return True
        if node in seen:
            continue
        seen.add(node)
        rows = conn.execute("SELECT depends_on FROM job_deps WHERE job_id = ?", (node,))
        stack.extend(row["depends_on"] for row in rows)
    return False


def _require_title(title: str, kind: str) -> str:
    cleaned = title.strip()
    if not cleaned:
        raise KilnError(f"{kind} title cannot be empty")
    return cleaned
