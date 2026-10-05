import json
import stat
import sys
from pathlib import Path

import pytest

from kiln.agent import (
    CodexHarness,
    CursorHarness,
    build_command,
    get_harness,
    parse_agent_output,
    run_agent,
)
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


def test_cursor_harness_maps_readonly_to_mode_or_force():
    harness = CursorHarness()
    ask = harness.build_command(
        prompt="look around",
        model="composer-2.5",
        workspace=Path("/tmp/repo"),
        readonly=True,
        bin="agent",
    )
    assert ask == [
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
    edit = harness.build_command(
        prompt="edit",
        model="worker",
        workspace=Path("/tmp/repo"),
        readonly=False,
        bin="agent",
    )
    assert "--mode" not in edit
    assert edit[-3:] == ["--trust", "--force", "edit"]


def test_codex_build_command_sandbox_modes(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("KILN_CODEX_BIN", raising=False)
    harness = CodexHarness()
    readonly = harness.build_command(
        prompt="look around",
        model="gpt-6-luna",
        workspace=Path("/tmp/repo"),
        readonly=True,
        bin=None,
    )
    assert readonly == [
        "codex",
        "exec",
        "-m",
        "gpt-6-luna",
        "-C",
        "/tmp/repo",
        "--skip-git-repo-check",
        "--color",
        "never",
        "--sandbox",
        "read-only",
        "--json",
        "look around",
    ]
    write = harness.build_command(
        prompt="edit",
        model="gpt-6-luna",
        workspace=Path("/tmp/repo"),
        readonly=False,
        bin="codex",
    )
    assert write[write.index("--sandbox") + 1] == "workspace-write"
    assert "--json" in write
    assert write[-1] == "edit"
    monkeypatch.setenv("KILN_CODEX_BIN", "/opt/codex")
    assert harness.build_command(
        prompt="edit",
        model="gpt-6-luna",
        workspace=Path("/tmp/repo"),
        readonly=False,
        bin=None,
    )[0] == "/opt/codex"


def test_codex_parse_output_success():
    stdout = "\n".join(
        json.dumps(event)
        for event in (
            {"type": "thread.started", "thread_id": "t"},
            {"type": "turn.started"},
            {
                "type": "item.completed",
                "item": {
                    "id": "item_0",
                    "type": "agent_message",
                    "text": '```json\n{"ok": false}\n```',
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "id": "item_1",
                    "type": "agent_message",
                    "text": '```json\n{"ok": true}\n```',
                },
            },
            {"type": "turn.completed", "usage": {"output_tokens": 5}},
        )
    )
    parsed = CodexHarness().parse_output("Reading additional input from stdin...\n" + stdout)
    assert parsed.is_error is False
    assert parsed.report == {"ok": True}
    assert parsed.envelope is not None
    assert parsed.envelope["is_error"] is False


def test_codex_parse_output_error():
    stdout = "\n".join(
        json.dumps(event)
        for event in (
            {"type": "thread.started", "thread_id": "t"},
            {
                "type": "item.completed",
                "item": {"id": "item_0", "type": "error", "message": "metadata missing"},
            },
            {"type": "turn.started"},
            {"type": "error", "message": "boom"},
            {"type": "turn.failed", "error": {"message": "boom"}},
        )
    )
    parsed = CodexHarness().parse_output(stdout)
    assert parsed.is_error is True
    assert parsed.report is None


def test_codex_parse_output_bare_fenced_report():
    text = 'note\n```json\n{"summary": "later"}\n```\n'
    parsed = CodexHarness().parse_output(text)
    assert parsed.report == {"summary": "later"}
    assert parsed.is_error is False
    assert parsed.envelope is None


def test_get_harness_rejects_unknown_name():
    assert get_harness("cursor").name == "cursor"
    assert get_harness("codex").name == "codex"
    with pytest.raises(KilnError, match="unknown harness"):
        get_harness("bogus")


def test_run_agent_codex_harness_parses_a_report(tmp_path: Path):
    line = json.dumps(
        {
            "type": "item.completed",
            "item": {
                "id": "item_0",
                "type": "agent_message",
                "text": '```json\n{"ok": true}\n```',
            },
        }
    )
    script = _fake_agent(tmp_path, "print(" + json.dumps(line) + ")\n")
    result = run_agent(
        prompt="probe",
        model="gpt-6-luna",
        workspace=tmp_path,
        log_path=tmp_path / "runs" / "codex.log",
        timeout=10,
        agent_bin=str(script),
        harness=CodexHarness(),
        readonly=True,
    )
    assert result.exit_code == 0
    assert result.timed_out is False
    assert result.report == {"ok": True}
    assert result.parsed.is_error is False
    log = result.log_path.read_text()
    assert "exec" in log
    assert "--sandbox" in log and "read-only" in log
    assert "--json" in log


def test_run_agent_codex_maps_mode_and_force(tmp_path: Path):
    script = _fake_agent(tmp_path, "print('ok')\n")
    ask = run_agent(
        prompt="probe",
        model="gpt-6-luna",
        workspace=tmp_path,
        log_path=tmp_path / "ask.log",
        mode="ask",
        timeout=10,
        agent_bin=str(script),
        harness=CodexHarness(),
    )
    write = run_agent(
        prompt="probe",
        model="gpt-6-luna",
        workspace=tmp_path,
        log_path=tmp_path / "write.log",
        force=True,
        timeout=10,
        agent_bin=str(script),
        harness=CodexHarness(),
    )
    assert "read-only" in ask.log_path.read_text()
    assert "workspace-write" in write.log_path.read_text()


def test_missing_codex_names_the_binary(tmp_path: Path):
    missing = tmp_path / "missing-codex"
    with pytest.raises(KilnError, match="missing-codex"):
        run_agent(
            prompt="probe",
            model="gpt-6-luna",
            workspace=tmp_path,
            log_path=tmp_path / "run.log",
            harness=CodexHarness(),
            agent_bin=str(missing),
            readonly=True,
        )


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
