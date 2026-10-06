import threading

import pytest

from kiln.db import connect, migrate
from kiln.errors import KilnError
from kiln.jobs import (
    add_dependency,
    add_goal,
    add_job,
    cancel_job,
    claim_job,
    claim_next,
    complete_job,
    list_jobs,
    ready_jobs,
    reject_job,
    rework_job,
    set_goal_brief,
    set_goal_evidence,
    set_integration,
    set_job_status,
)
from kiln.models import Integration, JobRole, JobStatus


@pytest.fixture
def conn(tmp_path):
    connection = connect(tmp_path / "kiln.db")
    migrate(connection)
    yield connection
    connection.close()


def test_migrate_is_idempotent(conn):
    migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1


def test_a_newer_database_is_refused(tmp_path):
    connection = connect(tmp_path / "kiln.db")
    try:
        connection.execute("PRAGMA user_version = 2")
        with pytest.raises(KilnError, match="newer than this kiln"):
            migrate(connection)
    finally:
        connection.close()


def test_goal_brief_and_evidence_round_trip(conn):
    goal = add_goal(conn, "Ship it")
    updated = set_goal_brief(conn, goal.id, "  Success: the marker exists.  ")
    finished = set_goal_evidence(conn, goal.id, ["pytest passed", " review found no blockers "])
    assert updated.brief == "Success: the marker exists."
    assert finished.evidence == ("pytest passed", "review found no blockers")
    with pytest.raises(KilnError, match="brief"):
        set_goal_brief(conn, goal.id, "   ")
    with pytest.raises(KilnError, match="evidence"):
        set_goal_evidence(conn, goal.id, [])


def test_a_worker_dependency_unblocks_only_after_it_is_merged(conn):
    goal = add_goal(conn, "Ship it")
    first = add_job(conn, goal.id, "Schema", priority=1)
    second = add_job(conn, goal.id, "CLI", priority=5)
    third = add_job(conn, goal.id, "Worker", priority=0)
    add_dependency(conn, third.id, first.id)
    add_dependency(conn, third.id, second.id)

    ready = [job.id for job in ready_jobs(conn)]
    assert ready == [second.id, first.id]

    set_job_status(conn, second.id, JobStatus.completed)
    set_integration(conn, second.id, Integration.pending)
    assert [job.id for job in ready_jobs(conn)] == [first.id]

    set_integration(conn, second.id, Integration.merged)
    assert [job.id for job in ready_jobs(conn)] == [first.id]

    set_job_status(conn, first.id, JobStatus.completed)
    set_integration(conn, first.id, Integration.merged)
    assert [job.id for job in ready_jobs(conn)] == [third.id]


def test_a_completed_scout_unblocks_a_worker_without_integration(conn):
    goal = add_goal(conn, "Ship it")
    scout = add_job(conn, goal.id, "Look", role=JobRole.scout)
    worker = add_job(conn, goal.id, "Change")
    add_dependency(conn, worker.id, scout.id)
    assert [job.id for job in ready_jobs(conn)] == [scout.id]
    set_job_status(conn, scout.id, JobStatus.completed)
    assert [job.id for job in ready_jobs(conn)] == [worker.id]


def test_a_reviewer_can_run_before_its_worker_is_merged(conn):
    goal = add_goal(conn, "Ship it")
    worker = add_job(conn, goal.id, "Change")
    later = add_job(conn, goal.id, "Next")
    set_job_status(conn, worker.id, JobStatus.completed)
    set_integration(conn, worker.id, Integration.pending)
    reviewer = add_job(
        conn,
        goal.id,
        "Review the change",
        role=JobRole.reviewer,
        target_job_id=worker.id,
        depends_on=[worker.id],
    )
    add_dependency(conn, later.id, worker.id)
    ready = [job.id for job in ready_jobs(conn)]
    assert reviewer.id in ready
    assert later.id not in ready


def test_a_reviewer_requires_a_worker_target_in_its_dependencies(conn):
    goal = add_goal(conn, "Ship it")
    worker = add_job(conn, goal.id, "Change")
    scout = add_job(conn, goal.id, "Look", role=JobRole.scout)
    with pytest.raises(KilnError, match="target_job_id"):
        add_job(conn, goal.id, "Review", role=JobRole.reviewer)
    with pytest.raises(KilnError, match="depends_on"):
        add_job(
            conn,
            goal.id,
            "Review",
            role=JobRole.reviewer,
            target_job_id=worker.id,
            depends_on=[scout.id],
        )
    with pytest.raises(KilnError, match="worker"):
        add_job(
            conn,
            goal.id,
            "Review",
            role=JobRole.reviewer,
            target_job_id=scout.id,
            depends_on=[scout.id],
        )
    with pytest.raises(KilnError, match="target_job_id"):
        add_job(conn, goal.id, "Change", target_job_id=worker.id)


def test_rework_clears_the_current_result_and_stops_when_attempts_are_exhausted(conn):
    goal = add_goal(conn, "Ship it")
    job = add_job(conn, goal.id, "Change", max_attempts=2)
    completed = complete_job(conn, job.id, {"summary": "first"})
    assert completed.status == JobStatus.completed
    assert completed.integration == Integration.pending
    sent_back = rework_job(conn, job.id, "try again")
    assert sent_back.status == JobStatus.pending
    assert sent_back.integration is None
    assert sent_back.result is None
    assert sent_back.feedback == "try again"

    conn.execute("UPDATE jobs SET attempts = max_attempts WHERE id = ?", (job.id,))
    finished = complete_job(conn, job.id, {"summary": "second"})
    with pytest.raises(KilnError, match="exhausted"):
        rework_job(conn, job.id, "once more")
    stored = conn.execute("SELECT status, integration, result FROM jobs WHERE id = ?", (job.id,)).fetchone()
    assert stored["status"] == JobStatus.completed.value
    assert stored["integration"] == Integration.pending.value
    assert stored["result"] == finished.result


def test_reject_keeps_the_result_and_leaves_execution_completed(conn):
    goal = add_goal(conn, "Ship it")
    job = add_job(conn, goal.id, "Change")
    complete_job(conn, job.id, {"summary": "landed"})
    rejected = reject_job(conn, job.id, "abandon")
    assert rejected.status == JobStatus.completed
    assert rejected.integration == Integration.rejected
    assert rejected.result is not None


def test_cancelled_and_failed_dependencies_keep_a_job_blocked(conn):
    goal = add_goal(conn, "Ship it")
    first = add_job(conn, goal.id, "Schema")
    second = add_job(conn, goal.id, "CLI")
    add_dependency(conn, second.id, first.id)
    set_job_status(conn, first.id, JobStatus.failed)
    assert ready_jobs(conn) == []
    set_job_status(conn, first.id, JobStatus.cancelled)
    assert ready_jobs(conn) == []


def test_claim_is_exclusive_and_skips_blocked_jobs(conn, tmp_path):
    goal = add_goal(conn, "Ship it")
    first = add_job(conn, goal.id, "Schema")
    second = add_job(conn, goal.id, "CLI")
    add_dependency(conn, second.id, first.id)

    assert claim_job(conn, second.id, "worker-a") is None
    claimed = claim_job(conn, first.id, "worker-a")
    assert claimed is not None
    assert claimed.claimed_by == "worker-a"
    assert claimed.status == JobStatus.claimed

    other = connect(tmp_path / "kiln.db")
    try:
        assert claim_job(other, first.id, "worker-b") is None
        assert claim_job(other, 999, "worker-b") is None
    finally:
        other.close()

    assert [job.id for job in ready_jobs(conn)] == []


def test_claim_next_picks_highest_priority_ready_job(conn):
    goal = add_goal(conn, "Ship it")
    low = add_job(conn, goal.id, "Low", priority=1)
    high = add_job(conn, goal.id, "High", priority=10)
    blocked = add_job(conn, goal.id, "Blocked", priority=100)
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
    job = add_job(connection, goal.id, "Schema")
    connection.close()

    barrier = threading.Barrier(2)
    winners: list[str] = []
    lock = threading.Lock()

    def attempt(name: str) -> None:
        worker = connect(path)
        try:
            barrier.wait(timeout=5)
            claimed = claim_job(worker, job.id, name)
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
        row = check.execute("SELECT claimed_by, status FROM jobs WHERE id = ?", (job.id,)).fetchone()
        assert row["status"] == JobStatus.claimed.value
        assert row["claimed_by"] == winners[0]
    finally:
        check.close()


def test_dependency_cycle_is_rejected(conn):
    goal = add_goal(conn, "Ship it")
    first = add_job(conn, goal.id, "A")
    second = add_job(conn, goal.id, "B")
    third = add_job(conn, goal.id, "C")
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
    root = add_job(conn, goal.id, "Root")
    left = add_job(conn, goal.id, "Left")
    right = add_job(conn, goal.id, "Right")
    leaf = add_job(conn, goal.id, "Leaf")
    add_dependency(conn, left.id, root.id)
    add_dependency(conn, right.id, root.id)
    add_dependency(conn, leaf.id, left.id)
    add_dependency(conn, leaf.id, right.id)
    add_dependency(conn, left.id, right.id)


def test_add_job_rolls_back_when_a_dependency_is_rejected(conn):
    first = add_goal(conn, "One")
    second = add_goal(conn, "Two")
    foreign = add_job(conn, second.id, "Foreign")
    with pytest.raises(KilnError, match="different goals"):
        add_job(conn, first.id, "Local", depends_on=[foreign.id])
    assert list_jobs(conn, goal_id=first.id) == []


def test_dependency_must_share_a_goal(conn):
    first = add_goal(conn, "One")
    second = add_goal(conn, "Two")
    job_a = add_job(conn, first.id, "A")
    job_b = add_job(conn, second.id, "B")
    with pytest.raises(KilnError, match="different goals"):
        add_dependency(conn, job_a.id, job_b.id)


def test_cancel_removes_a_job_from_the_ready_set(conn):
    goal = add_goal(conn, "Ship it")
    job = add_job(conn, goal.id, "Schema")
    cancel_job(conn, job.id)
    assert ready_jobs(conn) == []
    with pytest.raises(KilnError, match="cannot be cancelled"):
        cancel_job(conn, job.id)


def test_blank_titles_are_rejected(conn):
    with pytest.raises(KilnError, match="title"):
        add_goal(conn, "   ")
    goal = add_goal(conn, "Ship it")
    with pytest.raises(KilnError, match="title"):
        add_job(conn, goal.id, " ")


def test_creations_are_audited(conn):
    goal = add_goal(conn, "Ship it")
    add_job(conn, goal.id, "Schema")
    kinds = [row["kind"] for row in conn.execute("SELECT kind FROM events ORDER BY id")]
    assert kinds == ["goal.created", "job.created"]
