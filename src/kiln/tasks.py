import sqlite3

from kiln.db import record_event, utc_now
from kiln.errors import KilnError
from kiln.models import Goal, GoalStatus, Task, TaskStatus


def _ready_sql(*, goal_id: int | None) -> tuple[str, tuple]:
    goal_clause = "t.goal_id = ? AND " if goal_id is not None else ""
    params = (goal_id,) if goal_id is not None else ()
    sql = f"""
        SELECT t.*
        FROM tasks t
        WHERE {goal_clause}t.status = ?
          AND NOT EXISTS (
              SELECT 1
              FROM task_deps
              JOIN tasks AS dep ON dep.id = task_deps.depends_on
              WHERE task_deps.task_id = t.id
                AND dep.status != ?
          )
        ORDER BY t.priority DESC, t.id ASC
    """
    return sql, (*params, TaskStatus.pending.value, TaskStatus.done.value)


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


def add_task(
    conn: sqlite3.Connection,
    goal_id: int,
    title: str,
    *,
    description: str = "",
    acceptance: str = "",
    priority: int = 0,
    max_attempts: int = 3,
) -> Task:
    require_goal(conn, goal_id)
    title = _require_title(title, "task")
    if max_attempts < 1:
        raise KilnError("max_attempts must be >= 1")
    now = utc_now()
    cur = conn.execute(
        """
        INSERT INTO tasks (
            goal_id, title, description, acceptance, status, priority,
            attempts, max_attempts, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
        """,
        (
            goal_id,
            title,
            description,
            acceptance,
            TaskStatus.pending.value,
            priority,
            max_attempts,
            now,
            now,
        ),
    )
    task = require_task(conn, cur.lastrowid)
    record_event(conn, "task.created", f"created task #{task.id} {task.title}", task_id=task.id)
    return task


def get_task(conn: sqlite3.Connection, task_id: int) -> Task | None:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return Task.from_row(row) if row else None


def require_task(conn: sqlite3.Connection, task_id: int) -> Task:
    task = get_task(conn, task_id)
    if task is None:
        raise KilnError(f"no task with id {task_id}")
    return task


def list_tasks(
    conn: sqlite3.Connection,
    *,
    goal_id: int | None = None,
    status: TaskStatus | None = None,
) -> list[Task]:
    clauses: list[str] = []
    params: list[object] = []
    if goal_id is not None:
        clauses.append("goal_id = ?")
        params.append(goal_id)
    if status is not None:
        clauses.append("status = ?")
        params.append(status.value)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(f"SELECT * FROM tasks {where} ORDER BY id ASC", params).fetchall()
    return [Task.from_row(row) for row in rows]


def ready_tasks(conn: sqlite3.Connection, *, goal_id: int | None = None) -> list[Task]:
    """Pending tasks whose dependencies are all done, highest priority first."""
    sql, params = _ready_sql(goal_id=goal_id)
    rows = conn.execute(sql, params).fetchall()
    return [Task.from_row(row) for row in rows]


def dependencies(conn: sqlite3.Connection, task_id: int) -> list[Task]:
    rows = conn.execute(
        """
        SELECT tasks.*
        FROM task_deps
        JOIN tasks ON tasks.id = task_deps.depends_on
        WHERE task_deps.task_id = ?
        ORDER BY tasks.id ASC
        """,
        (task_id,),
    ).fetchall()
    return [Task.from_row(row) for row in rows]


def dependents(conn: sqlite3.Connection, task_id: int) -> list[Task]:
    rows = conn.execute(
        """
        SELECT tasks.*
        FROM task_deps
        JOIN tasks ON tasks.id = task_deps.task_id
        WHERE task_deps.depends_on = ?
        ORDER BY tasks.id ASC
        """,
        (task_id,),
    ).fetchall()
    return [Task.from_row(row) for row in rows]


def add_dependency(conn: sqlite3.Connection, task_id: int, depends_on: int) -> None:
    """Record that task_id cannot start until depends_on is done."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        task = require_task(conn, task_id)
        dep = require_task(conn, depends_on)
        if task.goal_id != dep.goal_id:
            raise KilnError(f"tasks #{task_id} and #{depends_on} are in different goals")
        if _reaches(conn, depends_on, task_id):
            raise KilnError(f"task #{task_id} depending on #{depends_on} would create a cycle")
        try:
            conn.execute(
                "INSERT INTO task_deps (task_id, depends_on) VALUES (?, ?)",
                (task_id, depends_on),
            )
        except sqlite3.IntegrityError as exc:
            raise KilnError(f"task #{task_id} already depends on #{depends_on}") from exc
        record_event(
            conn,
            "task.dependency",
            f"task #{task_id} depends on #{depends_on}",
            task_id=task_id,
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def claim_task(conn: sqlite3.Connection, task_id: int, worker: str) -> Task | None:
    """Claim a pending, unblocked task. Returns None if it is not claimable."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None or row["status"] != TaskStatus.pending.value or not _deps_done(conn, task_id):
            conn.execute("ROLLBACK")
            return None
        conn.execute(
            """
            UPDATE tasks
            SET status = ?, claimed_by = ?, updated_at = ?
            WHERE id = ? AND status = ?
            """,
            (TaskStatus.claimed.value, worker, utc_now(), task_id, TaskStatus.pending.value),
        )
        record_event(conn, "task.claimed", f"claimed by {worker}", task_id=task_id)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return require_task(conn, task_id)


def claim_next(conn: sqlite3.Connection, worker: str, *, goal_id: int | None = None) -> Task | None:
    """Claim the highest-priority ready task, if one exists."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        sql, params = _ready_sql(goal_id=goal_id)
        row = conn.execute(sql + " LIMIT 1", params).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return None
        task_id = row["id"]
        cur = conn.execute(
            """
            UPDATE tasks
            SET status = ?, claimed_by = ?, updated_at = ?
            WHERE id = ? AND status = ?
            """,
            (TaskStatus.claimed.value, worker, utc_now(), task_id, TaskStatus.pending.value),
        )
        if cur.rowcount != 1:
            conn.execute("ROLLBACK")
            return None
        record_event(conn, "task.claimed", f"claimed by {worker}", task_id=task_id)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return require_task(conn, task_id)


def set_task_status(conn: sqlite3.Connection, task_id: int, status: TaskStatus) -> Task:
    task = require_task(conn, task_id)
    conn.execute(
        "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
        (status.value, utc_now(), task_id),
    )
    record_event(
        conn,
        "task.status",
        f"{task.status.value} -> {status.value}",
        task_id=task_id,
    )
    return require_task(conn, task_id)


def release_claim(conn: sqlite3.Connection, task_id: int) -> Task:
    """Return a claimed task to pending when setup fails before an attempt starts."""
    require_task(conn, task_id)
    conn.execute(
        "UPDATE tasks SET status = ?, claimed_by = NULL, updated_at = ? WHERE id = ?",
        (TaskStatus.pending.value, utc_now(), task_id),
    )
    record_event(conn, "task.released", f"released claim on task #{task_id}", task_id=task_id)
    return require_task(conn, task_id)


def start_attempt(conn: sqlite3.Connection, task_id: int, *, branch: str, worktree_path: str) -> Task:
    require_task(conn, task_id)
    conn.execute(
        """
        UPDATE tasks
        SET status = ?, attempts = attempts + 1, branch = ?, worktree_path = ?, updated_at = ?
        WHERE id = ?
        """,
        (TaskStatus.running.value, branch, worktree_path, utc_now(), task_id),
    )
    task = require_task(conn, task_id)
    record_event(
        conn,
        "task.attempt",
        f"attempt {task.attempts}/{task.max_attempts} on {branch}",
        task_id=task_id,
    )
    return task


def fail_task(conn: sqlite3.Connection, task_id: int, reason: str) -> Task:
    task = require_task(conn, task_id)
    conn.execute(
        """
        UPDATE tasks
        SET status = ?, feedback = ?, claimed_by = NULL, updated_at = ?
        WHERE id = ?
        """,
        (TaskStatus.failed.value, reason, utc_now(), task_id),
    )
    record_event(conn, "task.status", f"{task.status.value} -> failed: {reason}", task_id=task_id)
    return require_task(conn, task_id)


def cancel_task(conn: sqlite3.Connection, task_id: int) -> Task:
    task = require_task(conn, task_id)
    if task.status in (TaskStatus.done, TaskStatus.cancelled):
        raise KilnError(f"task #{task_id} is {task.status.value} and cannot be cancelled")
    return set_task_status(conn, task_id, TaskStatus.cancelled)


def _deps_done(conn: sqlite3.Connection, task_id: int) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM task_deps
        JOIN tasks AS dep ON dep.id = task_deps.depends_on
        WHERE task_deps.task_id = ?
          AND dep.status != ?
        LIMIT 1
        """,
        (task_id, TaskStatus.done.value),
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
        rows = conn.execute("SELECT depends_on FROM task_deps WHERE task_id = ?", (node,))
        stack.extend(row["depends_on"] for row in rows)
    return False


def _require_title(title: str, kind: str) -> str:
    cleaned = title.strip()
    if not cleaned:
        raise KilnError(f"{kind} title cannot be empty")
    return cleaned
