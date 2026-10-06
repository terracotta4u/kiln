from kiln.db import connect, migrate
from kiln.jobs import (
    add_dependency,
    add_goal,
    add_job,
    get_job,
    list_jobs,
    require_goal,
    set_goal_brief,
    set_integration,
    set_job_status,
)
from kiln.models import GoalStatus, Integration, JobRole, JobStatus
from kiln.roles.foreman import apply_actions, goal_brief


def test_apply_creates_a_dependency_chain(tmp_path):
    conn = _db(tmp_path)
    goal = add_goal(conn, "Ship it")
    messages = apply_actions(
        conn,
        _config(),
        goal,
        [
            {
                "type": "create_job",
                "ref": "schema",
                "role": "worker",
                "title": "Schema",
                "description": "tables",
                "acceptance": "rows persist",
                "priority": 2,
            },
            {
                "type": "create_job",
                "ref": "cli",
                "role": "worker",
                "title": "CLI",
                "depends_on": ["schema"],
                "priority": 1,
            },
        ],
    )
    tasks = list_jobs(conn, goal_id=goal.id)
    assert [task.title for task in tasks] == ["Schema", "CLI"]
    assert tasks[0].priority == 2
    assert "depending on #1" in messages[1]
    ready = [task.title for task in tasks if task.status == JobStatus.pending]
    assert ready == ["Schema", "CLI"]
    conn.close()


def test_apply_notes_brief_and_goal_done(tmp_path):
    conn = _db(tmp_path)
    goal = add_goal(conn, "Ship it")
    task = add_job(conn, goal.id, "Schema")
    messages = apply_actions(
        conn,
        _config(),
        goal,
        [
            {"type": "note", "text": "looked around"},
            {"type": "update_brief", "text": "Success: the schema exists."},
            {"type": "cancel", "job_id": task.id},
            {"type": "goal_done"},
            {"type": "goal_done", "evidence": ["schema task was cancelled"]},
            {"type": "frobnicate"},
        ],
    )
    stored = get_job(conn, task.id)
    finished = require_goal(conn, goal.id)
    assert any(message.startswith("note #") for message in messages)
    assert stored is not None and stored.status == JobStatus.cancelled
    assert finished.brief == "Success: the schema exists."
    assert any("requires evidence" in message for message in messages)
    assert finished.status == GoalStatus.done
    assert finished.evidence == ("schema task was cancelled",)
    assert any("unknown action" in message for message in messages)
    conn.close()


def test_goal_done_waits_for_open_tasks(tmp_path):
    conn = _db(tmp_path)
    goal = add_goal(conn, "Ship it")
    add_job(conn, goal.id, "Schema")
    messages = apply_actions(
        conn,
        _config(),
        goal,
        [{"type": "goal_done", "evidence": ["not yet"]}],
    )
    assert "open jobs" in messages[0]
    assert get_goal_status(conn, goal.id) == GoalStatus.active
    assert require_goal(conn, goal.id).evidence is None
    conn.close()


def test_dispatch_respects_the_limit_and_blocked_tasks(tmp_path):
    conn = _db(tmp_path)
    goal = add_goal(conn, "Ship it")
    first = add_job(conn, goal.id, "Schema")
    second = add_job(conn, goal.id, "CLI")
    add_dependency(conn, second.id, first.id)
    limited = apply_actions(
        conn,
        _config(),
        goal,
        [
            {"type": "dispatch", "job_id": first.id},
            {"type": "dispatch", "job_id": second.id},
        ],
        worker_limit=0,
    )
    blocked = apply_actions(conn, _config(), goal, [{"type": "dispatch", "job_id": second.id}])
    set_job_status(conn, first.id, JobStatus.completed)
    set_integration(conn, first.id, Integration.merged)
    reviewer = add_job(
        conn,
        goal.id,
        "Review schema",
        role=JobRole.reviewer,
        target_job_id=first.id,
        depends_on=[first.id],
    )
    review = apply_actions(conn, _config(), goal, [{"type": "dispatch", "job_id": reviewer.id}])
    assert limited == ["limit reached, dispatch next turn", "limit reached, dispatch next turn"]
    assert "blocked" in blocked[0]
    assert "pending integration" in review[0]
    stored = {job.id: job for job in list_jobs(conn, goal_id=goal.id)}
    assert stored[first.id].status == JobStatus.completed
    assert stored[second.id].status == JobStatus.pending
    assert stored[reviewer.id].status == JobStatus.pending
    conn.close()


def test_reviewer_dispatch_and_a_decision_in_one_turn_are_refused(tmp_path):
    conn = _db(tmp_path)
    goal = add_goal(conn, "Ship it")
    first = add_job(conn, goal.id, "Schema")
    second = add_job(conn, goal.id, "CLI")
    third = add_job(conn, goal.id, "Docs")
    for job in (first, second, third):
        set_job_status(conn, job.id, JobStatus.completed)
        set_integration(conn, job.id, Integration.pending)
    reviewer = add_job(
        conn,
        goal.id,
        "Review schema",
        role=JobRole.reviewer,
        target_job_id=first.id,
        depends_on=[first.id],
    )
    conn.execute(
        "UPDATE jobs SET result = ? WHERE id = ?",
        ('{"summary": "wrote docs"}', third.id),
    )
    messages = apply_actions(
        conn,
        _config(),
        goal,
        [
            {"type": "dispatch", "job_id": reviewer.id},
            {"type": "approve", "job_id": first.id},
            {"type": "note", "text": "still here"},
            {"type": "rework", "job_id": second.id, "feedback": "split the command"},
            {"type": "reject", "job_id": third.id, "reason": "wrong approach"},
        ],
    )
    refusal = f"cannot dispatch a reviewer and approve or reject job #{first.id} in one turn"
    assert messages[0] == f"action 1 failed: {refusal}"
    assert messages[1] == f"action 2 failed: {refusal}"
    assert messages[2].startswith("note #")
    assert "sent back for rework" in messages[3]
    assert messages[4] == f"job #{third.id} rejected"
    stored = [get_job(conn, job_id) for job_id in (first.id, second.id, third.id, reviewer.id)]
    assert stored[0] is not None and stored[0].status == JobStatus.completed
    assert stored[0].integration == Integration.pending
    assert stored[1] is not None and stored[1].status == JobStatus.pending
    assert stored[1].integration is None
    assert stored[1].feedback == "split the command"
    assert stored[2] is not None and stored[2].status == JobStatus.completed
    assert stored[2].integration == Integration.rejected
    assert stored[2].result is not None and "wrote docs" in stored[2].result
    assert stored[3] is not None and stored[3].status == JobStatus.pending
    conn.close()


def test_creating_a_reviewer_and_deciding_its_target_in_one_turn_is_refused(tmp_path):
    conn = _db(tmp_path)
    goal = add_goal(conn, "Ship it")
    worker = add_job(conn, goal.id, "Schema")
    set_job_status(conn, worker.id, JobStatus.completed)
    set_integration(conn, worker.id, Integration.pending)
    messages = apply_actions(
        conn,
        _config(),
        goal,
        [
            {
                "type": "create_job",
                "ref": "check",
                "role": "reviewer",
                "title": "Review",
                "target_job_id": worker.id,
                "depends_on": [worker.id],
            },
            {"type": "dispatch", "ref": "check"},
            {"type": "approve", "job_id": worker.id},
            {"type": "note", "text": "kept"},
        ],
    )
    assert messages[0].startswith("created job #")
    assert "cannot dispatch a reviewer and approve or reject" in messages[1]
    assert "cannot dispatch a reviewer and approve or reject" in messages[2]
    assert messages[3].startswith("note #")
    stored = get_job(conn, worker.id)
    reviewer = [job for job in list_jobs(conn, goal_id=goal.id) if job.role == JobRole.reviewer]
    assert stored is not None and stored.status == JobStatus.completed
    assert stored.integration == Integration.pending
    assert len(reviewer) == 1 and reviewer[0].status == JobStatus.pending
    conn.close()


def test_a_reviewer_needs_a_worker_target(tmp_path):
    conn = _db(tmp_path)
    goal = add_goal(conn, "Ship it")
    worker = add_job(conn, goal.id, "Schema")
    missing = apply_actions(
        conn,
        _config(),
        goal,
        [{"type": "create_job", "role": "reviewer", "title": "Review", "depends_on": [worker.id]}],
    )
    omitted = apply_actions(
        conn,
        _config(),
        goal,
        [
            {
                "type": "create_job",
                "role": "reviewer",
                "title": "Review",
                "target_job_id": worker.id,
                "depends_on": [],
            }
        ],
    )
    assert "target_job_id" in missing[0]
    assert "depends_on" in omitted[0]
    assert [job.title for job in list_jobs(conn, goal_id=goal.id)] == ["Schema"]
    conn.close()


def test_goal_brief_shows_the_brief_and_what_is_ready(tmp_path):
    conn = _db(tmp_path)
    goal = add_goal(conn, "Ship it")
    set_goal_brief(conn, goal.id, "Success: both tasks land.")
    first = add_job(conn, goal.id, "Schema")
    second = add_job(conn, goal.id, "CLI")
    add_dependency(conn, second.id, first.id)
    text = goal_brief(conn, _config(), require_goal(conn, goal.id))
    assert "Success: both tasks land." in text
    assert f"#{first.id} [pending] ready" in text
    assert f"#{second.id} [pending] blocked by #{first.id}" in text
    assert "Workers this turn: 2" in text
    conn.close()


def test_bad_dependency_keeps_the_created_task(tmp_path):
    conn = _db(tmp_path)
    goal = add_goal(conn, "Ship it")
    messages = apply_actions(
        conn,
        _config(),
        goal,
        [{"type": "create_job", "role": "worker", "title": "CLI", "depends_on": ["missing"]}],
    )
    assert "dependency failed" in messages[0]
    assert [task.title for task in list_jobs(conn, goal_id=goal.id)] == ["CLI"]
    conn.close()


def test_actions_cannot_touch_another_goal(tmp_path):
    conn = _db(tmp_path)
    first = add_goal(conn, "One")
    second = add_goal(conn, "Two")
    task = add_job(conn, second.id, "Elsewhere")
    set_job_status(conn, task.id, JobStatus.completed)
    set_integration(conn, task.id, Integration.pending)
    messages = apply_actions(conn, _config(), first, [{"type": "reject", "job_id": task.id, "reason": "no"}])
    assert "not in goal" in messages[0]
    stored = get_job(conn, task.id)
    assert stored is not None and stored.status == JobStatus.completed
    assert stored.integration == Integration.pending
    conn.close()


def _db(tmp_path):
    conn = connect(tmp_path / "kiln.db")
    migrate(conn)
    return conn


def _config():
    from pathlib import Path

    from kiln.config import Config, Models

    root = Path("/tmp/kiln-test")
    return Config(
        repo_root=root,
        kiln_dir=root / ".kiln",
        db_path=root / ".kiln" / "kiln.db",
        toml_path=root / "kiln.toml",
        base_branch="main",
        verify="",
        max_parallel_workers=2,
        max_attempts=3,
        max_foreman_turns=25,
        delete_merged_branches=True,
        models=Models(foreman="foreman", worker="worker", scout="scout", reviewer="reviewer"),
    )


def get_goal_status(conn, goal_id):
    return require_goal(conn, goal_id).status
