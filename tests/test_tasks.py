import threading

import pytest

from kiln.db import connect, migrate
from kiln.errors import KilnError
from kiln.models import TaskStatus
from kiln.tasks import (
    add_dependency,
    add_goal,
    add_task,
    cancel_task,
    claim_next,
    claim_task,
    ready_tasks,
    set_task_status,
)


@pytest.fixture
def conn(tmp_path):
    connection = connect(tmp_path / "kiln.db")
    migrate(connection)
    yield connection
    connection.close()


def test_migrate_is_idempotent(conn):
    migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1


def test_ready_set_respects_dependencies_and_status(conn):
    goal = add_goal(conn, "Ship it")
    first = add_task(conn, goal.id, "Schema", priority=1)
    second = add_task(conn, goal.id, "CLI", priority=5)
    third = add_task(conn, goal.id, "Worker", priority=0)
    add_dependency(conn, third.id, first.id)
    add_dependency(conn, third.id, second.id)

    ready = [task.id for task in ready_tasks(conn)]
    assert ready == [second.id, first.id]

    set_task_status(conn, second.id, TaskStatus.done)
    ready = [task.id for task in ready_tasks(conn)]
    assert ready == [first.id]

    set_task_status(conn, first.id, TaskStatus.done)
    assert [task.id for task in ready_tasks(conn)] == [third.id]


def test_cancelled_and_failed_dependencies_keep_a_task_blocked(conn):
    goal = add_goal(conn, "Ship it")
    first = add_task(conn, goal.id, "Schema")
    second = add_task(conn, goal.id, "CLI")
    add_dependency(conn, second.id, first.id)
    set_task_status(conn, first.id, TaskStatus.failed)
    assert ready_tasks(conn) == []
    set_task_status(conn, first.id, TaskStatus.cancelled)
    assert ready_tasks(conn) == []


def test_claim_is_exclusive_and_skips_blocked_tasks(conn, tmp_path):
    goal = add_goal(conn, "Ship it")
    first = add_task(conn, goal.id, "Schema")
    second = add_task(conn, goal.id, "CLI")
    add_dependency(conn, second.id, first.id)

    assert claim_task(conn, second.id, "worker-a") is None
    claimed = claim_task(conn, first.id, "worker-a")
    assert claimed is not None
    assert claimed.claimed_by == "worker-a"
    assert claimed.status == TaskStatus.claimed

    other = connect(tmp_path / "kiln.db")
    try:
        assert claim_task(other, first.id, "worker-b") is None
        assert claim_task(other, 999, "worker-b") is None
    finally:
        other.close()

    assert [task.id for task in ready_tasks(conn)] == []


def test_claim_next_picks_highest_priority_ready_task(conn):
    goal = add_goal(conn, "Ship it")
    low = add_task(conn, goal.id, "Low", priority=1)
    high = add_task(conn, goal.id, "High", priority=10)
    blocked = add_task(conn, goal.id, "Blocked", priority=100)
    add_dependency(conn, blocked.id, low.id)

    claimed = claim_next(conn, "worker-a")
    assert claimed is not None
    assert claimed.id == high.id
    nxt = claim_next(conn, "worker-b")
    assert nxt is not None
    assert nxt.id == low.id
    assert claim_next(conn, "worker-c") is None


def test_overlapping_claims_have_one_winner(tmp_path):
    path = tmp_path / "kiln.db"
    connection = connect(path)
    migrate(connection)
    goal = add_goal(connection, "Ship it")
    task = add_task(connection, goal.id, "Schema")
    connection.close()

    barrier = threading.Barrier(2)
    winners: list[str] = []
    lock = threading.Lock()

    def attempt(name: str) -> None:
        worker = connect(path)
        try:
            barrier.wait(timeout=5)
            claimed = claim_task(worker, task.id, name)
            if claimed is not None:
                with lock:
                    winners.append(name)
        finally:
            worker.close()

    threads = [threading.Thread(target=attempt, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert len(winners) == 1
    check = connect(path)
    try:
        row = check.execute("SELECT claimed_by, status FROM tasks WHERE id = ?", (task.id,)).fetchone()
        assert row["status"] == TaskStatus.claimed.value
        assert row["claimed_by"] == winners[0]
    finally:
        check.close()


def test_dependency_cycle_is_rejected(conn):
    goal = add_goal(conn, "Ship it")
    first = add_task(conn, goal.id, "A")
    second = add_task(conn, goal.id, "B")
    third = add_task(conn, goal.id, "C")
    add_dependency(conn, second.id, first.id)
    add_dependency(conn, third.id, second.id)

    with pytest.raises(KilnError, match="cycle"):
        add_dependency(conn, first.id, third.id)
    with pytest.raises(KilnError, match="cycle"):
        add_dependency(conn, first.id, first.id)

    add_dependency(conn, third.id, first.id)
    with pytest.raises(KilnError, match="already depends"):
        add_dependency(conn, third.id, first.id)


def test_diamond_dependencies_are_allowed(conn):
    goal = add_goal(conn, "Ship it")
    root = add_task(conn, goal.id, "Root")
    left = add_task(conn, goal.id, "Left")
    right = add_task(conn, goal.id, "Right")
    leaf = add_task(conn, goal.id, "Leaf")
    add_dependency(conn, left.id, root.id)
    add_dependency(conn, right.id, root.id)
    add_dependency(conn, leaf.id, left.id)
    add_dependency(conn, leaf.id, right.id)
    add_dependency(conn, left.id, right.id)


def test_dependency_must_share_a_goal(conn):
    first = add_goal(conn, "One")
    second = add_goal(conn, "Two")
    task_a = add_task(conn, first.id, "A")
    task_b = add_task(conn, second.id, "B")
    with pytest.raises(KilnError, match="different goals"):
        add_dependency(conn, task_a.id, task_b.id)


def test_cancel_removes_a_task_from_the_ready_set(conn):
    goal = add_goal(conn, "Ship it")
    task = add_task(conn, goal.id, "Schema")
    cancel_task(conn, task.id)
    assert ready_tasks(conn) == []
    with pytest.raises(KilnError, match="cannot be cancelled"):
        cancel_task(conn, task.id)


def test_blank_titles_are_rejected(conn):
    with pytest.raises(KilnError, match="title"):
        add_goal(conn, "   ")
    goal = add_goal(conn, "Ship it")
    with pytest.raises(KilnError, match="title"):
        add_task(conn, goal.id, " ")


def test_creations_are_audited(conn):
    goal = add_goal(conn, "Ship it")
    add_task(conn, goal.id, "Schema")
    kinds = [row["kind"] for row in conn.execute("SELECT kind FROM events ORDER BY id")]
    assert kinds == ["goal.created", "task.created"]
