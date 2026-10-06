import json
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

from kiln.agent import (
    _LIVE_LOCK,
    build_command,
    kill_live_agents,
    parse_agent_output,
    run_agent,
    shutdown_agents,
)
import kiln.agent as agent_module
from kiln.errors import KilnError
from kiln.prompts import render_prompt

PROBE_ENVELOPE = (
    '{"type":"result","subtype":"success","is_error":false,"duration_ms":1896,'
    '"result":"```json\\n{\\"ok\\": true}\\n```","session_id":"abc"}'
)


def test_parse_unwraps_the_json_envelope_and_takes_the_last_fence():
    parsed = parse_agent_output(PROBE_ENVELOPE)
    assert parsed.envelope is not None
    assert parsed.envelope["session_id"] == "abc"
    assert parsed.report == {"ok": True}
    assert parsed.is_error is False

    text = 'intro\n```json\n{"ok": false}\n```\n\n```json\n{"summary": "later"}\n```\n'
    assert parse_agent_output(text).report == {"summary": "later"}


def test_parse_accepts_a_bare_object_and_rejects_prose():
    assert parse_agent_output('{"summary": "hi"}').report == {"summary": "hi"}
    assert parse_agent_output("I looked around the repo.").report is None
    assert parse_agent_output("[1, 2]").report is None


def test_parse_flags_agent_errors():
    stdout = json.dumps(
        {"type": "result", "subtype": "error", "is_error": True, "result": "boom"}
    )
    parsed = parse_agent_output(stdout)
    assert parsed.is_error is True
    assert parsed.report is None


def test_build_command_locks_print_flags():
    command = build_command(
        prompt="look around",
        model="composer-2.5",
        workspace=Path("/tmp/repo"),
        mode="ask",
        agent_bin="agent",
    )
    assert command == [
        "agent",
        "-p",
        "--output-format",
        "json",
        "--workspace",
        "/tmp/repo",
        "--model",
        "composer-2.5",
        "--trust",
        "--mode",
        "ask",
        "look around",
    ]
    forced = build_command(
        prompt="edit",
        model="worker",
        workspace=Path("/tmp/repo"),
        force=True,
        trust=False,
        agent_bin="agent",
    )
    assert "--mode" not in forced
    assert "--trust" not in forced
    assert forced[-2:] == ["--force", "edit"]


def test_run_agent_streams_output_and_parses_the_report(tmp_path: Path):
    script = _fake_agent(
        tmp_path,
        'print(' + json.dumps(PROBE_ENVELOPE) + ")\n",
    )
    result = run_agent(
        prompt="probe",
        model="composer-2.5",
        workspace=tmp_path,
        log_path=tmp_path / "runs" / "1.log",
        mode="ask",
        timeout=10,
        agent_bin=str(script),
    )
    assert result.exit_code == 0
    assert result.timed_out is False
    assert result.report == {"ok": True}
    log = result.log_path.read_text()
    assert "--- prompt ---\nprobe\n--- output ---" in log
    assert "--mode" in log and "ask" in log


def test_run_agent_times_out(tmp_path: Path):
    script = _fake_agent(tmp_path, "import time\ntime.sleep(30)\n")
    result = run_agent(
        prompt="wait",
        model="composer-2.5",
        workspace=tmp_path,
        log_path=tmp_path / "run.log",
        timeout=0.5,
        agent_bin=str(script),
    )
    assert result.timed_out is True
    assert "timed out" in result.log_path.read_text()


def test_kill_live_agents_stops_a_running_agent(tmp_path: Path):
    script = _fake_agent(tmp_path, "import time\ntime.sleep(30)\n")
    log_path = tmp_path / "run.log"
    holder: dict[str, object] = {}

    def go() -> None:
        holder["result"] = run_agent(
            prompt="wait",
            model="composer-2.5",
            workspace=tmp_path,
            log_path=log_path,
            timeout=30,
            agent_bin=str(script),
        )

    thread = threading.Thread(target=go)
    thread.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not log_path.exists():
        time.sleep(0.02)
    assert log_path.exists()
    kill_live_agents()
    thread.join(5)

    assert not thread.is_alive()
    result = holder["result"]
    assert result.timed_out is False
    assert result.exit_code != 0


def test_shutdown_agents_kills_what_is_running_and_refuses_the_next_spawn(tmp_path: Path):
    script = _fake_agent(tmp_path, "import time\ntime.sleep(30)\n")
    log_path = tmp_path / "run.log"
    holder: dict[str, object] = {}

    def go() -> None:
        holder["result"] = run_agent(
            prompt="wait",
            model="composer-2.5",
            workspace=tmp_path,
            log_path=log_path,
            timeout=30,
            agent_bin=str(script),
        )

    thread = threading.Thread(target=go)
    thread.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not log_path.exists():
        time.sleep(0.02)
    assert log_path.exists()
    try:
        shutdown_agents()
        thread.join(5)
        assert not thread.is_alive()
        result = holder["result"]
        assert result.timed_out is False
        assert result.exit_code != 0
        with pytest.raises(KilnError, match="shutting down"):
            run_agent(
                prompt="wait",
                model="composer-2.5",
                workspace=tmp_path,
                log_path=tmp_path / "next.log",
                timeout=2,
                agent_bin=str(script),
            )
        assert not (tmp_path / "next.log").exists()
    finally:
        with _LIVE_LOCK:
            agent_module._SHUTTING_DOWN = False


def test_missing_agent_is_an_error(tmp_path: Path):
    with pytest.raises(KilnError, match="not found"):
        run_agent(
            prompt="probe",
            model="composer-2.5",
            workspace=tmp_path,
            log_path=tmp_path / "run.log",
            agent_bin=str(tmp_path / "missing-agent"),
        )


def test_prompt_values_are_not_expanded_twice():
    rendered = render_prompt(
        "scout.md",
        {
            "repo_root": "/repo",
            "goal_id": "1",
            "goal_title": "Ship",
            "goal_description": "contains {{question}} literally",
            "question": "Where is {{goal_title}}?",
        },
    )
    assert "Where is {{goal_title}}?" in rendered
    assert "contains {{question}} literally" in rendered
    assert "Goal #1: Ship" in rendered


def _fake_agent(directory: Path, body: str) -> Path:
    path = directory / "agent"
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path
