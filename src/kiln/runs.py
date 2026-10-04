import json
import sqlite3

from kiln.db import utc_now
from kiln.errors import KilnError
from kiln.models import Run, RunStatus


def start_run(
    conn: sqlite3.Connection,
    *,
    role: str,
    model: str,
    task_id: int | None = None,
) -> Run:
    cur = conn.execute(
        """
        INSERT INTO runs (task_id, role, model, status, started_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (task_id, role, model, RunStatus.running.value, utc_now()),
    )
    run_id = cur.lastrowid
    if run_id is None:
        raise KilnError("failed to record run")
    return require_run(conn, run_id)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    status: RunStatus,
    exit_code: int | None,
    log_path: str | None,
    report: dict | None,
) -> Run:
    require_run(conn, run_id)
    conn.execute(
        """
        UPDATE runs
        SET status = ?, exit_code = ?, finished_at = ?, log_path = ?, report_json = ?
        WHERE id = ?
        """,
        (
            status.value,
            exit_code,
            utc_now(),
            log_path,
            json.dumps(report) if report is not None else None,
            run_id,
        ),
    )
    return require_run(conn, run_id)


def get_run(conn: sqlite3.Connection, run_id: int) -> Run | None:
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return Run.from_row(row) if row else None


def require_run(conn: sqlite3.Connection, run_id: int) -> Run:
    run = get_run(conn, run_id)
    if run is None:
        raise KilnError(f"no run with id {run_id}")
    return run
