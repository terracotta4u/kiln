import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from kiln.cli import app
from kiln.config import init_factory, load_config
from kiln.db import connect
from kiln.models import GoalStatus, TaskStatus
from kiln.notes import list_notes
from kiln.roles.worker import run_worker
from kiln.review import approve_task
from kiln.tasks import add_goal, add_task, get_task, list_tasks
from kiln.tick import run_tick, run_until_done

runner = CliRunner()


@pytest.fixture
def factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "kiln@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Kiln"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
    monkeypatch.chdir(tmp_path)
    init_factory(tmp_path)
    return tmp_path


def test_tick_scouts_plans_and_the_next_tick_merges(factory: Path):
    script = _agent(factory, _smart_agent())
    config = load_config(factory)
    conn = connect(config.db_path)
    try:
        add_goal(conn, "Ship it", "Add a marker file")
        first = run_tick(conn, config, agent_bin=str(script))
        task = list_tasks(conn)[0]
        on_main_after_work = (factory / "marker.txt").exists()
        second = run_tick(conn, config, agent_bin=str(script))
        stored = get_task(conn, task.id)
        goal = get_goal_row(conn)
    finally:
        conn.close()

    assert any("created task #1" in line for line in first.lines)
    assert any("task #1  review" in line for line in first.lines)
    assert task.status == TaskStatus.review
    assert on_main_after_work is False
    assert stored is not None
    assert stored.status == TaskStatus.done
    assert not (factory / "marker.txt").exists()
    marker = subprocess.run(
        ["git", "show", "kiln/goal-1-ship-it:marker.txt"],
        cwd=factory,
        check=True,
        capture_output=True,
        text=True,
    )
    assert marker.stdout == "ok\n"
    assert any("merged into kiln/goal-1-ship-it" in line for line in second.lines)
    assert not (factory / ".kiln" / "worktrees" / "1").exists()
    missing = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", "refs/heads/kiln/1-add-marker"],
        cwd=factory,
    )
    assert missing.returncode != 0
    assert goal == GoalStatus.active


def test_conflict_sends_the_task_back_for_rework(factory: Path):
    script = _agent(
        factory,
        "from pathlib import Path\n"
        "Path('README.md').write_text('worker\\n')\n"
        + _emit({"summary": "edited readme", "files": ["README.md"]}),
    )
    config = load_config(factory)
    conn = connect(config.db_path)
    try:
        goal = add_goal(conn, "Ship it")
        task = add_task(conn, goal.id, "Edit readme")
        run_worker(conn, config, task_id=task.id, agent_bin=str(script))
        from kiln.tasks import require_goal

        integration = require_goal(conn, goal.id).branch
        subprocess.run(["git", "checkout", integration], cwd=factory, check=True, capture_output=True)
        (factory / "README.md").write_text("goal\n")
        subprocess.run(["git", "commit", "-am", "goal edit"], cwd=factory, check=True, capture_output=True)
        subprocess.run(["git", "checkout", "main"], cwd=factory, check=True, capture_output=True)
        message = approve_task(conn, config, task.id)
        stored = get_task(conn, task.id)
    finally:
        conn.close()

    assert "rework" in message
    assert stored is not None
    assert stored.status == TaskStatus.pending
    assert stored.feedback is not None and "conflicted" in stored.feedback
    assert (factory / "README.md").read_text() == "hello\n"
    goal_readme = subprocess.run(
        ["git", "show", f"{integration}:README.md"],
        cwd=factory,
        check=True,
        capture_output=True,
        text=True,
    )
    assert goal_readme.stdout == "goal\n"
    assert subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{stored.branch}"],
        cwd=factory,
    ).returncode == 0


def test_scout_action_runs_during_the_tick(factory: Path):
    script = _agent(factory, _queue_agent())
    config = load_config(factory)
    conn = connect(config.db_path)
    try:
        goal = add_goal(conn, "Ship it")
        add_task(conn, goal.id, "Existing")
        first = run_tick(conn, config, agent_bin=str(script), dispatch=False)
        notes_after_first = list_notes(conn, goal.id)
        second = run_tick(conn, config, agent_bin=str(script), dispatch=False)
        notes = list_notes(conn, goal.id)
    finally:
        conn.close()

    assert any("scout note" in line for line in first.lines)
    assert notes_after_first and "scout-me please" in notes_after_first[0].text
    assert len(notes) == 1
    assert any("no actions" in line for line in second.lines)


def test_dry_run_changes_nothing(factory: Path):
    script = _agent(factory, "from pathlib import Path\nPath('agent-ran').write_text('x')\n")
    config = load_config(factory)
    conn = connect(config.db_path)
    try:
        add_goal(conn, "Ship it", "A factory")
        result = run_tick(conn, config, agent_bin=str(script), dry_run=True)
        tasks = list_tasks(conn)
    finally:
        conn.close()

    assert not (factory / "agent-ran").exists()
    assert tasks == []
    assert any("Ship it" in line for line in result.lines)
    assert any("Turn 1 of 25" in line for line in result.lines)
    assert any("would create branch" in line for line in result.lines)


def test_no_dispatch_leaves_the_task_pending(factory: Path):
    script = _agent(factory, _smart_agent())
    config = load_config(factory)
    conn = connect(config.db_path)
    try:
        add_goal(conn, "Ship it")
        run_tick(conn, config, agent_bin=str(script), dispatch=False)
        tasks = list_tasks(conn)
    finally:
        conn.close()

    assert len(tasks) == 1
    assert tasks[0].status == TaskStatus.pending
    assert not (factory / ".kiln" / "worktrees").exists()


def test_worker_limit_dispatches_one_of_two_ready_tasks(factory: Path):
    script = _agent(factory, _smart_agent())
    config = load_config(factory)
    conn = connect(config.db_path)
    try:
        goal = add_goal(conn, "Ship it")
        add_task(conn, goal.id, "First")
        add_task(conn, goal.id, "Second")
        run_tick(conn, config, workers=1, agent_bin=str(script))
        tasks = list_tasks(conn)
    finally:
        conn.close()

    statuses = sorted(task.status for task in tasks)
    assert statuses == [TaskStatus.pending, TaskStatus.review]


def test_two_workers_run_together(factory: Path):
    script = _agent(factory, _smart_agent())
    config = load_config(factory)
    conn = connect(config.db_path)
    try:
        goal = add_goal(conn, "Ship it")
        add_task(conn, goal.id, "First")
        add_task(conn, goal.id, "Second")
        run_tick(conn, config, workers=2, agent_bin=str(script))
        tasks = list_tasks(conn)
    finally:
        conn.close()

    assert [task.status for task in tasks] == [TaskStatus.review, TaskStatus.review]


def test_run_until_done_opens_one_pull_request(factory: Path):
    bare = factory.parent / f"{factory.name}-origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=factory, check=True)
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=factory, check=True, capture_output=True)
    script = _agent(factory, _smart_agent())
    gh = factory / "fake-gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "from pathlib import Path\n"
        f"Path({str(factory / 'pr-args.txt')!r}).write_text('\\n'.join(sys.argv[1:]))\n"
        "print('https://example.com/pull/1')\n"
    )
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    config = load_config(factory)
    conn = connect(config.db_path)
    try:
        add_goal(conn, "Ship it", "Add a marker file")
        result = run_until_done(conn, config, agent_bin=str(script), gh_bin=str(gh))
        goal = get_goal_row(conn)
        from kiln.tasks import list_goals

        stored = list_goals(conn)[0]
    finally:
        conn.close()

    assert goal == GoalStatus.done
    assert stored.pr_url == "https://example.com/pull/1"
    assert any("opened https://example.com/pull/1" in line for line in result.lines)
    assert not (factory / "marker.txt").exists()
    marker = subprocess.run(
        ["git", "show", "kiln/goal-1-ship-it:marker.txt"],
        cwd=factory,
        check=True,
        capture_output=True,
        text=True,
    )
    assert marker.stdout == "ok\n"
    subprocess.run(
        ["git", "ls-remote", "--exit-code", "--heads", "origin", "kiln/goal-1-ship-it"],
        cwd=factory,
        check=True,
        capture_output=True,
    )
    args = (factory / "pr-args.txt").read_text()
    assert "pr\ncreate" in args
    assert "--base\nmain" in args
    assert "--head\nkiln/goal-1-ship-it" in args


def test_turn_cap_stops_after_notes(factory: Path):
    script = _agent(factory, _note_agent())
    config = load_config(factory)
    conn = connect(config.db_path)
    try:
        goal = add_goal(conn, "Ship it")
        result = run_until_done(conn, config, agent_bin=str(script), turns=2)
        notes = list_notes(conn, goal.id)
        status = get_goal_row(conn)
    finally:
        conn.close()

    assert len(notes) == 2
    assert status == GoalStatus.active
    assert "stopped: reached 2 foreman turns" in result.lines


def test_two_foreman_failures_stop_the_run(factory: Path):
    script = _agent(factory, "print('not a report')\n")
    config = load_config(factory)
    conn = connect(config.db_path)
    try:
        add_goal(conn, "Ship it")
        result = run_until_done(conn, config, agent_bin=str(script), turns=5)
    finally:
        conn.close()

    failures = [line for line in result.lines if line.startswith("foreman failed")]
    assert len(failures) == 2
    assert "stopped: foreman failed twice in a row" in result.lines
    assert not any("reached" in line for line in result.lines)


def test_no_progress_stops_the_run(factory: Path):
    script = _agent(
        factory,
        "import sys\n"
        "prompt = sys.argv[-1]\n"
        "if 'You are the Kiln foreman' in prompt:\n"
        f"    {_emit_call('{\"actions\": []}')}\n",
    )
    config = load_config(factory)
    conn = connect(config.db_path)
    try:
        add_goal(conn, "Ship it")
        result = run_until_done(conn, config, agent_bin=str(script), turns=5)
        status = get_goal_row(conn)
    finally:
        conn.close()

    assert status == GoalStatus.active
    assert any("no actions" in line for line in result.lines)
    assert "stopped: a turn made no progress" in result.lines


def test_cli_review_approves(factory: Path, monkeypatch: pytest.MonkeyPatch):
    script = _agent(
        factory,
        "from pathlib import Path\n"
        "Path('marker.txt').write_text('ok\\n')\n"
        + _emit({"summary": "added marker", "files": ["marker.txt"]}),
    )
    monkeypatch.setenv("KILN_AGENT_BIN", str(script))
    assert runner.invoke(app, ["goal", "add", "Ship it"]).exit_code == 0
    assert runner.invoke(app, ["task", "add", "1", "Add marker"]).exit_code == 0
    worked = runner.invoke(app, ["work"])
    assert worked.exit_code == 0, worked.output
    approved = runner.invoke(app, ["review", "1", "--approve"])
    assert approved.exit_code == 0, approved.output
    assert "merged into kiln/goal-1-ship-it" in approved.output
    assert not (factory / "marker.txt").exists()
    marker = subprocess.run(
        ["git", "show", "kiln/goal-1-ship-it:marker.txt"],
        cwd=factory,
        check=True,
        capture_output=True,
        text=True,
    )
    assert marker.stdout == "ok\n"


def _smart_agent() -> str:
    return f"""
import json, sys
from pathlib import Path
prompt = sys.argv[-1]
if "You are a scout" in prompt:
    {_emit_call('{"summary": "repo has a readme", "findings": ["README.md"]}')}
elif "You are the Kiln foreman" in prompt:
    if "[review]" in prompt:
        {_emit_call('{"actions": [{"type": "approve", "task_id": 1}]}')}
    elif "Tasks:\\n(none)" in prompt:
        {_emit_call('{"actions": [{"type": "create_task", "ref": "marker", "title": "Add marker", "description": "Write marker.txt", "acceptance": "file exists", "depends_on": [], "priority": 1}, {"type": "dispatch", "ref": "marker"}]}')}
    elif "[pending]" in prompt:
        {_emit_call('{"actions": [{"type": "dispatch", "task_id": 1}, {"type": "dispatch", "task_id": 2}]}')}
    else:
        {_emit_call('{"actions": []}')}
else:
    Path("marker.txt").write_text("ok\\n")
    {_emit_call('{"summary": "added marker", "files": ["marker.txt"]}')}
"""


def _note_agent() -> str:
    return (
        "import sys\n"
        "prompt = sys.argv[-1]\n"
        "if 'You are the Kiln foreman' in prompt:\n"
        f"    {_emit_call('{\"actions\": [{\"type\": \"note\", \"text\": \"still going\"}]}')}\n"
    )


def _queue_agent() -> str:
    return f"""
import sys
prompt = sys.argv[-1]
if "You are a scout" in prompt:
    {_emit_call('{"summary": "answered", "findings": []}')}
elif "You are the Kiln foreman" in prompt:
    if "scout-me please" in prompt:
        {_emit_call('{"actions": []}')}
    else:
        {_emit_call('{"actions": [{"type": "scout", "question": "scout-me please"}]}')}
else:
    {_emit_call('{"summary": "worker", "files": []}')}
"""


def _emit_call(payload: str) -> str:
    return "print(" + json.dumps(_envelope(payload)) + ")"


def _emit(payload: dict) -> str:
    return "print(" + json.dumps(_envelope(json.dumps(payload))) + ")\n"


def _envelope(result_body: str) -> str:
    if not result_body.startswith("{"):
        body = result_body
    else:
        body = result_body
    report = body if body.startswith("{") else body
    # result_body is already a JSON object string.
    text = "```json\n" + report + "\n```"
    return json.dumps(
        {"type": "result", "subtype": "success", "is_error": False, "result": text}
    )


def _agent(directory: Path, body: str) -> Path:
    path = directory / "fake-agent"
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def get_goal_row(conn):
    from kiln.tasks import list_goals

    return list_goals(conn)[0].status
