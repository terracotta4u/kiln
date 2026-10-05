import sqlite3

from kiln.db import record_event, utc_now
from kiln.errors import KilnError


def enqueue_scout(conn: sqlite3.Connection, goal_id: int, question: str) -> int:
    cleaned = question.strip()
    if not cleaned:
        raise KilnError("scout question cannot be empty")
    cur = conn.execute(
        "INSERT INTO scout_requests (goal_id, question, created_at) VALUES (?, ?, ?)",
        (goal_id, cleaned, utc_now()),
    )
    request_id = cur.lastrowid
    if request_id is None:
        raise KilnError("failed to queue scout")
    record_event(conn, "scout.queued", cleaned)
    return request_id


def pending_scouts(conn: sqlite3.Connection, goal_id: int) -> list[tuple[int, str]]:
    rows = conn.execute(
        "SELECT id, question FROM scout_requests WHERE goal_id = ? ORDER BY id ASC",
        (goal_id,),
    ).fetchall()
    return [(row["id"], row["question"]) for row in rows]


def drop_scout(conn: sqlite3.Connection, request_id: int) -> None:
    conn.execute("DELETE FROM scout_requests WHERE id = ?", (request_id,))
