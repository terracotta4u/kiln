import sqlite3

from kiln.db import utc_now
from kiln.errors import KilnError
from kiln.models import Note


def add_note(conn: sqlite3.Connection, goal_id: int, text: str) -> Note:
    cleaned = text.strip()
    if not cleaned:
        raise KilnError("note text cannot be empty")
    cur = conn.execute(
        "INSERT INTO notes (goal_id, text, created_at) VALUES (?, ?, ?)",
        (goal_id, cleaned, utc_now()),
    )
    note_id = cur.lastrowid
    if note_id is None:
        raise KilnError("failed to record note")
    return require_note(conn, note_id)


def get_note(conn: sqlite3.Connection, note_id: int) -> Note | None:
    row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    return Note.from_row(row) if row else None


def require_note(conn: sqlite3.Connection, note_id: int) -> Note:
    note = get_note(conn, note_id)
    if note is None:
        raise KilnError(f"no note with id {note_id}")
    return note


def list_notes(conn: sqlite3.Connection, goal_id: int) -> list[Note]:
    rows = conn.execute(
        "SELECT * FROM notes WHERE goal_id = ? ORDER BY id ASC",
        (goal_id,),
    ).fetchall()
    return [Note.from_row(row) for row in rows]
