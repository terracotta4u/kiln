import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from kiln.config import init_factory, load_config
from kiln.db import connect
from kiln.errors import KilnError
from kiln.git import commit_if_dirty, remove_worktree
from kiln.models import Integration, JobRole, JobStatus, RunStatus
from kiln.roles.foreman import goal_brief
from kiln.roles.reviewer import DIFF_CHAR_LIMIT, run_reviewer
from kiln.roles.worker import run_worker
from kiln.runs import finish_run, start_run
from kiln.jobs import add_goal, add_job, get_job, require_goal


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


def test_reviewer_sees_the_diff_and_leaves_the_task_in_review(factory: Path):
    worker = _agent(factory, "worker-agent", _worker_script())
    reviewer = _agent(factory, "reviewer-agent", _reviewer_script())
    config = load_config(factory)
    conn = connect(config.db_path)
    try:
        goal = add_goal(conn, "Ship it", "Add a marker")
        task = add_job(conn, goal.id, "Add marker", acceptance="marker.txt exists")
        run_worker(conn, config, task_id=task.id, agent_bin=str(worker))
        reviewed = get_job(conn, task.id)
        assert reviewed is not None and reviewed.worktree_path
        remove_worktree(factory, Path(reviewed.worktree_path))

        outcome = run_reviewer(conn, config, task.id, focus="check the marker", agent_bin=str(reviewer))
        stored = get_job(conn, task.id)
        brief = goal_brief(conn, config, require_goal(conn, goal.id))
        log = Path(outcome.run.log_path or "").read_text()

        later = start_run(conn, role="worker", model="worker", job_id=task.id)
        finish_run(
            conn,
            later.id,
            status=RunStatus.succeeded,
            exit_code=0,
            log_path=None,
            report={"worker": {"summary": "second attempt"}, "verify": {}},
        )
        hidden = goal_brief(conn, config, require_goal(conn, goal.id))
    finally:
        conn.close()

    assert outcome.failure is None
    assert outcome.verdict == "needs_changes"
    assert outcome.job.role == JobRole.reviewer
    assert outcome.job.target_job_id == task.id
    assert outcome.job.status == JobStatus.completed
    review = json.loads(outcome.job.result or "")
    assert review["role_result"]["verdict"] == "needs_changes"
    assert stored is not None
    assert stored.status == JobStatus.completed
    assert stored.integration == Integration.pending
    worker_result = json.loads(stored.result or "")
    assert worker_result["summary"] == "added marker"
    assert (Path(stored.worktree_path or "") / "reviewer-was-here").is_file()
    assert not (factory / "reviewer-was-here").exists()
    command, _prompt = log.split("--- prompt ---", 1)
    assert "--mode ask" in command
    assert "--force" not in command
    assert "claude-sonnet-5-thinking-high" in command
    assert "review-me-token" in log
    assert "check the marker" in log
    assert "needs_changes" in brief
    assert "marker is one line" in brief
    assert "diffstat" not in brief
    assert "needs_changes" in hidden
    assert "added marker" in hidden


def test_reviewer_truncates_a_long_diff(factory: Path):
    worker = _agent(factory, "worker-agent", _worker_script())
    reviewer = _agent(factory, "reviewer-agent", _reviewer_script())
    config = load_config(factory)
    conn = connect(config.db_path)
    try:
        goal = add_goal(conn, "Ship it")
        task = add_job(conn, goal.id, "Add marker")
        run_worker(conn, config, task_id=task.id, agent_bin=str(worker))
        stored = get_job(conn, task.id)
        assert stored is not None and stored.worktree_path
        worktree = Path(stored.worktree_path)
        (worktree / "big.txt").write_text("x" * (DIFF_CHAR_LIMIT + 5000))
        commit_if_dirty(worktree, "add a large file")
        outcome = run_reviewer(conn, config, task.id, agent_bin=str(reviewer))
        log = Path(outcome.run.log_path or "").read_text()
    finally:
        conn.close()

    assert outcome.failure is None
    assert "[diff truncated," in log


def test_reviewer_rejects_a_task_that_is_not_in_review(factory: Path):
    config = load_config(factory)
    conn = connect(config.db_path)
    try:
        goal = add_goal(conn, "Ship it")
        task = add_job(conn, goal.id, "Add marker")
        with pytest.raises(KilnError, match="pending integration"):
            run_reviewer(conn, config, task.id, agent_bin=str(factory / "missing"))
    finally:
        conn.close()


def test_reviewer_records_a_bad_report_as_a_failure(factory: Path):
    worker = _agent(factory, "worker-agent", _worker_script())
    reviewer = _agent(factory, "reviewer-agent", "print('no json here')\n")
    config = load_config(factory)
    conn = connect(config.db_path)
    try:
        goal = add_goal(conn, "Ship it")
        task = add_job(conn, goal.id, "Add marker")
        run_worker(conn, config, task_id=task.id, agent_bin=str(worker))
        outcome = run_reviewer(conn, config, task.id, agent_bin=str(reviewer))
        stored = get_job(conn, task.id)
    finally:
        conn.close()

    assert outcome.failure == "response had no JSON report"
    assert outcome.run.status == RunStatus.failed
    assert outcome.job.role == JobRole.reviewer
    assert outcome.job.status == JobStatus.failed
    assert outcome.job.result is None
    assert stored is not None and stored.status == JobStatus.completed
    assert stored.integration == Integration.pending
    assert stored.result is not None


def _worker_script() -> str:
    return (
        "from pathlib import Path\n"
        "Path('marker.txt').write_text('review-me-token\\n')\n"
        + _emit({"summary": "added marker", "files": ["marker.txt"]})
    )


def _reviewer_script() -> str:
    return (
        "from pathlib import Path\n"
        "Path('reviewer-was-here').write_text('ok\\n')\n"
        + _emit(
            {
                "verdict": "needs_changes",
                "summary": "marker is one line",
                "findings": ["add a newline comment"],
                "confidence": 0.8,
            }
        )
    )


def _emit(payload: dict) -> str:
    text = "```json\n" + json.dumps(payload) + "\n```"
    envelope = {"type": "result", "subtype": "success", "is_error": False, "result": text}
    return "print(" + json.dumps(json.dumps(envelope)) + ")\n"


def _agent(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path
