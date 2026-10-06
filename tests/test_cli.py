import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from kiln.cli import app
from kiln.config import load_config
from kiln.errors import KilnError

runner = CliRunner()


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_init_writes_config_database_and_gitignore(repo):
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    assert (repo / "kiln.toml").is_file()
    assert (repo / ".kiln" / "kiln.db").is_file()
    assert ".kiln/" in (repo / ".gitignore").read_text()

    config = load_config(repo)
    assert config.base_branch == "main"
    assert config.models.foreman == "claude-opus-5-thinking-high"
    assert config.models.reviewer == "claude-sonnet-5-thinking-high"
    assert config.max_attempts == 3
    assert config.max_foreman_turns == 25

    original = (repo / "kiln.toml").read_text()
    again = runner.invoke(app, ["init"])
    assert again.exit_code == 0
    assert "left unchanged" in again.output
    assert (repo / "kiln.toml").read_text() == original


def test_init_appends_gitignore_without_clobbering(repo):
    (repo / ".gitignore").write_text("dist\n")
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    assert (repo / ".gitignore").read_text() == "dist\n.kiln/\n"


def test_init_outside_a_git_repo_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init"])
    assert result.exit_code != 0
    assert "git repository" in result.output


def test_goal_and_job_flow(repo):
    assert runner.invoke(app, ["init"]).exit_code == 0
    toml = (repo / "kiln.toml").read_text().replace("max_attempts = 3", "max_attempts = 7")
    (repo / "kiln.toml").write_text(toml)

    created = runner.invoke(app, ["goal", "add", "Ship it", "--description", "A factory"])
    assert created.exit_code == 0, created.output
    assert "goal #1" in created.output

    listed = runner.invoke(app, ["goal", "list"])
    assert "#1  active  Ship it" in listed.output

    shown_goal = runner.invoke(app, ["goal", "show", "1"])
    assert shown_goal.exit_code == 0, shown_goal.output
    assert "A factory" in shown_goal.output
    assert "\nbrief\n(none)" in shown_goal.output
    assert "\nevidence\n(none)" in shown_goal.output

    first = runner.invoke(app, ["job", "add", "1", "Schema", "--priority", "1"])
    second = runner.invoke(
        app,
        ["job", "add", "1", "CLI", "--depends-on", "1", "--acceptance", "commands work"],
    )
    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output

    cycle = runner.invoke(app, ["job", "dep", "1", "2"])
    assert cycle.exit_code != 0
    assert "cycle" in cycle.output

    pending = runner.invoke(app, ["job", "list", "--status", "pending"])
    assert "Schema" in pending.output
    assert "deps: #1" in pending.output

    shown = runner.invoke(app, ["job", "show", "2"])
    assert shown.exit_code == 0, shown.output
    assert "attempts    0/7" in shown.output
    assert "commands work" in shown.output

    dashboard = runner.invoke(app, ["status"])
    assert dashboard.exit_code == 0, dashboard.output
    assert "Ship it" in dashboard.output
    assert "Schema" in dashboard.output
    assert "blocked" in dashboard.output

    missing = runner.invoke(app, ["goal", "show", "9"])
    assert missing.exit_code != 0
    assert "no goal with id 9" in missing.output

    cancelled = runner.invoke(app, ["job", "cancel", "2"])
    assert cancelled.exit_code == 0, cancelled.output
    assert "cancelled #2" in cancelled.output


def test_commands_require_init(repo):
    for args in (["status"], ["log"], ["gc"], ["runs", "show", "1"]):
        result = runner.invoke(app, args)
        assert result.exit_code != 0
        assert "kiln init" in result.output


def test_log_and_runs_show(repo):
    assert runner.invoke(app, ["init"]).exit_code == 0
    empty = runner.invoke(app, ["log"])
    assert empty.exit_code == 0, empty.output
    assert "no events" in empty.output

    assert runner.invoke(app, ["goal", "add", "Ship it"]).exit_code == 0
    assert runner.invoke(app, ["job", "add", "1", "Schema"]).exit_code == 0
    logged = runner.invoke(app, ["log"])
    assert logged.exit_code == 0, logged.output
    assert logged.output.index("goal.created") < logged.output.index("job.created")
    assert "job #1" in logged.output

    latest = runner.invoke(app, ["log", "-n", "1"])
    assert "job.created" in latest.output
    assert "goal.created" not in latest.output

    rejected = runner.invoke(app, ["log", "-n", "0"])
    assert rejected.exit_code != 0
    assert "limit" in rejected.output

    from kiln.db import connect
    from kiln.models import RunStatus
    from kiln.runs import finish_run, start_run

    config = load_config(repo)
    conn = connect(config.db_path)
    try:
        run = start_run(conn, role="scout", model="composer-2.5")
        log_path = config.runs_dir / f"{run.id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("".join(f"line {index}\n" for index in range(45)))
        finish_run(
            conn,
            run.id,
            status=RunStatus.succeeded,
            exit_code=0,
            log_path=str(log_path),
            report={"summary": "readme exists"},
        )
    finally:
        conn.close()

    shown = runner.invoke(app, ["runs", "show", "1"])
    assert shown.exit_code == 0, shown.output
    assert "composer-2.5" in shown.output
    assert "readme exists" in shown.output
    assert "job         (none)" in shown.output
    assert "... 5 earlier lines" in shown.output
    assert "line 44" in shown.output
    assert "line 0\n" not in shown.output

    missing = runner.invoke(app, ["runs", "show", "9"])
    assert missing.exit_code != 0
    assert "no run with id 9" in missing.output


def test_goal_show_prints_brief_and_evidence(repo):
    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(app, ["goal", "add", "Ship it"]).exit_code == 0
    from kiln.db import connect
    from kiln.jobs import set_goal_brief, set_goal_evidence

    config = load_config(repo)
    conn = connect(config.db_path)
    try:
        set_goal_brief(conn, 1, "Success: the marker exists.")
        set_goal_evidence(conn, 1, ["pytest passed", "review found no blockers"])
    finally:
        conn.close()

    shown = runner.invoke(app, ["goal", "show", "1"])
    assert shown.exit_code == 0, shown.output
    assert "Success: the marker exists." in shown.output
    assert "- pytest passed" in shown.output
    assert "- review found no blockers" in shown.output


def test_missing_optional_settings_use_defaults(repo):
    (repo / "kiln.toml").write_text(
        """
base_branch = "main"
verify = ""
max_parallel_workers = 2
max_attempts = 3
delete_merged_branches = true

[models]
foreman = "foreman"
worker = "worker"
scout = "scout"
"""
    )
    config = load_config(repo)
    assert config.max_foreman_turns == 25
    assert config.models.reviewer == "claude-sonnet-5-thinking-high"

    (repo / "kiln.toml").write_text((repo / "kiln.toml").read_text().replace(
        'max_parallel_workers = 2',
        'max_parallel_workers = 2\nmax_foreman_turns = 0',
    ))
    with pytest.raises(KilnError, match="max_foreman_turns"):
        load_config(repo)


def test_invalid_config_is_rejected(repo):
    (repo / "kiln.toml").write_text('base_branch = "main"\n')
    with pytest.raises(KilnError, match="models"):
        load_config(repo)
