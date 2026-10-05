from kiln.db import connect, migrate
from kiln.models import GoalStatus, TaskStatus
from kiln.queue import pending_scouts
from kiln.roles.foreman import apply_actions
from kiln.tasks import add_goal, add_task, get_task, list_tasks, set_task_status


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


def test_apply_notes_scouts_cancel_and_goal_done(tmp_path):
    conn = _db(tmp_path)
    goal = add_goal(conn, "Ship it")
    task = add_task(conn, goal.id, "Schema")
    messages = apply_actions(
        conn,
        _config(),
        goal,
        [
            {"type": "note", "text": "looked around"},
            {"type": "request_scout", "question": "Where is the CLI?"},
            {"type": "cancel", "task_id": task.id},
            {"type": "goal_done"},
            {"type": "frobnicate"},
        ],
    )
    assert any(message.startswith("note #") for message in messages)
    assert pending_scouts(conn, goal.id) == [(1, "Where is the CLI?")]
    stored = get_task(conn, task.id)
    assert stored is not None and stored.status == TaskStatus.cancelled
    assert any("done" in message for message in messages)
    assert get_goal_status(conn, goal.id) == GoalStatus.done
    assert any("unknown action" in message for message in messages)
    conn.close()


def test_goal_done_waits_for_open_tasks(tmp_path):
    conn = _db(tmp_path)
    goal = add_goal(conn, "Ship it")
    add_task(conn, goal.id, "Schema")
    messages = apply_actions(conn, _config(), goal, [{"type": "goal_done"}])
    assert "open tasks" in messages[0]
    assert get_goal_status(conn, goal.id) == GoalStatus.active
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
