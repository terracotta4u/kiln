from kiln.db import connect, migrate
from kiln.models import GoalStatus, TaskStatus
from kiln.roles.foreman import apply_actions, goal_brief
from kiln.tasks import (
    add_dependency,
    add_goal,
    add_task,
    get_task,
    list_tasks,
    require_goal,
    set_task_status,
)


def test_apply_creates_a_dependency_chain(tmp_path):
    conn = _db(tmp_path)
    goal = add_goal(conn, "Ship it")
    messages = apply_actions(
        conn,
        _config(),
        goal,
        [
            {
                "type": "create_task",
                "ref": "schema",
                "title": "Schema",
                "description": "tables",
                "acceptance": "rows persist",
                "priority": 2,
            },
            {
                "type": "create_task",
                "ref": "cli",
                "title": "CLI",
                "depends_on": ["schema"],
                "priority": 1,
            },
        ],
    )
    tasks = list_tasks(conn, goal_id=goal.id)
    assert [task.title for task in tasks] == ["Schema", "CLI"]
    assert tasks[0].priority == 2
    assert "depending on #1" in messages[1]
    ready = [task.title for task in tasks if task.status == TaskStatus.pending]
    assert ready == ["Schema", "CLI"]
    conn.close()


def test_apply_notes_brief_and_goal_done(tmp_path):
    conn = _db(tmp_path)
    goal = add_goal(conn, "Ship it")
    task = add_task(conn, goal.id, "Schema")
    messages = apply_actions(
        conn,
        _config(),
        goal,
        [
            {"type": "note", "text": "looked around"},
            {"type": "update_brief", "text": "Success: the schema exists."},
            {"type": "cancel", "task_id": task.id},
            {"type": "goal_done"},
            {"type": "goal_done", "evidence": ["schema task was cancelled"]},
            {"type": "frobnicate"},
        ],
    )
    stored = get_task(conn, task.id)
    finished = require_goal(conn, goal.id)
    assert any(message.startswith("note #") for message in messages)
    assert stored is not None and stored.status == TaskStatus.cancelled
    assert finished.brief == "Success: the schema exists."
    assert any("requires evidence" in message for message in messages)
    assert finished.status == GoalStatus.done
    assert finished.evidence == ("schema task was cancelled",)
    assert any("unknown action" in message for message in messages)
    conn.close()


def test_goal_done_waits_for_open_tasks(tmp_path):
    conn = _db(tmp_path)
    goal = add_goal(conn, "Ship it")
    add_task(conn, goal.id, "Schema")
    messages = apply_actions(
        conn,
        _config(),
        goal,
        [{"type": "goal_done", "evidence": ["not yet"]}],
    )
    assert "open tasks" in messages[0]
    assert get_goal_status(conn, goal.id) == GoalStatus.active
    assert require_goal(conn, goal.id).evidence is None
    conn.close()


def test_dispatch_respects_the_limit_and_blocked_tasks(tmp_path):
    conn = _db(tmp_path)
    goal = add_goal(conn, "Ship it")
    first = add_task(conn, goal.id, "Schema")
    second = add_task(conn, goal.id, "CLI")
    add_dependency(conn, second.id, first.id)
    limited = apply_actions(
        conn,
        _config(),
        goal,
        [
            {"type": "dispatch", "task_id": first.id},
            {"type": "dispatch", "task_id": second.id},
        ],
        worker_limit=0,
    )
    blocked = apply_actions(conn, _config(), goal, [{"type": "dispatch", "task_id": second.id}])
    review = apply_actions(conn, _config(), goal, [{"type": "review", "task_id": first.id}])
    assert limited == ["limit reached, dispatch next turn", "limit reached, dispatch next turn"]
    assert "blocked" in blocked[0]
    assert "only a task in review" in review[0]
    assert [task.status for task in list_tasks(conn, goal_id=goal.id)] == [
        TaskStatus.pending,
        TaskStatus.pending,
    ]
    conn.close()


def test_review_and_a_decision_in_one_turn_are_refused(tmp_path):
    conn = _db(tmp_path)
    goal = add_goal(conn, "Ship it")
    first = add_task(conn, goal.id, "Schema")
    second = add_task(conn, goal.id, "CLI")
    third = add_task(conn, goal.id, "Docs")
    set_task_status(conn, first.id, TaskStatus.review)
    set_task_status(conn, second.id, TaskStatus.review)
    set_task_status(conn, third.id, TaskStatus.review)
    messages = apply_actions(
        conn,
        _config(),
        goal,
        [
            {"type": "review", "task_id": first.id},
            {"type": "approve", "task_id": first.id},
            {"type": "note", "text": "still here"},
            {"type": "rework", "task_id": second.id, "feedback": "split the command"},
            {"type": "review", "task_id": second.id},
            {"type": "fail", "task_id": third.id, "reason": "wrong approach"},
        ],
    )
    assert messages[0] == f"action 1 failed: cannot review and approve, rework, or fail task #{first.id} in one turn"
    assert messages[1] == f"action 2 failed: cannot review and approve, rework, or fail task #{first.id} in one turn"
    assert messages[2].startswith("note #")
    assert messages[3] == f"action 4 failed: cannot review and approve, rework, or fail task #{second.id} in one turn"
    assert messages[4] == f"action 5 failed: cannot review and approve, rework, or fail task #{second.id} in one turn"
    assert messages[5] == f"task #{third.id} failed"
    stored = [get_task(conn, task_id) for task_id in (first.id, second.id, third.id)]
    assert stored[0] is not None and stored[0].status == TaskStatus.review
    assert stored[1] is not None and stored[1].status == TaskStatus.review
    assert stored[2] is not None and stored[2].status == TaskStatus.failed
    conn.close()


def test_goal_brief_shows_the_brief_and_what_is_ready(tmp_path):
    conn = _db(tmp_path)
    goal = add_goal(conn, "Ship it")
    from kiln.tasks import set_goal_brief

    set_goal_brief(conn, goal.id, "Success: both tasks land.")
    first = add_task(conn, goal.id, "Schema")
    second = add_task(conn, goal.id, "CLI")
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
        [{"type": "create_task", "title": "CLI", "depends_on": ["missing"]}],
    )
    assert "dependency failed" in messages[0]
    assert [task.title for task in list_tasks(conn, goal_id=goal.id)] == ["CLI"]
    conn.close()


def test_actions_cannot_touch_another_goal(tmp_path):
    conn = _db(tmp_path)
    first = add_goal(conn, "One")
    second = add_goal(conn, "Two")
    task = add_task(conn, second.id, "Elsewhere")
    set_task_status(conn, task.id, TaskStatus.review)
    messages = apply_actions(conn, _config(), first, [{"type": "fail", "task_id": task.id, "reason": "no"}])
    assert "not in goal" in messages[0]
    stored = get_task(conn, task.id)
    assert stored is not None and stored.status == TaskStatus.review
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
    from kiln.tasks import require_goal

    return require_goal(conn, goal_id).status
