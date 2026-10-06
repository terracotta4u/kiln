import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from kiln.errors import KilnError
from kiln.models import Event

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE goals (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'done')),
    created_at TEXT NOT NULL,
    branch TEXT,
    pr_url TEXT,
    brief TEXT,
    evidence TEXT
);

CREATE TABLE jobs (
    id INTEGER PRIMARY KEY,
    goal_id INTEGER NOT NULL REFERENCES goals(id),
    role TEXT NOT NULL CHECK (role IN ('scout', 'worker', 'reviewer')),
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    acceptance TEXT NOT NULL DEFAULT '',
    question TEXT NOT NULL DEFAULT '',
    focus TEXT NOT NULL DEFAULT '',
    target_job_id INTEGER REFERENCES jobs(id),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'claimed', 'running', 'completed', 'failed', 'cancelled')
    ),
    integration TEXT CHECK (integration IS NULL OR integration IN ('pending', 'merged', 'rejected')),
    priority INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    branch TEXT,
    worktree_path TEXT,
    claimed_by TEXT,
    feedback TEXT,
    result TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE job_deps (
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    depends_on INTEGER NOT NULL REFERENCES jobs(id),
    PRIMARY KEY (job_id, depends_on)
);

CREATE TABLE runs (
    id INTEGER PRIMARY KEY,
    job_id INTEGER REFERENCES jobs(id),
    role TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL,
    exit_code INTEGER,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    log_path TEXT,
    report_json TEXT
);

CREATE TABLE notes (
    id INTEGER PRIMARY KEY,
    goal_id INTEGER NOT NULL REFERENCES goals(id),
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE events (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    job_id INTEGER REFERENCES jobs(id),
    run_id INTEGER REFERENCES runs(id),
    message TEXT NOT NULL
);

CREATE INDEX idx_jobs_goal ON jobs(goal_id);
CREATE INDEX idx_jobs_status ON jobs(status);
CREATE INDEX idx_job_deps_depends_on ON job_deps(depends_on);
CREATE INDEX idx_events_ts ON events(ts);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise KilnError(
            f"database version {version} is newer than this kiln (supports {SCHEMA_VERSION})"
        )
    if version < 1:
        conn.executescript(SCHEMA)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def list_events(conn: sqlite3.Connection, *, limit: int) -> list[Event]:
    if limit < 1:
        raise KilnError("log limit must be >= 1")
    rows = conn.execute(
        "SELECT * FROM events ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [Event.from_row(row) for row in reversed(rows)]


def record_event(
    conn: sqlite3.Connection,
    kind: str,
    message: str,
    *,
    job_id: int | None = None,
    run_id: int | None = None,
) -> None:
    conn.execute(
        "INSERT INTO events (ts, kind, job_id, run_id, message) VALUES (?, ?, ?, ?, ?)",
        (utc_now(), kind, job_id, run_id, message),
    )
