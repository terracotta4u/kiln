import stat
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from kiln.cli import app
from kiln.config import init_factory, load_config
from kiln.db import connect
from kiln.git import branch_exists, ensure_worktree
from kiln.gc import cleanup
from kiln.jobs import add_goal, add_job, get_job, set_integration, set_job_status, start_attempt
from kiln.models import Integration, JobStatus
from kiln.tick import run_tick

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


def test_cleanup_drops_finished_worktrees_and_keeps_live_ones(factory: Path):
    config = load_config(factory)
    conn = connect(config.db_path)
    try:
        goal = add_goal(conn, "Ship it")
        done = _checkout(conn, factory, add_job(conn, goal.id, "Schema"), "kiln/1-schema")
        review = _checkout(conn, factory, add_job(conn, goal.id, "CLI"), "kiln/2-cli")
        failed = _checkout(conn, factory, add_job(conn, goal.id, "Docs"), "kiln/3-docs")
        set_job_status(conn, done.id, JobStatus.completed)
        set_integration(conn, done.id, Integration.merged)
        set_job_status(conn, review.id, JobStatus.completed)
        set_integration(conn, review.id, Integration.pending)
        set_job_status(conn, failed.id, JobStatus.failed)
        leftover = factory / ".kiln" / "worktrees" / "99"
        leftover.mkdir(parents=True)
        (leftover / "junk.txt").write_text("x\n")

        lines = cleanup(conn, config)
        again = cleanup(conn, config)
        done_row = get_job(conn, done.id)
        review_row = get_job(conn, review.id)
        failed_row = get_job(conn, failed.id)
    finally:
        conn.close()

    assert any("removed worktree for job #1" in line for line in lines)
    assert any("deleted branch kiln/1-schema" in line for line in lines)
    assert any("removed leftover 99" in line for line in lines)
    assert again == []
    assert done_row is not None and done_row.worktree_path is None and done_row.branch is None
    assert not (factory / ".kiln" / "worktrees" / "1").exists()
    assert not branch_exists(factory, "kiln/1-schema")
    assert review_row is not None and review_row.branch == "kiln/2-cli"
    assert (factory / ".kiln" / "worktrees" / "2").is_dir()
    assert branch_exists(factory, "kiln/2-cli")
    assert failed_row is not None and failed_row.branch == "kiln/3-docs"
    assert failed_row.worktree_path is None
    assert not (factory / ".kiln" / "worktrees" / "3").exists()
    assert branch_exists(factory, "kiln/3-docs")
    assert not leftover.exists()


def test_cleanup_keeps_a_merged_branch_when_configured(factory: Path):
    text = (factory / "kiln.toml").read_text().replace(
        "delete_merged_branches = true",
        "delete_merged_branches = false",
    )
    (factory / "kiln.toml").write_text(text)
    config = load_config(factory)
    conn = connect(config.db_path)
    try:
        goal = add_goal(conn, "Ship it")
        task = _checkout(conn, factory, add_job(conn, goal.id, "Schema"), "kiln/1-schema")
        set_job_status(conn, task.id, JobStatus.completed)
        set_integration(conn, task.id, Integration.merged)
        preview = cleanup(conn, config, dry_run=True)
        assert (factory / ".kiln" / "worktrees" / "1").is_dir()
        lines = cleanup(conn, config)
        stored = get_job(conn, task.id)
    finally:
        conn.close()

    assert any("would remove worktree for job #1" in line for line in preview)
    assert not any("would delete branch" in line for line in preview)
    assert any("removed worktree for job #1" in line for line in lines)
    assert stored is not None and stored.branch == "kiln/1-schema"
    assert branch_exists(factory, "kiln/1-schema")
    assert not (factory / ".kiln" / "worktrees" / "1").exists()


def test_cleanup_leaves_a_worktree_outside_kiln(factory: Path, tmp_path: Path):
    config = load_config(factory)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("stay\n")
    conn = connect(config.db_path)
    try:
        goal = add_goal(conn, "Ship it")
        task = add_job(conn, goal.id, "Schema")
        start_attempt(conn, task.id, branch="kiln/1-schema", worktree_path=str(outside))
        set_job_status(conn, task.id, JobStatus.completed)
        set_integration(conn, task.id, Integration.merged)
        lines = cleanup(conn, config)
    finally:
        conn.close()

    assert any("left worktree for job #1" in line for line in lines)
    assert (outside / "keep.txt").is_file()


def test_tick_cleans_before_the_foreman(factory: Path):
    config = load_config(factory)
    conn = connect(config.db_path)
    script = factory / "fake-agent"
    script.write_text("#!" + sys.executable + "\n" + _EMPTY_FOREMAN)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    try:
        goal = add_goal(conn, "Ship it")
        task = _checkout(conn, factory, add_job(conn, goal.id, "Schema"), "kiln/1-schema")
        set_job_status(conn, task.id, JobStatus.completed)
        set_integration(conn, task.id, Integration.merged)
        dry = run_tick(conn, config, dry_run=True)
        assert (factory / ".kiln" / "worktrees" / "1").is_dir()
        result = run_tick(conn, config, agent_bin=str(script))
    finally:
        conn.close()

    assert any("would remove worktree for job #1" in line for line in dry.lines)
    assert any("removed worktree for job #1" in line for line in result.lines)
    assert not (factory / ".kiln" / "worktrees" / "1").exists()


def test_cli_gc(factory: Path):
    config = load_config(factory)
    conn = connect(config.db_path)
    try:
        goal = add_goal(conn, "Ship it")
        task = _checkout(conn, factory, add_job(conn, goal.id, "Schema"), "kiln/1-schema")
        set_job_status(conn, task.id, JobStatus.completed)
        set_integration(conn, task.id, Integration.merged)
    finally:
        conn.close()

    cleaned = runner.invoke(app, ["gc"])
    assert cleaned.exit_code == 0, cleaned.output
    assert "removed worktree for job #1" in cleaned.output
    assert "deleted branch kiln/1-schema" in cleaned.output
    again = runner.invoke(app, ["gc"])
    assert again.exit_code == 0, again.output
    assert "nothing to clean" in again.output


def _checkout(conn, factory: Path, task, branch: str):
    worktree = factory / ".kiln" / "worktrees" / str(task.id)
    ensure_worktree(factory, worktree, branch, "main")
    start_attempt(conn, task.id, branch=branch, worktree_path=str(worktree.resolve()))
    return task


_EMPTY_FOREMAN = (
    "import json, sys\n"
    "print(json.dumps({'type':'result','subtype':'success','is_error':False,"
    "'result':'```json\\n' + json.dumps({'actions': []}) + '\\n```'}))\n"
)
