import json
import stat
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from kiln.cli import app
from kiln.config import init_factory, load_config
from kiln.db import connect
from kiln.errors import KilnError
from kiln.models import RunStatus
from kiln.notes import list_notes
from kiln.roles.scout import run_scout
from kiln.runs import get_run
from kiln.tasks import add_goal

runner = CliRunner()


@pytest.fixture
def factory(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    monkeypatch.chdir(tmp_path)
    init_factory(tmp_path)
    return tmp_path


def test_scout_stores_the_report_as_a_note(factory, monkeypatch):
    capture = factory / "args"
    monkeypatch.setenv("KILN_CAPTURE_ARGS", str(capture))
    script = _agent(
        factory,
        "import os, pathlib, sys\n"
        "pathlib.Path(os.environ['KILN_CAPTURE_ARGS']).write_text('\\n'.join(sys.argv[1:]))\n"
        + _print_report("found it", ["pyproject.toml"]),
    )
    config = load_config(factory)
    conn = connect(config.db_path)
    try:
        goal = add_goal(conn, "Ship it", "A factory")
        outcome = run_scout(
            conn,
            config,
            "Where is the project file?",
            goal_id=goal.id,
            agent_bin=str(script),
        )
    finally:
        conn.close()

    assert outcome.failure is None
    assert outcome.note is not None
    assert "found it" in outcome.note.text
    assert "pyproject.toml" in outcome.note.text
    assert outcome.run.status == RunStatus.succeeded
    assert json.loads(outcome.run.report_json or "")["summary"] == "found it"
    args = capture.read_text()
    assert "--mode\nask" in args
    assert "--trust" in args
    assert "composer-2.5" in args
    log = Path(outcome.run.log_path or "")
    assert "Where is the project file?" in log.read_text()


def test_scout_uses_the_only_active_goal(factory):
    script = _agent(factory, _print_report("only goal", []))
    config = load_config(factory)
    conn = connect(config.db_path)
    try:
        add_goal(conn, "Only")
        outcome = run_scout(conn, config, "What is here?", agent_bin=str(script))
        assert outcome.goal.title == "Only"
        assert list_notes(conn, outcome.goal.id)
    finally:
        conn.close()


def test_failed_scout_does_not_store_a_note(factory):
    script = _agent(
        factory,
        "print("
        + json.dumps(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "nope"}))
        + ")\n",
    )
    config = load_config(factory)
    conn = connect(config.db_path)
    try:
        goal = add_goal(conn, "Ship it")
        outcome = run_scout(conn, config, "Look", goal_id=goal.id, agent_bin=str(script))
        assert outcome.note is None
        assert outcome.failure == "response had no JSON report"
        assert outcome.run.status == RunStatus.failed
        assert list_notes(conn, goal.id) == []
        stored = get_run(conn, outcome.run.id)
        assert stored is not None
        assert stored.log_path
    finally:
        conn.close()


def test_scout_requires_a_goal_when_several_are_active(factory):
    config = load_config(factory)
    conn = connect(config.db_path)
    try:
        add_goal(conn, "One")
        add_goal(conn, "Two")
        with pytest.raises(KilnError, match="--goal"):
            run_scout(conn, config, "Look", agent_bin=str(factory / "missing"))
    finally:
        conn.close()


def test_codex_scout_records_read_only(factory, monkeypatch):
    capture = factory / "args"
    monkeypatch.setenv("KILN_CAPTURE_ARGS", str(capture))
    script = _agent(
        factory,
        "import os, pathlib, sys\n"
        "pathlib.Path(os.environ['KILN_CAPTURE_ARGS']).write_text('\\n'.join(sys.argv[1:]))\n"
        + _print_report("found it", ["pyproject.toml"], codex=True),
    )
    config = replace(load_config(factory), harness="codex")
    conn = connect(config.db_path)
    try:
        goal = add_goal(conn, "Ship it", "A factory")
        outcome = run_scout(
            conn,
            config,
            "Where is the project file?",
            goal_id=goal.id,
            agent_bin=str(script),
        )
    finally:
        conn.close()

    assert outcome.failure is None
    assert outcome.note is not None
    assert "found it" in outcome.note.text
    args = capture.read_text().splitlines()
    assert args[0] == "exec"
    assert args[args.index("--sandbox") + 1] == "read-only"
    log = Path(outcome.run.log_path or "").read_text()
    assert log.startswith(f"$ {script} exec ")


def test_cli_scout(factory, monkeypatch):
    script = _agent(factory, _print_report("cli report", ["src/kiln"]))
    monkeypatch.setenv("KILN_AGENT_BIN", str(script))
    assert runner.invoke(app, ["goal", "add", "Ship it"]).exit_code == 0
    result = runner.invoke(app, ["scout", "What is the layout?"])
    assert result.exit_code == 0, result.output
    assert "scouting goal #1" in result.output
    assert "cli report" in result.output
    assert "note #1" in result.output


def _print_report(summary: str, findings: list[str], *, codex: bool = False) -> str:
    fenced = "```json\n" + json.dumps({"summary": summary, "findings": findings}) + "\n```"
    if codex:
        printed = json.dumps(
            {
                "type": "item.completed",
                "item": {"id": "item_0", "type": "agent_message", "text": fenced},
            }
        )
    else:
        printed = json.dumps(
            {"type": "result", "subtype": "success", "is_error": False, "result": fenced}
        )
    return "print(" + json.dumps(printed) + ")\n"


def _agent(directory: Path, body: str) -> Path:
    path = directory / "fake-agent"
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path
