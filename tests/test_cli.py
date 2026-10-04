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
    assert config.max_attempts == 3

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


def test_goal_and_task_flow(repo):
    assert runner.invoke(app, ["init"]).exit_code == 0
    toml = (repo / "kiln.toml").read_text().replace("max_attempts = 3", "max_attempts = 7")
    (repo / "kiln.toml").write_text(toml)

    created = runner.invoke(app, ["goal", "add", "Ship it", "--description", "A factory"])
    assert created.exit_code == 0, created.output
    assert "goal #1" in created.output

    listed = runner.invoke(app, ["goal", "list"])
    assert "#1  active  Ship it" in listed.output

    first = runner.invoke(app, ["task", "add", "1", "Schema", "--priority", "1"])
    second = runner.invoke(
        app,
        ["task", "add", "1", "CLI", "--depends-on", "1", "--acceptance", "commands work"],
    )
    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output

    cycle = runner.invoke(app, ["task", "dep", "1", "2"])
    assert cycle.exit_code != 0
    assert "cycle" in cycle.output

    pending = runner.invoke(app, ["task", "list", "--status", "pending"])
    assert "Schema" in pending.output
    assert "deps: #1" in pending.output

    shown = runner.invoke(app, ["task", "show", "2"])
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

    cancelled = runner.invoke(app, ["task", "cancel", "2"])
    assert cancelled.exit_code == 0, cancelled.output
    assert "cancelled #2" in cancelled.output


def test_commands_require_init(repo):
    result = runner.invoke(app, ["status"])
    assert result.exit_code != 0
    assert "kiln init" in result.output


def test_invalid_config_is_rejected(repo):
    (repo / "kiln.toml").write_text('base_branch = "main"\n')
    with pytest.raises(KilnError, match="models"):
        load_config(repo)
